"""OpenCode sessions in `opencode.db` under `~/.local/share/opencode/`.

A session is its `session_v2` row and its `session_message` rows in `seq`
order; a source's path is `<database>#<session id>`. OpenCode rewrites a
turn's rows until it ends the turn with an `idle` row, and never after, so
only rows through a session's latest `idle` row are read. Sessions that
OpenCode 1.x wrote, migrated into these tables, hold no `idle` row and no
longer change; all their rows are read. The 1.x tables are never read.
"""

import json
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Generator

from .. import rows
from . import (
    IMAGE_OMITTED,
    Expectation,
    Found,
    Head,
    Listing,
    Message,
    count_unknown,
    loads,
    parts,
    utc_time,
)

NAME = "opencode"
DATABASE = "opencode.db"
_LAST_KEY = 2**63 - 1
# Turn ends, agent switches, and the host's own context: the instructions
# it loads (`synthetic`) and its notices to the model (`system`).
_SKIPPED = frozenset({"idle", "agent-switched", "synthetic", "system"})


def _split(path: Path) -> tuple[Path, str]:
    database, _, session = str(path).rpartition("#")
    return Path(database), session


def _migrated(version: str) -> bool:
    return version.startswith("1.")


def discover(roots: tuple[Path, ...], listing: Listing) -> list[Found]:
    found: list[Found] = []
    for root in roots:
        entries = listing(str(root))
        if entries is None or DATABASE not in entries[1]:
            continue
        database = root / DATABASE
        try:
            sessions = rows.fetch(
                database,
                "SELECT session_id, max(seq), count(*), max(time_updated) FROM session_message "
                "GROUP BY session_id",
            )
        except OSError:
            continue
        except rows.UnsupportedFile as error:
            raise Expectation(str(error)) from error
        for session, last, count, updated in sessions:
            found.append(Found(session, session, f"{database}#{session}",
                               token=f"{last}:{count}", mtime=updated / 1000))
    return found


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    database, session = _split(path)
    found = rows.fetch(
        database,
        "SELECT parent_id, directory, version, time_created, time_viewed, "
        "(SELECT count(*) FROM session_message WHERE session_id = s.id AND type = 'user') "
        "FROM session_v2 s WHERE id = ?",
        (session,),
    )
    if not found:
        return None
    parent, directory, version, created, viewed, users = found[0]
    # The TUI marks a session it shows; OpenCode 1.x did not, so a migrated
    # session with a second prompt counts as typed into.
    if parent:
        session_class = "subagent"
    elif viewed is not None or (_migrated(version) and users >= 2):
        session_class = "interactive"
    else:
        session_class = "worker"
    return Head(
        session_id=session,
        session_class=session_class,
        started=utc_time(created),
        parent=parent,
        cwd=directory,
        cli_version=version,
    )


class _Messages:
    def rows(self, path: Path, after: int | None, through: int | None) -> Generator[tuple[int, bytes], None, None]:
        database, session = _split(path)
        with closing(rows.connect(database)) as db:
            found = db.execute("SELECT version FROM session_v2 WHERE id = ?", (session,)).fetchone()
            if found is None:
                raise FileNotFoundError(f"{path}: the session is gone")
            bound = _LAST_KEY if through is None else through
            if through is None and not _migrated(found[0]):
                (idle,) = db.execute(
                    "SELECT max(seq) FROM session_message WHERE session_id = ? AND type = 'idle'",
                    (session,),
                ).fetchone()
                if idle is None:
                    return
                bound = min(bound, idle)
            for seq, message_id, kind, created, data in db.execute(
                "SELECT seq, id, type, time_created, data FROM session_message "
                "WHERE session_id = ? AND seq > ? AND seq <= ? ORDER BY seq",
                (session, -1 if after is None else after, bound),
            ):
                record = {"id": message_id, "type": kind, "time": created, "data": data}
                yield seq, json.dumps(record).encode()


def _content_text(content: object) -> str:
    if not isinstance(content, list):
        raise Expectation("tool output is not a list")
    pieces = []
    for item in content:
        if not isinstance(item, dict):
            raise Expectation("tool output item is not an object")
        if item.get("type") == "text":
            pieces.append(str(item.get("text", "")))
        elif str(item.get("mime") or item.get("uri") or "").startswith(("image/", "data:image/")):
            pieces.append(IMAGE_OMITTED)
        else:
            pieces.append("[file omitted]")
    return "\n".join(pieces)


def _error_text(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type"))
    return str(error)


def _assistant(data: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    content = data.get("content")
    if not isinstance(content, list):
        raise Expectation("assistant content is not a list")
    out: list[tuple[str, str | None, str]] = []
    for block in content:
        if not isinstance(block, dict):
            raise Expectation("assistant block is not an object")
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text", ""))
            if text.strip():
                out.append(("agent", None, text))
        elif block_type == "tool":
            name = str(block.get("name") or "tool")
            tool_state = block.get("state")
            if not isinstance(tool_state, dict):
                raise Expectation("tool block without a state")
            out.append(("tool_call", name, json.dumps(tool_state.get("input"), ensure_ascii=False)))
            status = tool_state.get("status")
            if status == "completed":
                out.append(("tool_result", name, _content_text(tool_state.get("content"))))
            elif status == "error":
                out.append(("tool_result", name, _error_text(tool_state.get("error"))))
            else:
                count_unknown(state, f"tool_status:{status}")
        elif block_type == "reasoning":
            continue
        else:
            count_unknown(state, f"assistant_block:{block_type}")
    # A failed or aborted reply; the turn's `idle` row repeats it.
    if data.get("error"):
        out.append(("event", None, _error_text(data["error"])))
    return out


def map_records(
    records: list[rows.Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    for record in records:
        try:
            row = loads(record.data)
            data = loads(row["data"])
        except ValueError as error:
            raise Expectation(f"row {record.index} is not JSON: {error}") from error
        if not isinstance(data, dict):
            raise Expectation(f"row {record.index} is not an object")
        kind = row["type"]
        time = utc_time(row["time"])
        found: list[tuple[str, str | None, str]] = []
        if kind == "user":
            text = data.get("text")
            if not isinstance(text, str):
                raise Expectation(f"user row {record.index} has no text")
            found = [("user", None, text)] if text.strip() else []
        elif kind == "assistant":
            found = _assistant(data, state)
        elif kind == "compaction":
            found = [("event", None, "context compacted")]
        elif kind in _SKIPPED:
            continue
        else:
            count_unknown(state, f"type:{kind}")
            continue
        turn = state.get("turn", time)
        part = 0
        for message_kind, tool, text in found:
            if message_kind == "user":
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, message_kind, tool, text,  # type: ignore[arg-type]
                             f"opencode:{row['id']}")
            messages.extend(produced)
            part += len(produced)
    return messages, state


class _OpenCode:
    name = NAME
    reader = rows.Reader(_Messages())
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)


ADAPTER = _OpenCode()
