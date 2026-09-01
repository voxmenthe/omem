"""Fail-open Codex and Claude JSON hook edge for canonical checkpoints.

Two concrete hosts share one envelope (hookSpecificOutput.hookEventName plus
additionalContext) and one host-neutral context builder. This module owns the
explicit codex|claude dispatch, the documented event mapping, JSON output,
bounded warnings, and the total passive-provider exception boundary: every
handled failure exits 0, never exit code 2.
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
from .layout import resolve_store_context

_MALFORMED_HOOK_WARNING = (
    "omem {name} received malformed input; {agent} continues without "
    "a memory checkpoint."
)
_CHECKPOINT_HOOK_WARNING = (
    "omem {name} could not load one canonical checkpoint; {agent} continues "
    "without it."
)

_HOST_LABELS = {
    "codex": ("codex-hook", "Codex"),
    "claude": ("hook claude", "Claude"),
}


def _malformed_warning(host: str) -> str:
    name, agent = _HOST_LABELS[host]
    return _MALFORMED_HOOK_WARNING.format(name=name, agent=agent)


def _checkpoint_warning(host: str) -> str:
    name, agent = _HOST_LABELS[host]
    return _CHECKPOINT_HOOK_WARNING.format(name=name, agent=agent)


def _write_hook_json(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _hook_warning(message: str) -> int:
    try:
        _write_hook_json({"continue": True, "systemMessage": message})
    except (BrokenPipeError, OSError):
        pass
    return 0


def _emit_context(event_name: str, context: str) -> int:
    try:
        _write_hook_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": context,
                }
            }
        )
    except (BrokenPipeError, OSError):
        pass
    return 0


def _decode_event(host: str) -> tuple[dict[str, object] | None, int]:
    """Read stdin; return (event-or-None, fail-open return code)."""

    try:
        event = json.load(sys.stdin)
    except Exception:
        return None, _hook_warning(_malformed_warning(host))
    if not isinstance(event, dict):
        return None, _hook_warning(_malformed_warning(host))
    return event, 0


def _emit_checkpoint_only(
    host: str, event_name: str, event_kind: EventKind
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
        return _hook_warning(_checkpoint_warning(host))
    return _emit_context(event_name, checkpoint)


def command_host_hook(
    args: Sequence[str], explicit_host: str | None = None
) -> int:
    """Handle one supported Codex or Claude hook event without blocking it."""

    if len(args) != 1:
        return _hook_warning(_malformed_warning("codex"))
    intrinsic_host = args[0]
    # Validate against the allow-list before any other use of the argument.
    try:
        host = parse_host(intrinsic_host)
    except Exception:
        return _hook_warning(_malformed_warning("codex"))

    event, early_code = _decode_event(host)
    if event is None:
        return early_code

    event_name = event.get("hook_event_name")
    if not isinstance(event_name, str):
        return _hook_warning(_malformed_warning(host))

    if event_name == "UserPromptSubmit":
        event_kind: EventKind = "turn"
        deadline: float | None = time.monotonic() + ORIENTATION_DEADLINE_SECONDS
    elif event_name == "SessionStart":
        source = event.get("source")
        if not isinstance(source, str):
            return _hook_warning(_malformed_warning(host))
        if source != "compact":
            return 0
        event_kind = "compact-recovery"
        deadline = None
    else:
        return 0

    try:
        if explicit_host is not None and parse_host(explicit_host) != host:
            return _emit_checkpoint_only(host, event_name, event_kind)
        store_context = resolve_store_context(host)
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
        return _hook_warning(_checkpoint_warning(host))
    except Exception:
        return _emit_checkpoint_only(host, event_name, event_kind)
    return _emit_context(event_name, context)
