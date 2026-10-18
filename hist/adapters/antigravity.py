"""Antigravity CLI conversations under `~/.gemini/antigravity-cli/conversations/`.

`<id>.db` is one conversation: its `steps` rows, read in `idx` order, each a
protobuf `gemini_coder.Step`, and its `trajectory_metadata_blob`. Messages
are decoded straight from the wire format by the field numbers below. A
step is rewritten until it reaches a final status. The database has a
`-wal` file only while the CLI holds the conversation open; then reading
stops at the first step not yet final. Once closed, every step is final.

Tool steps of the older, specific types (view file, run command, ...) are
counted, not indexed; the planner's call to each still is.
"""

import json
import os
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Generator
from urllib.parse import unquote, urlparse

from .. import rows
from . import (
    IMAGE_OMITTED,
    Expectation,
    Found,
    Head,
    Listing,
    Message,
    count_unknown,
    parts,
    utc_time,
)

NAME = "antigravity"
EPOCH = "1970-01-01T00:00:00.000Z"
_LAST_KEY = 2**63 - 1
_FINAL = (3, 4, 5, 6, 7, 12)  # DONE, INVALID, CLEARED, CANCELED, ERROR, INTERRUPTED

# Step types (`CortexStepType`) and the Step field holding each one's body.
USER_INPUT, PLANNER_RESPONSE, GENERIC, ERROR_MESSAGE, CHECKPOINT = 14, 15, 132, 17, 23
_BODY = {USER_INPUT: 19, PLANNER_RESPONSE: 20, GENERIC: 140, ERROR_MESSAGE: 24}
# The host's own context: system, ephemeral, and conversation-history messages.
_DROPPED = frozenset({101, 90, 98})
_METADATA = 5  # CortexStepMetadata: 1 created_at, 4 tool_call
# ChatToolCall: 1 id, 2 name, 3 arguments_json.
# CortexStepUserInput: 2 user_response, 9 media.
# CortexStepPlannerResponse: 1 response, 7 tool_calls; 3 thinking is not read.
# CortexStepGeneric: 2 result (GenericStepResult: 1 result, 4 media).
# CortexStepErrorMessage: 3 error (CortexErrorDetails: 1 user_error_message, 2 short_error).
# CortexTrajectoryMetadata: 1 workspaces (CortexWorkspaceMetadata:
#   1 workspace_folder_absolute_uri, 3 repository (Repository: 2 git_origin_url)),
#   2 created_at, 5 parent_conversation_id.
# The first prompt's user_config.planner_config.tool_config.mcp.non_blocking_turn_load,
# set false only by a `-p` run.
_PRINT_FLAG = (12, 1, 13, 16, 4)
Fields = dict[int, list[Any]]


def decode(data: bytes) -> Fields:
    """Protobuf wire format: field number to its values, varints as ints and
    length-delimited values as bytes."""

    found: Fields = {}
    i, end = 0, len(data)
    try:
        while i < end:
            key, i = _varint(data, i)
            number, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _varint(data, i)
            elif wire == 2:
                size, i = _varint(data, i)
                value, i = data[i : i + size], i + size
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
                value, i = data[i : i + size], i + size
            else:
                raise Expectation(f"protobuf wire type {wire}")
            if i > end:
                raise Expectation("protobuf message is truncated")
            found.setdefault(number, []).append(value)
    except IndexError as error:
        raise Expectation("protobuf varint is truncated") from error
    return found


def _varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _message(fields: Fields, number: int) -> Fields:
    return decode(fields[number][0]) if number in fields else {}


def _text(fields: Fields, number: int) -> str:
    return fields[number][0].decode("utf-8", errors="replace") if number in fields else ""


def _milliseconds(timestamp: Fields) -> int:
    return timestamp.get(1, [0])[0] * 1000 + timestamp.get(2, [0])[0] // 1_000_000


def discover(roots: tuple[Path, ...], listing: Listing) -> list[Found]:
    found: list[Found] = []
    for root in roots:
        directory = root / "conversations"
        entries = listing(str(directory))
        if entries is None:
            continue
        files = set(entries[1])
        for name in entries[1]:
            if not name.endswith(".db"):
                continue
            path = directory / name
            try:
                stats = [os.stat(path)]
                if f"{name}-wal" in files:
                    stats.append(os.stat(f"{path}-wal"))
            except OSError:
                continue
            token = ":".join(f"{s.st_size}:{s.st_mtime_ns}" for s in stats)
            found.append(Found(path.stem, path.stem, str(path), token=token,
                               mtime=max(s.st_mtime for s in stats)))
    return found


