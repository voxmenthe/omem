"""Codex rollouts: `<home>/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl`.

A home is a directory holding `sessions` or `archived_sessions`, found up to
four levels below a configured root. Rollout file names end in the thread
id (plus `_<rollout id>` after a revert), so discovery opens no files.
Injected context is recognised the way Codex itself recognises it: by each
content item's recorded kind, or by Codex's tag pairs in older rollouts.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from ..reader import Record, read as read_records, Cursor
from . import (
    IMAGE_OMITTED,
    Expectation,
    Found,
    Head,
    HookInput,
    Listing,
    Message,
    SessionClass,
    count_unknown,
    loads,
    normalize_time,
    parts,
)

NAME = "codex"
HOME_DEPTH = 4
SESSION_DIRS = ("sessions", "archived_sessions")
ROLLOUT = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:_([0-9A-Za-z-]+))?\.jsonl(\.zst)?"
)
EPOCH = "1970-01-01T00:00:00.000Z"

# Codex's contextual user fragments: (start, end, case-insensitive start).
_FRAGMENTS: tuple[tuple[str, str, bool], ...] = (
    ("# AGENTS.md instructions", "</INSTRUCTIONS>", True),
    ("<user_instructions>", "</user_instructions>", True),
    ("<environment_context>", "</environment_context>", True),
    ("<skill>", "</skill>", True),
    ("<user_shell_command>", "</user_shell_command>", True),
    ("<turn_aborted>", "</turn_aborted>", True),
    ("<goal_context>", "</goal_context>", True),
    ("<recommended_plugins>", "</recommended_plugins>", True),
    ("<codex_internal_context source=", "</codex_internal_context>", False),
    ("<hook_prompt", "</hook_prompt>", False),
)
_EXTERNAL = re.compile(r"<external_([A-Za-z0-9_]+)>[\s\S]*</external_\1>\Z")
_LEGACY_WARNINGS = (
    "Warning: The maximum number of unified exec processes you can keep open is",
    "Warning: apply_patch was requested via ",
    "Warning: Your account was flagged for potentially high-risk cyber activity",
)
_MEDIA_LABEL = re.compile(r"</?(?:image|audio)>|<(?:image|audio) name=[^>]*>\Z")
_NOTIFICATION = ("<subagent_notification>", "</subagent_notification>")
_USER_KINDS = frozenset({"user.text", "user.image", "user.audio"})
_IGNORED_TOP = frozenset(
    {
        "turn_context",
        "world_state",
        "token_usage_record",
        "inter_agent_communication_metadata",
        "security_risk_score",
        "realtime_item",
    }
)
_IGNORED_ITEMS = frozenset({"reasoning"})


def _homes(root: str, listing: Listing) -> list[str]:
    homes: list[str] = []
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        entries = listing(directory)
        if entries is None:
            continue
        subdirs, _ = entries
        if any(name in SESSION_DIRS for name in subdirs):
            homes.append(directory)
            continue
        if depth < HOME_DEPTH:
            pending.extend((f"{directory}/{name}", depth + 1) for name in subdirs)
    return homes


def _rollouts(directory: str, listing: Listing, depth: int) -> list[str]:
    entries = listing(directory)
    if entries is None:
        return []
    subdirs, files = entries
    found = [f"{directory}/{name}" for name in files if name.startswith("rollout-")]
    if depth > 0:
        for name in subdirs:
            found.extend(_rollouts(f"{directory}/{name}", listing, depth - 1))
    return found


def discover(roots: tuple[Path, ...], listing: Listing) -> list[Found]:
    found: list[Found] = []
    for root in roots:
        for home in _homes(str(root), listing):
            for rank, name in enumerate(SESSION_DIRS):
                for path in _rollouts(f"{home}/{name}", listing, 3):
                    match = ROLLOUT.fullmatch(path.rsplit("/", 1)[1])
                    if match is None:
                        continue
                    thread, rollout_id, compressed = match.groups()
                    native = f"{thread}_{rollout_id}" if rollout_id else thread
                    found.append(
                        Found(native, thread, path, (1 if compressed else 0, rank))
                    )
    return found


def _first_line(path: Path) -> dict[str, Any] | None:
    records, _ = read_records(path, Cursor(), max_bytes=1)
    if not records:
        return None
    try:
        value = loads(records[0].data)
    except ValueError as error:
        raise Expectation(f"first record is not JSON: {error}") from error
    if not isinstance(value, dict):
        raise Expectation("first record is not an object")
    return value


def _classify(source: object) -> tuple[SessionClass, str | None]:
    if source in ("cli", "vscode"):
        return "interactive", None
    if source in ("exec", "mcp"):
        return "worker", None
    if isinstance(source, dict) and len(source) == 1:
        (key, value), = source.items()
        if key == "subagent":
            if value == "memory_consolidation":
                return "maintenance", None
            if isinstance(value, dict) and isinstance(value.get("thread_spawn"), dict):
                parent = value["thread_spawn"].get("parent_thread_id")
                return "subagent", parent if isinstance(parent, str) else None
            return "subagent", None
        if key == "internal":
            if value == "memory_consolidation":
                return "maintenance", None
            if value == "guardian":
                return "subagent", None
    return "unclassified", None


def _item_digest(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in ("id", "internal_chat_message_metadata_passthrough", "metadata")
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _response_items(path: Path) -> Any:
    """(record index, time, payload) for each response item in a rollout."""

    cursor = Cursor()
    while True:
        records, cursor = read_records(path, cursor)
        if not records:
            return
        for record in records:
            try:
                value = loads(record.data)
            except ValueError:
                continue
            if value.get("type") == "response_item" and isinstance(value.get("payload"), dict):
                yield record.index, normalize_time(value.get("timestamp"), EPOCH), value["payload"]


# Many subagents fork from one parent; keep the last few parents' digests.
_PARENT_DIGESTS: dict[tuple[str, int, int], list[tuple[str, str]]] = {}


def _parent_digests(parent: Path) -> list[tuple[str, str]]:
    info = parent.stat()
    key = (str(parent), info.st_size, info.st_mtime_ns)
    if key not in _PARENT_DIGESTS:
        if len(_PARENT_DIGESTS) >= 4:
            _PARENT_DIGESTS.pop(next(iter(_PARENT_DIGESTS)))
        _PARENT_DIGESTS[key] = [
            (time, _item_digest(payload)) for _, time, payload in _response_items(parent)
        ]
    return _PARENT_DIGESTS[key]


def _copied_prefix(path: Path, parent: Path, started: str) -> int:
    """Records before the first item that is not a copy of a parent item.

    Legacy forks copy the parent's items with no end marker, so the copy is
    the run of leading items whose content appears in the parent rollout up
    to the fork's start.
    """

    seen = {digest for time, digest in _parent_digests(parent) if time <= started}
    first_own = 1
    for index, _, payload in _response_items(path):
        if _item_digest(payload) not in seen:
            return index
        first_own = index + 1
    return first_own


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    first = _first_line(path)
    if first is None:
        return None
    match = ROLLOUT.fullmatch(path.name)
    if match is None:
        raise Expectation(f"not a rollout file name: {path.name}")
    thread = match.group(1)
    if first.get("type") != "session_meta":
        if "type" not in first and "id" in first and "instructions" in first:
            git = first.get("git") if isinstance(first.get("git"), dict) else {}
            return Head(
                session_id=thread,
                session_class="unclassified",
                started=normalize_time(first.get("timestamp"), EPOCH),
                origin=git.get("repository_url"),
                state={"legacy": True},
            )
        raise Expectation(f"unknown first record type {first.get('type')!r}")
    meta = first.get("payload")
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        raise Expectation("session_meta without an id")
    if meta["id"] != thread:
        raise Expectation("session_meta id does not match the file name")
    session_class, parent = _classify(meta.get("source", "vscode"))
    started = normalize_time(meta.get("timestamp") or first.get("timestamp"), EPOCH)
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    state: dict[str, Any] = {}
    skip = None
    if isinstance(meta.get("subagent_history_start_ordinal"), int):
        state["min_ordinal"] = meta["subagent_history_start_ordinal"]
    fork = meta.get("forked_from_id")
    if isinstance(fork, str) and meta.get("history_base") is None and "min_ordinal" not in state:
        parent_path = locate(fork)
        if parent_path is None:
            skip = "fork parent rollout missing; its copied prefix cannot be told apart"
        else:
            state["skip_before"] = _copied_prefix(path, parent_path, started)
    return Head(
        session_id=thread,
        session_class=session_class,
        started=started,
        parent=parent,
        cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
        origin=git.get("repository_url") if isinstance(git.get("repository_url"), str) else None,
        cli_version=meta.get("cli_version") if isinstance(meta.get("cli_version"), str) else None,
        skip=skip,
        state=state,
    )


def _is_fragment(text: str) -> bool:
    stripped = text.strip()
    for start, end, insensitive in _FRAGMENTS:
        head_part = stripped[: len(start)]
        if (head_part.lower() == start.lower() if insensitive else head_part == start) and (
            stripped[-len(end) :].lower() == end.lower()
        ):
            return True
    return bool(_EXTERNAL.match(stripped)) or stripped.startswith(_LEGACY_WARNINGS)


def _user_message(payload: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, text) for a user-role message, or None when it is injected."""

    content = payload.get("content")
    if not isinstance(content, list):
        raise Expectation("user message without a content list")
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(kinds, list) or len(kinds) != len(content):
        kinds = None
    pieces: list[str] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            raise Expectation("user content item is not an object")
        kind = kinds[index] if kinds else None
        if item.get("type") == "input_text":
            text = item.get("text", "")
            stripped = text.strip()
            if stripped.startswith(_NOTIFICATION[0]) and stripped.endswith(_NOTIFICATION[1]):
                return "work", stripped
            if kind is not None and kind not in _USER_KINDS:
                return None
            if kind is None and _is_fragment(text):
                return None
            if _MEDIA_LABEL.fullmatch(stripped):
                continue
            pieces.append(text)
        elif item.get("type") in ("input_image", "image"):
            pieces.append(IMAGE_OMITTED)
        elif item.get("type") == "input_audio":
            pieces.append("[audio omitted]")
        else:
            raise Expectation(f"unknown user content item {item.get('type')!r}")
    text = "\n".join(pieces).strip()
    if text.startswith("<hist>") and text.endswith("</hist>"):
        return None
    return ("user", text) if text else None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    pieces: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            pieces.append(str(item))
        elif item.get("type") in ("input_image", "image", "output_image"):
            pieces.append(IMAGE_OMITTED)
        elif isinstance(item.get("text"), str):
            pieces.append(item["text"])
        else:
            pieces.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(pieces)


