"""Kimi Code sessions: `<home>/sessions/wd_<dir>/session_<uuid>/agents/<agent>/wire.jsonl`.

A home is a directory holding `sessions`, found up to four levels below a
configured root: `~/.kimi-code`, and the per-repository homes the launchers
make under `~/.kimi-code-state`. Each agent's journal is one source. `main`
is the session, `session_<uuid>` as Kimi's hooks name it; every other agent
is a subagent, `session_<uuid>/<agent>`, whose parent `state.json` names.

Content comes from `turn.prompt`, `turn.steer`, and loop events. The
`agent.message.appended` and `context.append_message` records repeat them
or hold the host's own context. Kimi's own rules mark injected prompt parts:
a `meta.source`, or the skill bodies an `origin.skillActivations` stands for.

A main session is interactive when one of its prompts is a line of the
home's typed-prompt history, or when it has a title and is not a fork; a
worker when it has neither and its first turn has ended or it has been idle
an hour. Until then it waits. A fork's journals start with copies of the
source's records, up to the last `forked` record; those are skipped, and
until Kimi writes the fork's `state.json`, after those records, it waits. Output
too large for the journal is saved under the agent's `tool-results/` or
`tasks/`, and `complete` reads it.

Kimi rewrites a journal in place when it loads one of an older protocol,
which stops that source: what was indexed stays, and the session's later
messages are not indexed. A protocol other than 1.4 or 1.5 stops a source
with that reason.
"""

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import reader
from ..reader import Cursor, Record, read as read_records
from . import (
    IMAGE_OMITTED,
    Expectation,
    Found,
    Head,
    HookInput,
    Listing,
    Message,
    count_unknown,
    loads,
    parts,
    read_saved,
    utc_time,
)