def _is_open(path: Path) -> bool:
    return path.with_name(f"{path.name}-wal").exists()


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    blobs = rows.fetch(path, "SELECT data FROM trajectory_metadata_blob")
    prompts = rows.fetch(path, "SELECT step_payload FROM steps WHERE step_type = ? ORDER BY idx", (USER_INPUT,))
    if not blobs:
        return None
    meta = decode(blobs[0][0])
    workspace = _message(meta, 1)
    folder = _text(workspace, 1)
    cwd = unquote(urlparse(folder).path) if folder else None
    origin = _text(_message(workspace, 3), 2) or None
    parent = _text(meta, 5) or None
    first = decode(prompts[0][0]) if prompts else {}
    flag: Fields = _message(first, _BODY[USER_INPUT])
    for number in _PRINT_FLAG[:-1]:
        flag = _message(flag, number)
    if parent:
        session_class = "subagent"
    elif flag.get(_PRINT_FLAG[-1]) == [0]:
        session_class = "worker"
    elif len(prompts) >= 2 or path.stem in _typed(path.parent.parent / "history.jsonl"):
        session_class = "interactive"
    elif _is_open(path):
        # The CLI writes a prompt to its history just after the step; wait for it.
        return None
    else:
        session_class = "worker"
    created = _message(meta, 2) or _message(_message(first, _METADATA), 1)
    return Head(
        session_id=path.stem,
        session_class=session_class,
        started=utc_time(_milliseconds(created)) if created else EPOCH,
        parent=parent,
        cwd=cwd,
        origin=origin,
    )


def _typed(history: Path) -> set[str]:
    """Conversations that a person typed a prompt into, from the CLI's
    prompt history."""

    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    typed = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("conversationId"), str):
            typed.add(entry["conversationId"])
    return typed


class _Steps:
    def rows(self, path: Path, after: int | None, through: int | None) -> Generator[tuple[int, bytes], None, None]:
        settled = through is not None or not _is_open(path)
        with closing(rows.connect(path)) as db:
            for idx, status, payload in db.execute(
                "SELECT idx, status, step_payload FROM steps WHERE idx > ? AND idx <= ? ORDER BY idx",
                (-1 if after is None else after, _LAST_KEY if through is None else through),
            ):
                if not settled and status not in _FINAL:
                    return
                yield idx, bytes(payload)


def _media(body: Fields, number: int) -> list[str]:
    return [IMAGE_OMITTED] * len(body.get(number, []))


def _step(kind: int, step: Fields, state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    body = _message(step, _BODY[kind]) if kind in _BODY else {}
    if kind == USER_INPUT:
        text = "\n".join([_text(body, 2), *_media(body, 9)])
        return [("user", None, text)] if text.strip() else []
    if kind == PLANNER_RESPONSE:
        out: list[tuple[str, str | None, str]] = []
        response = _text(body, 1)
        if response.strip():
            out.append(("agent", None, response))
        for raw in body.get(7, []):
            call = decode(raw)
            out.append(("tool_call", _text(call, 2) or "tool", _text(call, 3)))
        return out
    if kind == GENERIC:
        name = _text(_message(_message(step, _METADATA), 4), 2) or None
        result = _message(body, 2)
        return [("tool_result", name, "\n".join([_text(result, 1), *_media(result, 4)]))]
    if kind == ERROR_MESSAGE:
        error = _message(body, 3)
        return [("event", None, _text(error, 1) or _text(error, 2))]
    if kind == CHECKPOINT:
        return [("event", None, "context compacted")]
    if kind not in _DROPPED:
        count_unknown(state, f"step:{kind}")
    return []


def map_records(
    records: list[rows.Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    for record in records:
        step = decode(record.data)
        if not isinstance(step.get(1, [None])[0], int):
            raise Expectation(f"step {record.index} has no type")
        created = _message(_message(step, _METADATA), 1)
        time = utc_time(_milliseconds(created)) if created else state.get("time", EPOCH)
        state["time"] = time
        turn = state.get("turn", time)
        part = 0
        for kind, tool, text in _step(step[1][0], step, state):
            if kind == "user":
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, kind, tool, text, None)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


class _Antigravity:
    name = NAME
    reader = rows.Reader(_Steps())
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)


ADAPTER = _Antigravity()