def _communication(text: str) -> tuple[str, str] | None:
    """The older form of a subagent's completion: JSON in an assistant message."""

    if not text.startswith("{"):
        return None
    try:
        value = loads(text)
    except ValueError:
        return None
    if isinstance(value, dict) and {"author", "recipient", "content"} <= set(value):
        return str(value["author"]), _content_text(value["content"])
    return None


def _item(
    payload: dict[str, Any], state: dict[str, Any]
) -> list[tuple[str, str | None, str]]:
    """(kind, tool, text) messages for one response item."""

    item_type = payload.get("type")
    calls: dict[str, str] = state.setdefault("calls", {})
    if item_type == "message":
        role = payload.get("role")
        if role == "user":
            found = _user_message(payload)
            return [] if found is None else [(found[0], None, found[1])]
        if role == "assistant":
            text = _content_text(payload.get("content"))
            communication = _communication(text)
            if communication is not None:
                return [("work", communication[0], communication[1])]
            return [("agent", None, text)] if text.strip() else []
        if role in ("developer", "system"):
            return []
        raise Expectation(f"unknown message role {role!r}")
    if item_type == "agent_message":
        author = str(payload.get("author") or "agent")
        content = payload.get("content")
        if isinstance(content, dict) and "encrypted_content" in content:
            return [("work", author, "[encrypted]")]
        return [("work", author, _content_text(content))]
    if item_type in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "tool")
        if isinstance(payload.get("namespace"), str):
            name = f"{payload['namespace']}.{name}"
        call_id = payload.get("call_id")
        if isinstance(call_id, str):
            calls[call_id] = name
            if len(calls) > 256:
                calls.pop(next(iter(calls)))
        body = payload.get("arguments" if item_type == "function_call" else "input")
        return [("tool_call", name, body if isinstance(body, str) else json.dumps(body))]
    if item_type == "local_shell_call":
        call_id = payload.get("call_id")
        if isinstance(call_id, str):
            calls[call_id] = "shell"
        return [("tool_call", "shell", json.dumps(payload.get("action"), ensure_ascii=False))]
    if item_type in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        call_id = payload.get("call_id")
        name = calls.pop(call_id, None) if isinstance(call_id, str) else None
        return [("tool_result", name, _content_text(payload.get("output")))]
    if item_type == "web_search_call":
        return [("tool_call", "web_search", json.dumps(payload.get("action"), ensure_ascii=False))]
    if item_type == "tool_search_call":
        return [("tool_call", "tool_search", json.dumps(payload.get("arguments"), ensure_ascii=False))]
    if item_type == "tool_search_output":
        tools = payload.get("tools")
        names = [t.get("name") for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
        return [("tool_result", "tool_search", ", ".join(str(n) for n in names))]
    if item_type == "image_generation_call":
        prompt = payload.get("revised_prompt") or ""
        return [("tool_call", "image_generation", f"{prompt} {IMAGE_OMITTED}".strip())]
    if item_type in _IGNORED_ITEMS:
        return []
    count_unknown(state, f"response_item:{item_type}")
    return []


def map_records(
    records: list[Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    for record in records:
        if record.index < state.get("skip_before", 0):
            continue
        try:
            value = loads(record.data)
        except ValueError as error:
            raise Expectation(f"record {record.index} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise Expectation(f"record {record.index} is not an object")
        if isinstance(value.get("ordinal"), int) and value["ordinal"] < state.get("min_ordinal", 0):
            continue
        time = normalize_time(value.get("timestamp"), state.get("time", EPOCH))
        state["time"] = time
        turn = state.get("turn", time)
        found: list[tuple[str, str | None, str]] = []
        record_type = value.get("type")
        if state.get("legacy"):
            if record.index == 0 or "record_type" in value:
                continue
            found = _item(value, state)
        elif record_type == "response_item":
            if not isinstance(value.get("payload"), dict):
                raise Expectation(f"record {record.index}: response_item without payload")
            found = _item(value["payload"], state)
        elif record_type == "compacted":
            found = [("event", None, "context compacted")]
        elif record_type == "event_msg":
            payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
            if payload.get("type") == "thread_rolled_back":
                found = [("event", None, f"rolled back {payload.get('num_turns')} turn(s)")]
        elif record_type == "inter_agent_communication":
            payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
            found = [("work", str(payload.get("author") or "agent"), _content_text(payload.get("content")))]
        elif record_type == "session_meta" or record_type in _IGNORED_TOP:
            continue
        else:
            count_unknown(state, f"type:{record_type}")
        part = 0
        for kind, tool, text in found:
            if kind == "user":
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, kind, tool, text, None)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


def hook_input(payload: dict[str, Any], event: str) -> HookInput:
    transcript = payload.get("transcript_path")
    path = Path(transcript) if isinstance(transcript, str) and transcript else None
    session = payload.get("session_id")
    if event == "SubagentStart" and isinstance(payload.get("agent_id"), str):
        session = payload["agent_id"]
    elif path is not None and (match := ROLLOUT.fullmatch(path.name)):
        session = match.group(1)
    if not isinstance(session, str) or not session:
        raise ValueError("hook input has no session id")
    cwd = payload.get("cwd")
    return HookInput(
        session_id=session,
        transcript=path,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
        start=str(payload.get("source") or "startup").lower(),
        subagent=event == "SubagentStart",
    )


class _Codex:
    name = NAME
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)
    hook_input = staticmethod(hook_input)


ADAPTER = _Codex()