NAME = "kimi"
EPOCH = "1970-01-01T00:00:00.000Z"
HOME_DEPTH = 4
HEAD_BYTES = 1024 * 1024
IDLE_MS = 3600 * 1000
PROTOCOLS = frozenset({"1.4", "1.5"})
_SESSION = re.compile(r"session_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Lines the head parses; the rest it only counts past.
_HEAD_MARKS = (b'"turn.prompt"', b'"turn.ended"', b'"prompt.completed"', b'"forked"')
# Record types that hold no content, or content another record also holds.
_IGNORED = frozenset(
    {
        "metadata", "config.update", "tools.set_active_tools", "tools.update_store",
        "llm.tools_snapshot", "llm.request", "usage.record", "permission.set_mode",
        "permission.record_approval_result", "goal.create", "goal.update", "plan_mode.enter",
        "plan_mode.exit", "swarm_mode.enter", "swarm_mode.exit", "turn.cancel",
        "runtime.set_binding", "environment.set_binding", "profile.bind",
        "agent.message.appended", "agent.turn.started", "agent.turn.ended",
        "context.append_message", "context.apply_compaction", "full_compaction.begin",
        "plugin.session_start", "mcp.tools_discovered", "token_counting.measured",
        "token_counting.turn_recorded", "token_counting.rebased", "prompt.accepted",
        "prompt.steered", "prompt.aborted", "prompt.completed", "changeNotifier.snapshot",
        "file_history.tracked", "file_history.checkpoint", "staleGuard.recorded",
        "subagent.spawned", "subagent.started", "subagent.completed", "subagent.failed",
        "subagent.cancelled", "task.started", "task.terminated", "task.waitDelivered",
        "tool_select.initialized", "tool_select.tools_changed", "interaction.request",
        "interaction.resolved", "interruptionReminder.requested", "interruptionReminder.recorded",
        "cron.add", "turn.step.interrupted", "turn.step.retrying", "forked",
    }
)
_IGNORED_LOOP = frozenset({"step.begin"})
# Prompt origins besides the user's own, and the kind each is indexed as.
_ORIGINS = {"system_trigger": "event", "task": "event", "background_task": "event"}
_MEDIA = {"image_url": IMAGE_OMITTED, "audio_url": "[audio omitted]", "video_url": "[video omitted]"}
_ENDS = frozenset({"completed", "failed", "cancelled"})
# The forms of a tool result whose output Kimi saved whole (renderPersistedToolResult
# and renderAppendedSpillPointer): a header in place of the output, or a pointer
# after its cut lines, then perhaps a tool's own lines, such as a task's id.
_SAVED_HEADER = re.compile(
    r"Tool output exceeded [^\n]*\ntool_name: [^\n]*\ntool_call_id: [^\n]*\noutput_size_chars: [^\n]*\n"
    r"(?:output_size_bytes: [^\n]*\n)?output_path: ([^\n]+)\nnext_step: "
)
_SPILL_MARK = "[Per-line truncation occurred;"
_SPILL = re.compile(
    re.escape(_SPILL_MARK) + r"[^\n]*\noutput_path: ([^\n]+)\nnext_step: [^\n]*\](?:\n\n[^\n]+(?:\n[^\n]+)*)?\Z"
)


def _name(value: object) -> str | None:
    """A record's type or kind as a lookup key; malformed values become text."""

    return value if value is None or isinstance(value, str) else json.dumps(value)


def _homes(root: str, listing: Listing) -> list[str]:
    homes: list[str] = []
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        entries = listing(directory)
        if entries is None:
            continue
        if "sessions" in entries[0]:
            homes.append(directory)
            continue
        if depth < HOME_DEPTH:
            pending.extend((f"{directory}/{name}", depth + 1) for name in entries[0])
    return homes


def discover(roots: tuple[Path, ...], listing: Listing) -> list[Found]:
    found: list[Found] = []
    for root in roots:
        for home in _homes(str(root), listing):
            sessions = f"{home}/sessions"
            workdirs = listing(sessions)
            for workdir in workdirs[0] if workdirs else []:
                listed = listing(f"{sessions}/{workdir}")
                for session in listed[0] if listed else []:
                    if not _SESSION.fullmatch(session):
                        continue
                    agents_dir = f"{sessions}/{workdir}/{session}/agents"
                    agents = listing(agents_dir)
                    for agent in agents[0] if agents else []:
                        native = session if agent == "main" else f"{session}/{agent}"
                        found.append(Found(native, native, f"{agents_dir}/{agent}/wire.jsonl"))
    return found


def _milliseconds(value: object) -> int | None:
    """Epoch milliseconds from `state.json`: a number, or an ISO string in versionless files."""

    if isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _utc(milliseconds: int, what: str) -> str:
    """utc_time, or Expectation for a time no calendar holds."""

    try:
        return utc_time(milliseconds)
    except (OverflowError, ValueError, OSError) as error:
        raise Expectation(f"{what} {milliseconds} is out of range") from error


def _state(session_dir: Path) -> dict[str, Any] | None:
    try:
        value = loads((session_dir / "state.json").read_bytes())
    except FileNotFoundError:
        return None
    except ValueError:
        # Mid-write, or not Kimi's; it is read again at the next sync.
        return None
    return value if isinstance(value, dict) else None


def _parent_agent(meta: dict[str, Any]) -> str | None:
    """Kimi's `subagentParentAgentId`: the labels' parent, else the legacy field."""

    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    for value in (labels.get("parentAgentId"), meta.get("parentAgentId")):
        if isinstance(value, str) and value:
            return value
    return None


def _input_text(content: object, origin: dict[str, Any], state: dict[str, Any] | None) -> str:
    """What the user wrote in a prompt or steer: Kimi's `turnPromptText`, with media named.

    A part with a `meta.source` is the host's (a hook's output, a loaded
    skill). Older prompts carry bundled skill bodies untagged: the first
    untagged text parts, one per entry of `origin.skillActivations`.
    """

    if not isinstance(content, list):
        raise Expectation("prompt input is not a list")
    activations = origin.get("skillActivations") if origin.get("kind") == "user" else None
    bundled = len(activations) if isinstance(activations, list) else 0
    if any(isinstance(p, dict) and _source(p) == "skill activation" for p in content):
        bundled = 0
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise Expectation("prompt part is not an object")
        kind = _name(part.get("type"))
        if kind == "text" and _source(part) is None and bundled:
            bundled -= 1
            continue
        if _source(part) is not None:
            continue
        if kind == "text":
            pieces.append(str(part.get("text", "")))
        elif kind in _MEDIA:
            pieces.append(_MEDIA[kind])
        elif state is not None:
            count_unknown(state, f"input_part:{kind}")
    return "".join(pieces).strip()


def _source(part: dict[str, Any]) -> str | None:
    meta = part.get("meta")
    source = meta.get("source") if isinstance(meta, dict) else None
    return source if isinstance(source, str) else None


def _slash(origin: dict[str, Any]) -> str | None:
    """A slash-activated skill as typed: `/skill:<name> <args>`, from the origin's fields."""

    name = origin.get("skillName")
    if not isinstance(name, str) or not name:
        return None
    args = origin.get("skillArgs")
    args = args.strip() if isinstance(args, str) else ""
    return f"/skill:{name} {args}" if args else f"/skill:{name}"


def _typed_prompt(value: dict[str, Any]) -> str | None:
    """A `turn.prompt` record's text when a person could have typed it."""

    origin = value.get("origin") if isinstance(value.get("origin"), dict) else {}
    if origin.get("kind") == "user":
        return _input_text(value.get("input"), origin, None) or None
    if origin.get("kind") == "skill_activation" and origin.get("trigger") == "user-slash":
        return _slash(origin)
    return None


def _typed_key(text: str) -> str:
    """A prompt as matched against the typed history. A slash command is keyed
    as Kimi parses it (parseSlashInput): the name up to the first space, in
    lower case and without `skill:`, then the trimmed arguments, since Kimi
    runs `/Audit x`, `/audit  x`, and `/skill:audit x` as one skill."""

    text = text.strip()
    if not text.startswith("/"):
        return text
    name, _, args = text[1:].strip().partition(" ")
    if "/" in name and ":" not in name:
        return text  # a path, which Kimi sends as a message
    name = name.lower().removeprefix("skill:")
    return f"/{name} {args.strip()}".rstrip()


def _typed_history(home: Path, cwd: str) -> set[str]:
    """Prompts typed into the TUI in `cwd`: `<home>/user-history/<md5(cwd)>.jsonl`."""

    name = hashlib.md5(cwd.encode("utf-8")).hexdigest()
    try:
        lines = (home / "user-history" / f"{name}.jsonl").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return set()
    typed: set[str] = set()
    for line in lines.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            typed.add(_typed_key(entry["content"]))
    return typed


def _scan(path: Path, fork: bool) -> tuple[list[tuple[int, dict[str, Any]]], int | None]:
    """The head's records of interest, after the fork boundary, and that boundary.

    A fork's journal is read whole for its last `forked` record; any other,
    only its first HEAD_BYTES.
    """

    found: list[tuple[int, dict[str, Any]]] = []
    boundary = None
    cursor, taken = Cursor(), 0
    while fork or taken < HEAD_BYTES:
        records, cursor = read_records(path, cursor, max_bytes=HEAD_BYTES)
        if not records:
            break
        for record in records:
            taken += len(record.data) + 1
            if not any(mark in record.data for mark in _HEAD_MARKS):
                continue
            try:
                value = loads(record.data)
            except ValueError as error:
                raise Expectation(f"record {record.index} is not JSON: {error}") from error
            if not isinstance(value, dict):
                raise Expectation(f"record {record.index} is not an object")
            if value.get("type") == "forked":
                boundary, found = record.index, []
            else:
                found.append((record.index, value))
    return found, boundary


def head(path: Path, locate: Callable[[str], Path | None]) -> Head | None:
    del locate
    agent_dir = path.parent
    session_dir = agent_dir.parent.parent
    session, agent = session_dir.name, agent_dir.name
    first, _ = read_records(path, Cursor(), max_bytes=1)
    if not first:
        return None
    try:
        metadata = loads(first[0].data)
    except ValueError as error:
        raise Expectation(f"first record is not JSON: {error}") from error
    if not isinstance(metadata, dict) or metadata.get("type") != "metadata":
        raise Expectation("first record is not the journal's metadata")
    protocol = metadata.get("protocol_version")
    if not isinstance(protocol, str) or protocol not in PROTOCOLS:
        raise Expectation(f"unsupported protocol_version {protocol!r}")
    state = _state(session_dir)
    if state is None:
        return None
    agents = state.get("agents")
    meta = agents.get(agent) if isinstance(agents, dict) else None
    if not isinstance(meta, dict):
        return None
    cwd = state.get("cwd") or state.get("workDir")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    created = _milliseconds(state.get("createdAt"))
    begun = metadata.get("created_at")
    if isinstance(begun, int) and not isinstance(begun, bool):
        created = max(created or 0, begun)
    fork = state.get("forkedFrom")
    fork = fork if isinstance(fork, str) and fork else None
    records, boundary = _scan(path, fork is not None)
    head_state: dict[str, Any] = {}
    if fork is not None and boundary is not None:
        head_state = {"skip_through": boundary, "forked_from": fork}
    native = session if agent == "main" else f"{session}/{agent}"
    common = {
        "started": _utc(created, "createdAt") if created is not None else EPOCH,
        "cwd": cwd,
        "cli_version": protocol,
        "state": head_state,
    }
    if agent != "main":
        parent_agent = _parent_agent(meta)
        parent = None
        if parent_agent is not None:
            parent = session if parent_agent == "main" else f"{session}/{parent_agent}"
        return Head(session_id=native, session_class="subagent", parent=parent, **common)
    if fork is not None and boundary is None:
        # Kimi writes a fork's state.json after its markers; wait for them.
        return None
    prompts = [text for _, value in records if value.get("type") == "turn.prompt"
               and (text := _typed_prompt(value)) is not None]
    if prompts and cwd is not None:
        typed = _typed_history(session_dir.parents[2], cwd)
        if any(_typed_key(prompt) in typed for prompt in prompts):
            return Head(session_id=session, session_class="interactive", **common)
    title = state.get("title")
    if fork is None and isinstance(title, str) and title.strip():
        return Head(session_id=session, session_class="interactive", **common)
    updated = max(_milliseconds(state.get("updatedAt")) or 0, int(os.stat(path).st_mtime * 1000))
    idle = time.time() * 1000 - updated > IDLE_MS
    if fork is not None:
        return Head(session_id=session, session_class="unclassified", **common) if idle else None
    # A first turn too long for the prefix still leaves its end in state.json.
    ended = _name(state.get("lastTurnReason")) in _ENDS or any(
        value.get("type") in ("turn.ended", "prompt.completed") for _, value in records)
    if ended or idle:
        return Head(session_id=session, session_class="worker", **common)
    return None


def _result_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return json.dumps(output, ensure_ascii=False)
    pieces: list[str] = []
    for part in output:
        kind = _name(part.get("type")) if isinstance(part, dict) else None
        if kind == "text":
            pieces.append(str(part.get("text", "")))
        elif kind in _MEDIA:
            pieces.append(_MEDIA[kind])
        else:
            pieces.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(pieces)


def _key(prefix: str, value: object) -> str | None:
    return f"kimi:{prefix}:{value}" if isinstance(value, str) and value else None


def _prompt(value: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str, str | None]]:
    origin = value.get("origin") if isinstance(value.get("origin"), dict) else {}
    kind = _name(origin.get("kind"))
    steer = value.get("type") == "turn.steer"
    key = _key("steer", value.get("messageId")) if steer else _key("prompt", value.get("promptId"))
    if kind == "user":
        text = _input_text(value.get("input"), origin, state)
        return [("user", None, text, key)] if text else []
    if kind == "skill_activation":
        # Kimi's own rule for input a person sees as theirs (isVisibleWireTurnInput).
        if origin.get("trigger") != "user-slash":
            return []
        command = _slash(origin)
        if command is None:
            count_unknown(state, "skill_activation without a skill name")
            return []
        return [("user", None, command, key)]
    if kind not in _ORIGINS:
        count_unknown(state, f"origin:{kind}")
        return []
    text = _input_text(value.get("input"), {}, state)
    if not text:
        return []
    if kind == "system_trigger" and origin.get("name") == "subagent":
        return [("work", "subagent", text, key)]
    return [(_ORIGINS[kind], str(kind), text, key)]


