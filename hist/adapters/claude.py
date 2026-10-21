"""Claude Code transcripts under `~/.claude/projects/<project>/`.

`<session>.jsonl` is a session; `<session>/subagents/[workflows/wf_<id>/]
agent-<id>.jsonl` is one of its subagents. Forks copy records into a new
file under the same `uuid`s and a few files re-append earlier blocks, so a
record's `uuid` is its key across files and the first occurrence wins.
`attachment` records, which also carry hook context, are dropped, except
queued commands: what the user typed while the agent worked, and task
notifications that arrived then. A large tool output is recorded as a
2 KB preview naming the file under `<session>/tool-results/` that holds it
whole; `complete` reads that file.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable

from .. import reader
from ..reader import Cursor, Record, read as read_records
from . import (
    IMAGE_OMITTED,
    TOOL_OUTPUT_CHARS,
    Expectation,
    Found,
    Head,
    HookInput,
    Listing,
    Message,
    clip_output,
    count_unknown,
    loads,
    normalize_time,
    parts,
)

NAME = "claude"
EPOCH = "1970-01-01T00:00:00.000Z"
HEAD_BYTES = 1024 * 1024
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_AGENT = re.compile(r"agent-([A-Za-z0-9_-]+)\.jsonl")
_INTERACTIVE = frozenset({"cli", "claude-desktop"})
_WORKER = frozenset({"sdk-cli"})
_METADATA = frozenset(
    {
        "last-prompt",
        "permission-mode",
        "mode",
        "queue-operation",
        "ai-title",
        "atis-latch",
        "file-history-snapshot",
        "file-history-delta",
        "cost-state",
        "custom-title",
        "agent-name",
        "pr-link",
        "agent-color",
        "summary",
        "fork-context-ref",
        "started",
        "result",
        "launched",
        "progress",
    }
)
# Leading markers of user-role strings: dropped as injected, or re-kinded.
_DROPPED = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<fork-boilerplate>",
)
_TOOL_OUTPUT = ("<bash-stdout>", "<bash-stderr>")
_INTERRUPTED = "[Request interrupted by user"
_NOTIFICATION = "<task-notification>"
_SAVED = re.compile(r"<persisted-output>\n[^\n]*Full output saved to: ([^\n]+)\n")
# Larger saved outputs are read as a head and a tail, not whole.
SAVED_READ_BYTES = 16 * 1024 * 1024


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
            subdirs, files = entries
            for name in files:
                stem = name[: -len(".jsonl")]
                if name.endswith(".jsonl") and _SESSION.fullmatch(stem):
                    found.append(Found(stem, stem, f"{root}/{project}/{name}"))
            for session in subdirs:
                if _SESSION.fullmatch(session):
                    found.extend(_subagents(f"{root}/{project}/{session}/subagents", listing, 2))
    return found


def _subagents(directory: str, listing: Listing, depth: int) -> list[Found]:
    entries = listing(directory)
    if entries is None:
        return []
    subdirs, files = entries
    found = []
    for name in files:
        match = _AGENT.fullmatch(name)
        if match:
            found.append(Found(match.group(1), match.group(1), f"{directory}/{name}"))
    if depth > 0:
        for name in subdirs:
            if name == "workflows" or name.startswith("wf_"):
                found.extend(_subagents(f"{directory}/{name}", listing, depth - 1))
    return found


def _is_subagent(path: Path) -> bool:
    return "subagents" in path.parts[-4:-1]


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    records, _ = read_records(path, Cursor(), max_bytes=HEAD_BYTES)
    started = None
    for record in records:
        try:
            value = loads(record.data)
        except ValueError as error:
            raise Expectation(f"record {record.index} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise Expectation(f"record {record.index} is not an object")
        if started is None and isinstance(value.get("timestamp"), str):
            started = normalize_time(value["timestamp"], EPOCH)
        if "uuid" not in value or not isinstance(value.get("cwd"), str):
            continue
        session = value.get("sessionId")
        if not isinstance(session, str):
            raise Expectation("message record without a sessionId")
        if _is_subagent(path):
            native = path.name[len("agent-") : -len(".jsonl")]
            return Head(
                session_id=native,
                session_class="subagent",
                started=started or EPOCH,
                parent=session,
                cwd=value["cwd"],
                cli_version=value.get("version"),
            )
        if session != path.stem:
            raise Expectation("sessionId does not match the file name")
        entrypoint = value.get("entrypoint")
        session_class = (
            "interactive"
            if entrypoint in _INTERACTIVE
            else "worker"
            if entrypoint in _WORKER
            else "unclassified"
        )
        return Head(
            session_id=session,
            session_class=session_class,
            started=started or EPOCH,
            cwd=value["cwd"],
            cli_version=value.get("version"),
        )
    return None


def _user_text(text: str) -> tuple[str, str | None] | None:
    """(kind, tool) for a user-role string, or None when it is injected."""

    stripped = text.lstrip()
    if stripped.startswith(_DROPPED):
        return None
    if stripped.startswith("<hist>") and stripped.rstrip().endswith("</hist>"):
        return None
    if stripped.startswith(_NOTIFICATION):
        return "work", "task"
    if stripped.startswith(_TOOL_OUTPUT):
        return "tool_result", "bash"
    if stripped.startswith(_INTERRUPTED):
        return "event", None
    return "user", None


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    pieces = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            pieces.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "image":
            pieces.append(IMAGE_OMITTED)
        elif isinstance(item, dict) and item.get("type") == "document":
            pieces.append("[document omitted]")
        else:
            pieces.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(pieces)


def _user(value: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    if value.get("isMeta") or value.get("isCompactSummary") or value.get("isVisibleInTranscriptOnly"):
        return []
    message = value.get("message")
    if not isinstance(message, dict):
        raise Expectation("user record without a message")
    content = message.get("content")
    if isinstance(content, str):
        found = _user_text(content)
        return [] if found is None else [(found[0], found[1], content)]
    if not isinstance(content, list):
        raise Expectation("user content is neither text nor a list")
    tools: dict[str, str] = state.setdefault("tools", {})
    result = value.get("toolUseResult")
    agent = result.get("agentId") if isinstance(result, dict) else None
    out: list[tuple[str, str | None, str]] = []
    typed: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            raise Expectation("user content item is not an object")
        item_type = item.get("type")
        if item_type == "tool_result":
            name = tools.pop(str(item.get("tool_use_id")), None)
            text = _result_text(item.get("content"))
            if isinstance(agent, str):
                out.append(("work", f"agent-{agent}", text))
            else:
                out.append(("tool_result", name, text))
        elif item_type == "text":
            typed.append(str(item.get("text", "")))
        elif item_type == "image":
            typed.append(IMAGE_OMITTED)
        elif item_type == "document":
            typed.append("[document omitted]")
        else:
            raise Expectation(f"unknown user content item {item_type!r}")
    if typed:
        text = "\n".join(typed)
        found = _user_text(text)
        if found is not None:
            out.append((found[0], found[1], text))
    return out


def _attachment(value: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    attachment = value.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return []
    mode, prompt = attachment.get("commandMode"), attachment.get("prompt")
    origin = attachment.get("origin")
    if not isinstance(prompt, str) or mode not in ("prompt", "task-notification"):
        count_unknown(state, f"queued_command:{mode}")
        return []
    if mode == "task-notification":
        return [("work", "task", prompt)]
    # A prompt another session sent, not one the user typed.
    if isinstance(origin, dict) and origin.get("kind") == "peer":
        return [("work", "peer", prompt)]
    found = _user_text(prompt)
    return [] if found is None else [(found[0], found[1], prompt)]


def _assistant(value: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    message = value.get("message")
    if not isinstance(message, dict):
        raise Expectation("assistant record without a message")
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise Expectation("assistant content is neither text nor a list")
    tools: dict[str, str] = state.setdefault("tools", {})
    out: list[tuple[str, str | None, str]] = []
    for block in content:
        if not isinstance(block, dict):
            raise Expectation("assistant block is not an object")
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text", ""))
            if value.get("isApiErrorMessage"):
                out.append(("event", None, text))
            elif text.strip():
                out.append(("agent", None, text))
        elif block_type == "tool_use":
            name = str(block.get("name") or "tool")
            tools[str(block.get("id"))] = name
            if len(tools) > 256:
                tools.pop(next(iter(tools)))
            out.append(("tool_call", name, json.dumps(block.get("input"), ensure_ascii=False)))
        # Text-free: reasoning, or a notice that the host switched models.
        elif block_type in ("thinking", "redacted_thinking", "fallback"):
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
        try:
            value = loads(record.data)
        except ValueError as error:
            raise Expectation(f"record {record.index} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise Expectation(f"record {record.index} is not an object")
        record_type = value.get("type")
        time = normalize_time(value.get("timestamp"), state.get("time", EPOCH))
        if "timestamp" in value:
            state["time"] = time
        found: list[tuple[str, str | None, str]] = []
        if record_type == "user":
            found = _user(value, state)
        elif record_type == "assistant":
            found = _assistant(value, state)
        elif record_type == "attachment":
            found = _attachment(value, state)
        elif record_type == "system":
            if value.get("subtype") == "compact_boundary":
                found = [("event", None, "context compacted")]
        elif record_type in _METADATA:
            continue
        else:
            count_unknown(state, f"type:{record_type}")
        uuid = value.get("uuid")
        key = f"claude:{uuid}" if isinstance(uuid, str) else None
        turn = state.get("turn", time)
        part = 0
        for kind, tool, text in found:
            if kind == "user":
                turn = state["turn"] = time
            produced = parts(record.index, part, time, turn, kind, tool, text, key)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


def _session_directory(transcript: Path) -> Path:
    if _is_subagent(transcript):
        return next(parent.parent for parent in transcript.parents if parent.name == "subagents")
    return transcript.parent / transcript.stem


def _read_saved(path: str) -> str | None:
    try:
        # Non-blocking, so that a pipe planted there cannot stall the sync.
        handle = open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    except OSError:
        return None
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        size = info.st_size
        if size <= SAVED_READ_BYTES:
            return clip_output(handle.read().decode("utf-8", "replace"))
        half = TOOL_OUTPUT_CHARS // 2
        head = handle.read(4 * half).decode("utf-8", "replace")[:half]
        handle.seek(-4 * half, os.SEEK_END)
        tail = handle.read().decode("utf-8", "replace")[-half:]
    dropped = size - len(head.encode()) - len(tail.encode())
    return f"{head}\n[... {dropped} bytes clipped ...]\n{tail}"


def complete(transcript: Path, text: str) -> str | None:
    """The whole output a saved-output preview stands for, clipped as any
    tool output is, or None. Only a regular file in the transcript's own
    session directory is read."""

    match = _SAVED.match(text)
    if match is None:
        return None
    allowed = os.path.realpath(_session_directory(transcript) / "tool-results")
    if not os.path.realpath(match.group(1).strip()).startswith(allowed + os.sep):
        return None
    return _read_saved(match.group(1).strip())


def hook_input(payload: dict[str, Any], event: str) -> HookInput:
    session = payload.get("session_id")
    subagent = event == "SubagentStart"
    if subagent:
        session = payload.get("agent_id")
    if not isinstance(session, str) or not session:
        raise ValueError("hook input has no session id")
    transcript = payload.get("transcript_path")
    cwd = payload.get("cwd")
    return HookInput(
        session_id=session,
        transcript=Path(transcript) if isinstance(transcript, str) and transcript and not subagent else None,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
        start=str(payload.get("source") or "startup").lower(),
        subagent=subagent,
    )


class _Claude:
    name = NAME
    reader = reader
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)
    complete = staticmethod(complete)
    hook_input = staticmethod(hook_input)


ADAPTER = _Claude()
