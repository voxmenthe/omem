"""Devin CLI sessions in `sessions.db` under `~/.local/share/devin/cli/`.

A session is its `sessions` row and a forest of `message_nodes` rows, read
in `row_id` order; a source's path is `<database>#<session id>`. Each chain
starts at a root node: the main conversation under a system prompt, or a
subagent, sidekick, or summarizer under its own. Only main chains are
indexed. Devin copies a message into each chain that carries it under one
`message_id`, so that id is a message's key and the first copy wins. Rows
are complete when written.

Devin records no sign of whether a person typed into a session, so every
session is a worker. Sessions that `hist summarize devin` runs are skipped.
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
    normalize_time,
    parts,
    utc_time,
)

NAME = "devin"
DATABASE = "sessions.db"
SUMMARIZER = "hist-summarize-"
_LAST_KEY = 2**63 - 1


def _split(path: Path) -> tuple[Path, str]:
    database, _, session = str(path).rpartition("#")
    return Path(database), session


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
                "SELECT s.id, s.last_activity_at, max(m.row_id), count(*) FROM sessions s "
                "JOIN message_nodes m ON m.session_id = s.id GROUP BY s.id",
            )
        except OSError:
            continue
        except rows.UnsupportedFile as error:
            raise Expectation(str(error)) from error
        for session, active, last, count in sessions:
            found.append(Found(session, session, f"{database}#{session}",
                               token=f"{last}:{count}", mtime=float(active)))
    return found


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    database, session = _split(path)
    found = rows.fetch(database, "SELECT working_directory, created_at FROM sessions WHERE id = ?", (session,))
    if not found:
        return None
    cwd, created = found[0]
    where = Path(cwd)
    return Head(
        session_id=session,
        session_class="worker",
        started=utc_time(created * 1000),
        cwd=cwd,
        skip="hist summarizer run"
        if where.name == "work" and where.parent.name.startswith(SUMMARIZER)
        else None,
    )


class _Nodes:
    def rows(self, path: Path, after: int | None, through: int | None) -> Generator[tuple[int, bytes], None, None]:
        database, session = _split(path)
        with closing(rows.connect(database)) as db:
            if db.execute("SELECT 1 FROM sessions WHERE id = ?", (session,)).fetchone() is None:
                raise FileNotFoundError(f"{path}: the session is gone")
            parents = dict(
                db.execute("SELECT node_id, parent_node_id FROM message_nodes WHERE session_id = ?", (session,))
            )
            # What decides a chain's kind; a root can be stored after its chain's rows.
            roots = {
                node: {"role": role, "source": source, "operation": operation, "text": text}
                for node, role, source, operation, text in db.execute(
                    "SELECT node_id, json_extract(chat_message, '$.role'), "
                    "json_extract(chat_message, '$.metadata.telemetry.source'), "
                    "json_extract(chat_message, '$.metadata.telemetry.operation'), "
                    "substr(json_extract(chat_message, '$.content'), 1, 200) "
                    "FROM message_nodes WHERE session_id = ? AND parent_node_id IS NULL",
                    (session,),
                )
            }
            for row_id, node, created, message in db.execute(
                "SELECT row_id, node_id, created_at, chat_message FROM message_nodes "
                "WHERE session_id = ? AND row_id > ? AND row_id <= ? ORDER BY row_id",
                (session, -1 if after is None else after, _LAST_KEY if through is None else through),
            ):
                root = roots.get(_root(parents, node))
                record = {"created": created, "root": root, "message": message}
                yield row_id, json.dumps(record).encode()


def _root(parents: dict[int, int | None], node: int) -> int | None:
    """The node's chain root; None when an ancestor is missing or loops."""

    for _ in range(len(parents)):
        parent = parents.get(node, -1)
        if parent is None:
            return node
        if parent == -1:
            return None
        node = parent
    return None


def _chain(root: dict[str, Any]) -> str:
    """"main", "side" (a subagent, sidekick, or summarizer), or an unknown
    root's description."""

    source, operation = root["source"], str(root["operation"])
    text = str(root["text"] or "").lstrip()
    if source == "sysprompt":
        if operation in ("normal", "ask"):
            return "main"
        return "side" if operation.startswith("subagent_") else f"sysprompt:{operation}"
    if text.startswith(("<system_info>", "You are Devin")) or source == "rules":
        return "main"
    if text.startswith("You are a Summarizer") or "Sidekick subagent" in text[:80]:
        return "side"
    # A subagent's task, handed to it as its chain's first message.
    if root["role"] == "user":
        return "side"
    return f"{root['role']}:{source}"


def _message(message: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, str):
        raise Expectation(f"{role} message content is not text")
    tools: dict[str, str] = state.setdefault("tools", {})
    if role == "user":
        # Without the flag: the host's compaction prompts and keep-alives.
        if (message.get("metadata") or {}).get("is_user_input"):
            return [("user", None, content)]
        return []
    if role == "assistant":
        out: list[tuple[str, str | None, str]] = []
        if content.strip():
            out.append(("agent", None, content))
        for call in message.get("tool_calls") or []:
            name = str(call.get("name") or "tool")
            tools[str(call.get("id"))] = name
            if len(tools) > 256:
                tools.pop(next(iter(tools)))
            out.append(("tool_call", name, str(call.get("arguments"))))
        return out
    if role == "tool":
        operation = ((message.get("metadata") or {}).get("telemetry") or {}).get("operation")
        name = tools.pop(str(message.get("tool_call_id")), None) or operation
        images = [IMAGE_OMITTED] * len(message.get("images") or [])
        return [("tool_result", name, "\n".join([content, *images]))]
    if role == "system":
        return []
    count_unknown(state, f"role:{role}")
    return []


def map_records(
    records: list[rows.Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    for record in records:
        try:
            row = loads(record.data)
            message = loads(row["message"])
        except ValueError as error:
            raise Expectation(f"row {record.index} is not JSON: {error}") from error
        if not isinstance(message, dict):
            raise Expectation(f"row {record.index} is not an object")
        chain = "detached" if row["root"] is None else _chain(row["root"])
        if chain not in ("main", "side"):
            count_unknown(state, f"chain:{chain}")
        if chain != "main":
            continue
        time = normalize_time((message.get("metadata") or {}).get("created_at"), utc_time(row["created"] * 1000))
        turn = state.get("turn", time)
        part = 0
        key = f"devin:{message['message_id']}" if isinstance(message.get("message_id"), str) else None
        for kind, tool, text in _message(message, state):
            # A later chain's copy of an earlier prompt does not start a turn.
            if kind == "user" and time > turn:
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, kind, tool, text, key)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


class _Devin:
    name = NAME
    reader = rows.Reader(_Nodes())
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)


ADAPTER = _Devin()