def _loop(event: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str, str | None]]:
    calls: dict[str, str] = state.setdefault("calls", {})
    kind = _name(event.get("type"))
    if kind == "content.part":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_kind = _name(part.get("type"))
        if part_kind == "text":
            text = str(part.get("text", ""))
            return [("agent", None, text, _key("part", event.get("uuid")))] if text.strip() else []
        if part_kind != "think":
            count_unknown(state, f"content_part:{part_kind}")
        return []
    if kind == "tool.call":
        name = str(event.get("name") or "tool")
        call = event.get("toolCallId")
        if isinstance(call, str):
            calls[call] = name
            if len(calls) > 256:
                calls.pop(next(iter(calls)))
        return [("tool_call", name, json.dumps(event.get("args"), ensure_ascii=False), _key("call", call))]
    if kind == "tool.result":
        call = event.get("toolCallId")
        name = calls.pop(call, None) if isinstance(call, str) else None
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        return [("tool_result", name, _result_text(result.get("output")), _key("result", call))]
    if kind == "step.end":
        if event.get("finishReason") == "error":
            return [("event", None, "the model step failed", None)]
        return []
    if kind not in _IGNORED_LOOP:
        count_unknown(state, f"loop:{kind}")
    return []


def _record(value: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str | None, str, str | None]]:
    record_type = _name(value.get("type"))
    if record_type in ("turn.prompt", "turn.steer"):
        return _prompt(value, state)
    if record_type == "context.append_loop_event":
        event = value.get("event")
        if not isinstance(event, dict):
            raise Expectation("loop event record without an event")
        return _loop(event, state)
    if record_type == "turn.ended":
        reason = value.get("reason")
        if reason == "failed":
            error = value.get("error") if isinstance(value.get("error"), dict) else {}
            return [("event", None, f"turn failed: {error.get('message') or error.get('code') or 'error'}", None)]
        if reason == "cancelled":
            return [("event", None, "turn cancelled", None)]
        return []
    if record_type == "full_compaction.complete":
        return [("event", None, "context compacted", None)]
    if record_type not in _IGNORED:
        count_unknown(state, f"type:{record_type}")
    return []


