"""Host-neutral checkpoint and orientation construction for CLI hooks.

One builder owns the behavior shared by every supported host edge: loading
the packaged canonical instructions, extracting exactly one marked
checkpoint, computing the remaining evidence budget, invoking the bounded
orientation service for turn queries, admitting only complete rendered
items, and enforcing the final 800 UTF-8-byte maximum.

The builder never encodes JSON, prints, reads a transcript, initializes,
repairs, or writes a store, or infers a host. Host identity arrives only as
an already-resolved StoreContext selecting which stores may be searched.
"""

import time
from importlib import resources
from pathlib import Path
from typing import Literal

from .layout import is_codex_native_memory_path
from .orientation import OrientationRequest, fetch_orientation

TURN_CHECKPOINT_START = "<!-- BEGIN OMEM TURN CHECKPOINT -->"
TURN_CHECKPOINT_END = "<!-- END OMEM TURN CHECKPOINT -->"
COMPACTION_CHECKPOINT_START = "<!-- BEGIN OMEM COMPACTION CHECKPOINT -->"
COMPACTION_CHECKPOINT_END = "<!-- END OMEM COMPACTION CHECKPOINT -->"
CONTEXT_LIMIT_BYTES = 800
ORIENTATION_DEADLINE_SECONDS = 0.500
EVIDENCE_SEPARATOR = "\n\n"

EventKind = Literal["turn", "compact-recovery"]


def read_packaged_text(name: str) -> str:
    return resources.files("omem").joinpath(name).read_text(encoding="utf-8")


class CheckpointError(Exception):
    """The packaged checkpoint resource is missing or malformed."""


def extract_checkpoint(
    instructions: str,
    start_marker: str,
    end_marker: str,
    label: str,
) -> str:
    if (
        instructions.count(start_marker) != 1
        or instructions.count(end_marker) != 1
    ):
        raise CheckpointError(
            f"canonical {label} checkpoint markers are not unique"
        )
    start = instructions.index(start_marker) + len(start_marker)
    end = instructions.index(end_marker)
    if start > end:
        raise CheckpointError(
            f"canonical {label} checkpoint markers are out of order"
        )
    checkpoint = instructions[start:end].strip()
    if not checkpoint:
        raise CheckpointError(f"canonical {label} checkpoint is empty")
    return checkpoint


def build_hook_context(
    *,
    event_kind: EventKind,
    query: str | None,
    cwd: Path,
    store_root: Path,
    max_context_bytes: int = CONTEXT_LIMIT_BYTES,
    deadline: float | None = None,
) -> str:
    """Return the bounded context for one supported hook event.

    Raises CheckpointError when the canonical checkpoint cannot be loaded;
    orientation failure keeps the bare checkpoint instead.
    """

    if event_kind == "turn":
        markers = (
            TURN_CHECKPOINT_START,
            TURN_CHECKPOINT_END,
            "turn",
        )
    else:
        markers = (
            COMPACTION_CHECKPOINT_START,
            COMPACTION_CHECKPOINT_END,
            "compaction",
        )
    try:
        checkpoint = extract_checkpoint(read_packaged_text("INSTRUCTIONS.md"), *markers)
    except Exception as error:
        raise CheckpointError(str(error)) from error

    if event_kind != "turn" or not isinstance(query, str):
        return checkpoint

    try:
        available = (
            max_context_bytes
            - len(checkpoint.encode("utf-8"))
            - len(EVIDENCE_SEPARATOR.encode("utf-8"))
        )
        if available <= 0:
            return checkpoint
        resolved_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + ORIENTATION_DEADLINE_SECONDS
        )
        result = fetch_orientation(
            OrientationRequest(
                query=query,
                cwd=cwd,
                max_evidence_bytes=available,
                max_items=3,
                deadline=resolved_deadline,
                store_root=store_root,
            )
        )
        if result.rendered:
            proposed = f"{checkpoint}{EVIDENCE_SEPARATOR}{result.rendered}"
            if len(proposed.encode("utf-8")) <= max_context_bytes:
                return proposed
    except Exception:
        # The orientation boundary is contained; the canonical checkpoint survives.
        pass
    return checkpoint


def native_memory_cwd(cwd: Path) -> bool:
    """Whether cwd sits inside a recognized Codex-owned memory tree."""

    try:
        return is_codex_native_memory_path(cwd)
    except Exception:
        return False
