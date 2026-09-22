"""Fail-open hook edge for hosts with a known prompt-hook wire format.

The host table in `hosts.py` names each supported host, its codec, and its
compaction event; one host-neutral context builder produces the text. This
module owns input decoding, event mapping, output encoding, bounded warnings,
and the total passive-provider exception boundary: every handled failure
exits 0, never exit code 2, so a hook can never block an agent turn.
"""

import json
import sys
import time
from pathlib import Path
from typing import Sequence

from .config import parse_host
from .hook_context import (
    CONTEXT_LIMIT_BYTES,
    ORIENTATION_DEADLINE_SECONDS,
    CheckpointError,
    EventKind,
    build_hook_context,
    native_memory_cwd,
)
from .hosts import HOOK_HOSTS, HookHost
from .layout import resolve_store_context

_MALFORMED_HOOK_WARNING = (
    "omem {name} received malformed input; {agent} continues without "
    "a memory checkpoint."
)
_CHECKPOINT_HOOK_WARNING = (
    "omem {name} could not load one canonical checkpoint; {agent} continues "
    "without it."
)
_UNSUPPORTED_HOST_WARNING = (
    "omem hook has no prompt-hook format for host {host!r}; the agent "
    "continues without a memory checkpoint."
)


def _write_hook_json(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _json_warning(message: str) -> int:
    try:
        _write_hook_json({"continue": True, "systemMessage": message})
    except (BrokenPipeError, OSError):
        pass
    return 0


def _hook_warning(host: HookHost, template: str) -> int:
    message = template.format(name=host.command, agent=host.label)
    if host.codec == "json":
        return _json_warning(message)
    # Text hosts append stdout to the model context, so warnings go to stderr.
    print(message, file=sys.stderr)
    return 0


def _emit_context(host: HookHost, event_name: str, context: str) -> int:
    try:
        if host.codec == "json":
            _write_hook_json(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event_name,
                        "additionalContext": context,
                    }
                }
            )
        else:
            print(context)
    except (BrokenPipeError, OSError):
        pass
    return 0


def _decode_event(host: HookHost) -> tuple[dict[str, object] | None, int]:
    """Read stdin; return (event-or-None, fail-open return code)."""

    try:
        event = json.load(sys.stdin)
    except Exception:
        return None, _hook_warning(host, _MALFORMED_HOOK_WARNING)
    if not isinstance(event, dict):
        return None, _hook_warning(host, _MALFORMED_HOOK_WARNING)
    return event, 0


def _emit_checkpoint_only(
    host: HookHost, event_name: str, event_kind: EventKind
) -> int:
    """Emit the canonical checkpoint without resolving or reading a store."""

    try:
        checkpoint = build_hook_context(
            event_kind=event_kind,
            query=None,
            cwd=Path(),
            store_root=Path(),
            max_context_bytes=CONTEXT_LIMIT_BYTES,
        )
    except Exception:
        return _hook_warning(host, _CHECKPOINT_HOOK_WARNING)
    return _emit_context(host, event_name, checkpoint)


def command_host_hook(
    args: Sequence[str], explicit_host: str | None = None
) -> int:
    """Handle one supported host hook event without blocking it."""

    if len(args) != 1:
        return _json_warning(_UNSUPPORTED_HOST_WARNING.format(host=" ".join(args)))
    slug = args[0]
    host = HOOK_HOSTS.get(slug)
    if host is None:
        return _json_warning(_UNSUPPORTED_HOST_WARNING.format(host=slug))

    event, early_code = _decode_event(host)
    if event is None:
        return early_code

    event_name = event.get("hook_event_name")
    if not isinstance(event_name, str):
        return _hook_warning(host, _MALFORMED_HOOK_WARNING)

    if event_name == "UserPromptSubmit":
        event_kind: EventKind = "turn"
        deadline: float | None = time.monotonic() + ORIENTATION_DEADLINE_SECONDS
    elif host.compact_event is not None and event_name == host.compact_event:
        if host.compact_source is not None:
            source = event.get("source")
            if not isinstance(source, str):
                return _hook_warning(host, _MALFORMED_HOOK_WARNING)
            if source != host.compact_source:
                return 0
        event_kind = "compact-recovery"
        deadline = None
    else:
        return 0

    try:
        if explicit_host is not None and parse_host(explicit_host) != slug:
            return _emit_checkpoint_only(host, event_name, event_kind)
        store_context = resolve_store_context(slug)
    except Exception:
        # Invalid or conflicting storage selection keeps the session alive
        # with the bare canonical checkpoint rather than a guessed namespace.
        return _emit_checkpoint_only(host, event_name, event_kind)

    try:
        event_cwd = event.get("cwd")
        cwd = Path(event_cwd) if isinstance(event_cwd, str) else Path.cwd()
        if native_memory_cwd(cwd):
            return 0
    except Exception:
        return _emit_checkpoint_only(host, event_name, event_kind)

    query = None
    if event_kind == "turn":
        prompt = event.get("prompt")
        if isinstance(prompt, str):
            query = prompt
    try:
        context = build_hook_context(
            event_kind=event_kind,
            query=query,
            cwd=cwd,
            store_root=store_context.store_root,
            max_context_bytes=CONTEXT_LIMIT_BYTES,
            deadline=deadline,
        )
    except CheckpointError:
        return _hook_warning(host, _CHECKPOINT_HOOK_WARNING)
    except Exception:
        return _emit_checkpoint_only(host, event_name, event_kind)
    return _emit_context(host, event_name, context)
