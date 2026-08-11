"""Fail-open Codex wire adapter for canonical checkpoints and orientation."""

from __future__ import annotations

import json
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Sequence

from .layout import is_codex_native_memory_path
from .orientation import OrientationRequest, fetch_orientation

_TURN_CHECKPOINT_START = "<!-- BEGIN OMEM TURN CHECKPOINT -->"
_TURN_CHECKPOINT_END = "<!-- END OMEM TURN CHECKPOINT -->"
_COMPACTION_CHECKPOINT_START = "<!-- BEGIN OMEM COMPACTION CHECKPOINT -->"
_COMPACTION_CHECKPOINT_END = "<!-- END OMEM COMPACTION CHECKPOINT -->"
_MALFORMED_HOOK_WARNING = (
    "omem codex-hook received malformed input; Codex continues without "
    "a memory checkpoint."
)
_CHECKPOINT_HOOK_WARNING = (
    "omem codex-hook could not load one canonical checkpoint; Codex continues "
    "without it."
)
_CONTEXT_LIMIT_BYTES = 800
_ORIENTATION_DEADLINE_SECONDS = 0.500
_EVIDENCE_SEPARATOR = "\n\n"


def _read_packaged_text(name: str) -> str:
    return resources.files("omem").joinpath(name).read_text(encoding="utf-8")


def _write_hook_json(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _hook_warning(message: str) -> int:
    try:
        _write_hook_json({"continue": True, "systemMessage": message})
    except (BrokenPipeError, OSError):
        pass
    return 0


def _extract_checkpoint(
    instructions: str,
    start_marker: str,
    end_marker: str,
    label: str,
) -> str:
    if (
        instructions.count(start_marker) != 1
        or instructions.count(end_marker) != 1
    ):
        raise ValueError(f"canonical {label} checkpoint markers are not unique")
    start = instructions.index(start_marker) + len(start_marker)
    end = instructions.index(end_marker)
    if start > end:
        raise ValueError(f"canonical {label} checkpoint markers are out of order")
    checkpoint = instructions[start:end].strip()
    if not checkpoint:
        raise ValueError(f"canonical {label} checkpoint is empty")
    return checkpoint


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


def command_codex_hook(args: Sequence[str]) -> int:
    """Handle supported Codex hook events without interrupting the host."""

    if args:
        return _hook_warning(_MALFORMED_HOOK_WARNING)
    try:
        event = json.load(sys.stdin)
    except Exception:
        return _hook_warning(_MALFORMED_HOOK_WARNING)
    if not isinstance(event, dict):
        return _hook_warning(_MALFORMED_HOOK_WARNING)

    event_name = event.get("hook_event_name")
    if not isinstance(event_name, str):
        return _hook_warning(_MALFORMED_HOOK_WARNING)
    if event_name == "UserPromptSubmit":
        markers = (_TURN_CHECKPOINT_START, _TURN_CHECKPOINT_END, "turn")
        deadline = time.monotonic() + _ORIENTATION_DEADLINE_SECONDS
    elif event_name == "SessionStart":
        source = event.get("source")
        if not isinstance(source, str):
            return _hook_warning(_MALFORMED_HOOK_WARNING)
        if source != "compact":
            return 0
        markers = (
            _COMPACTION_CHECKPOINT_START,
            _COMPACTION_CHECKPOINT_END,
            "compaction",
        )
        deadline = None
    else:
        return 0

    try:
        checkpoint = _extract_checkpoint(
            _read_packaged_text("INSTRUCTIONS.md"), *markers
        )
    except Exception:
        return _hook_warning(_CHECKPOINT_HOOK_WARNING)

    try:
        event_cwd = event.get("cwd")
        cwd = Path(event_cwd) if isinstance(event_cwd, str) else Path.cwd()
        if is_codex_native_memory_path(cwd):
            return 0
    except Exception:
        return _emit_context(event_name, checkpoint)

    if event_name == "SessionStart":
        return _emit_context(event_name, checkpoint)

    context = checkpoint
    try:
        prompt = event.get("prompt")
        if isinstance(prompt, str):
            available = (
                _CONTEXT_LIMIT_BYTES
                - len(checkpoint.encode("utf-8"))
                - len(_EVIDENCE_SEPARATOR.encode("utf-8"))
            )
            if available > 0:
                result = fetch_orientation(
                    OrientationRequest(
                        query=prompt,
                        cwd=cwd,
                        max_evidence_bytes=available,
                        max_items=3,
                        deadline=deadline,  # type: ignore[arg-type]
                    )
                )
                if result.rendered:
                    proposed = f"{checkpoint}{_EVIDENCE_SEPARATOR}{result.rendered}"
                    if len(proposed.encode("utf-8")) <= _CONTEXT_LIMIT_BYTES:
                        context = proposed
    except Exception:
        # This is the total passive-provider boundary. No exception or query
        # text is reflected into the host; the canonical checkpoint survives.
        context = checkpoint
    return _emit_context(event_name, context)
