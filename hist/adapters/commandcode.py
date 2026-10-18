"""Command Code transcripts under `~/.commandcode/projects/<project>/`.

`<session>.jsonl` is a session: a `session` header, then `message` records
whose `meta.source` says who wrote them. `<session>.meta.json`, written
before the transcript, marks headless runs with `entrypoint: "print"`.
Skill bodies and reminders the host adds to a prompt live in
`meta.injected`, never in the content, so they are never read.
"""

import json
import re
from pathlib import Path
from typing import Any, Callable

from .. import reader
from ..reader import Cursor, Record, read as read_records
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
)

NAME = "commandcode"
EPOCH = "1970-01-01T00:00:00.000Z"
HEAD_BYTES = 64 * 1024
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_METADATA = frozenset({"session", "model_change", "session_info"})
# What the person typed: a prompt, or a note sent while the agent works.
_TYPED = frozenset({"user", "steering"})
# The host's "Please continue your response" nudge after an empty reply.
_NUDGE = "followup"


def discover(roots: tuple[Path, ...], listing: Listing) -> list[Found]:
    found: list[Found] = []
    for root in map(str, roots):
        projects = listing(root)
        if projects is None:
            continue
        for project in projects[0]:
            entries = listing(f"{root}/{project}")
            if entries is None:
                continue
            for name in entries[1]:
                stem = name[: -len(".jsonl")]
                if name.endswith(".jsonl") and _SESSION.fullmatch(stem):
                    found.append(Found(stem, stem, f"{root}/{project}/{name}"))
    return found


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    records, _ = read_records(path, Cursor(), max_bytes=HEAD_BYTES)
    if not records:
        return None
    value = _object(records[0])
    if value.get("type") != "session" or not isinstance(value.get("cwd"), str):
        raise Expectation("first record is not a session header with a cwd")
    if value.get("id") != path.stem:
        raise Expectation("session id does not match the file name")
    try:
        meta = json.loads(path.with_name(f"{path.stem}.meta.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        meta = None
    except ValueError as error:
        raise Expectation(f"session metadata is not JSON: {error}") from error
    if meta is None:
        session_class = "unclassified"
    elif not isinstance(meta, dict):
        raise Expectation("session metadata is not an object")
    else:
        session_class = "worker" if meta.get("entrypoint") == "print" else "interactive"
    return Head(
        session_id=path.stem,
        session_class=session_class,
        started=normalize_time(value.get("timestamp"), EPOCH),
        cwd=value["cwd"],
    )


def _object(record: Record) -> dict[str, Any]:
    try:
        value = loads(record.data)
    except ValueError as error:
        raise Expectation(f"record {record.index} is not JSON: {error}") from error
    if not isinstance(value, dict):
        raise Expectation(f"record {record.index} is not an object")
    return value


def _text(blocks: object) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        raise Expectation("content is neither text nor a list")
    pieces = []
    for block in blocks:
        if not isinstance(block, dict):
            raise Expectation("content block is not an object")
        if block.get("type") == "text":
            pieces.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            pieces.append(IMAGE_OMITTED)
        else:
            pieces.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(pieces)


def _user(content: list[Any], source: object, state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    if source == _NUDGE:
        return []
    if source == "tool":
        tools: dict[str, str] = state.setdefault("tools", {})
        out: list[tuple[str, str | None, str]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                raise Expectation("tool message holds a non-result block")
            name = tools.pop(str(block.get("tool_use_id")), None)
            out.append(("tool_result", name, _text(block.get("content"))))
        return out
    if source in _TYPED:
        text = _text(content)
        return [("user", None, text)] if text.strip() else []
    # Without a source: the host's error notice, such as an upstream timeout.
    if source is None:
        return [("event", None, _text(content))]
    count_unknown(state, f"user_source:{source}")
    return []


def _assistant(content: list[Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    tools: dict[str, str] = state.setdefault("tools", {})
    out: list[tuple[str, str | None, str]] = []
    for block in content:
        if not isinstance(block, dict):
            raise Expectation("assistant block is not an object")
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text", ""))
            if text.strip():
                out.append(("agent", None, text))
        elif block_type == "tool_use":
            name = str(block.get("name") or "tool")
            tools[str(block.get("id"))] = name
            if len(tools) > 256:
                tools.pop(next(iter(tools)))
            out.append(("tool_call", name, json.dumps(block.get("input"), ensure_ascii=False)))
        elif block_type == "thinking":
            continue
        else:
            count_unknown(state, f"assistant_block:{block_type}")
    return out


def map_records(
    records: list[Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    for record in records:
        value = _object(record)
        record_type = value.get("type")
        if record_type in _METADATA:
            continue
        if record_type != "message":
            count_unknown(state, f"type:{record_type}")
            continue
        message = value.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise Expectation(f"record {record.index} has no message content")
        meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
        time = normalize_time(value.get("timestamp"), state.get("time", EPOCH))
        state["time"] = time
        role = message.get("role")
        if role == "user":
            found = _user(message["content"], meta.get("source"), state)
        elif role == "assistant":
            found = _assistant(message["content"], state)
        else:
            count_unknown(state, f"role:{role}")
            continue
        message_id = meta.get("messageId")
        key = f"commandcode:{message_id}" if isinstance(message_id, str) else None
        turn = state.get("turn", time)
        part = 0
        for kind, tool, text in found:
            if kind == "user":
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, kind, tool, text, key)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


class _CommandCode:
    name = NAME
    reader = reader
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)


ADAPTER = _CommandCode()