def map_records(
    records: list[Record], state: dict[str, Any]
) -> tuple[list[Message], dict[str, Any]]:
    state = json.loads(json.dumps(state))
    messages: list[Message] = []
    skip_through = state.get("skip_through", -1)
    for record in records:
        if record.index < skip_through:
            continue
        try:
            value = loads(record.data)
        except ValueError as error:
            raise Expectation(f"record {record.index} is not JSON: {error}") from error
        if not isinstance(value, dict):
            raise Expectation(f"record {record.index} is not an object")
        stamp = value.get("time")
        if isinstance(stamp, int) and not isinstance(stamp, bool):
            state["time"] = _utc(stamp, f"record {record.index} time")
        time_ = state.get("time", EPOCH)
        turn = state.get("turn", time_)
        if record.index == skip_through:
            found = [("event", None, f"forked from {state.get('forked_from')}", None)]
        else:
            found = _record(value, state)
        part = 0
        for kind, tool, text, key in found:
            if kind == "user":
                turn = state["turn"] = time_
            produced = parts(record.index, part, time_, turn, kind, tool, text, key)  # type: ignore[arg-type]
            messages.extend(produced)
            part += len(produced)
    return messages, state


def complete(transcript: Path, text: str) -> str | None:
    """The whole output a "Tool output exceeded" notice, or an appended
    per-line truncation pointer, stands for, clipped as any tool output is, or
    None. Only a regular file under the agent's own `tool-results/` or
    `tasks/` is read. Kimi writes either form once a call's output is whole;
    a background task's growing log is named only by a TaskOutput record."""

    # Kimi's pointer is the last in the text and starts a line; an output
    # that only quotes a header still ends with its own pointer.
    start = text.rfind(_SPILL_MARK)
    match = None
    if start != -1 and (start == 0 or text[start - 1] == "\n"):
        match = _SPILL.match(text, start)
    match = match or _SAVED_HEADER.match(text)
    if match is None:
        return None
    saved = os.path.realpath(match.group(1).strip())
    agent = os.path.realpath(transcript.parent)
    if not any(saved.startswith(f"{agent}/{name}/") for name in ("tool-results", "tasks")):
        return None
    return read_saved(saved)


def hook_input(payload: dict[str, Any], event: str) -> HookInput:
    del event
    session = payload.get("session_id")
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise ValueError("hook input has no Kimi session id")
    cwd = payload.get("cwd")
    return HookInput(
        session_id=session,
        transcript=None,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
        start="startup",
        subagent=False,
    )


class _Kimi:
    name = NAME
    reader = reader
    discover = staticmethod(discover)
    head = staticmethod(head)
    map = staticmethod(map_records)
    complete = staticmethod(complete)
    hook_input = staticmethod(hook_input)


ADAPTER = _Kimi()
