"""Explicit staged bootstrap from a legacy store to the current format.

Migration never mutates legacy files. Legacy notes are converted into a staged
observation log under `consolidations/migration/`; each batch runs the ordinary
bounded consolidation protocol against staging; the final batch's apply commits
by copying staged artifacts into place and atomically publishing the complete
observation log last. Deleting the staging directory
abandons an interrupted migration with the legacy store unchanged.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .consolidation import apply_pending, build_request, fetch_policy
from .dreams import DreamError, load_current, validate_current_sources
from .generations import GenerationLayout, now, read_current, record_failure
from .layout import (
    StoreContext,
    atomic_write_json,
    canonical_digest,
    pending_consolidation_paths,
    read_json,
    secure_directory,
    secure_file,
)
from .models import ConsolidationError, MemoryError
from .store import (
    OBSERVATION_RECORD_BYTES,
    MemoryStore,
    decode_observation_records,
    observation_payload,
)

LEGACY_TRIGGER_CONTEXT = "Legacy raw note; original capture context unavailable"

_MIGRATION_RUBRIC_NOTE = (
    "This is a bootstrap migration of legacy raw notes. Their trigger context "
    "was not recorded; do not fabricate one. Integrate a legacy note only when "
    "its text and source already establish clear applicability and reuse; "
    "otherwise defer or reject it. A prior dream projection, when supplied, is "
    "a labeled review aid, not independent evidence."
)


def _staging_root(store: MemoryStore) -> Path:
    return store.path / "consolidations" / "migration"


def _staging_layout(store: MemoryStore) -> GenerationLayout:
    staging = _staging_root(store)
    return GenerationLayout(
        observations_path=staging / "OBSERVATIONS.log",
        pending_dir=staging / "pending",
        generations_dir=staging / "generations",
        current_path=staging / "current.json",
        failures_path=staging / "failures.jsonl",
    )


def _legacy_fingerprint(store: MemoryStore) -> dict[str, Any]:
    notes = store.snapshot()
    return _fingerprint_payload(notes)


def _fingerprint_payload(notes) -> dict[str, Any]:
    return {
        "note_count": len(notes),
        "log_digest": canonical_digest(
            [
                {
                    "id": note.id,
                    "date": note.date,
                    "kind": note.kind,
                    "provenance": note.provenance,
                    "text": note.text,
                }
                for note in notes
            ]
        ),
    }


def _legacy_observation_payload(note) -> dict[str, Any]:
    return {
        "id": note.id,
        "recorded_at": f"{note.date}T00:00:00+00:00",
        "kind": note.kind,
        "provenance": note.provenance,
        "text": note.text,
        "trigger_context": LEGACY_TRIGGER_CONTEXT,
    }


def _stage_observations(store: MemoryStore, layout: GenerationLayout) -> int:
    """Convert the legacy log into a staged observation log; return the count."""

    notes = store.snapshot()
    layout.secure()
    temp_path = layout.observations_path.with_suffix(".tmp")
    with temp_path.open("wb") as handle:
        for note in notes:
            line = json.dumps(
                _legacy_observation_payload(note),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            encoded = line.encode("utf-8")
            if len(encoded) > OBSERVATION_RECORD_BYTES - 1:
                raise ConsolidationError(
                    f"legacy note #{note.id} exceeds the observation record "
                    f"width once encoded ({len(encoded)} bytes)"
                )
            handle.write(
                encoded + b" " * (OBSERVATION_RECORD_BYTES - 1 - len(encoded)) + b"\n"
            )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, layout.observations_path)
    secure_file(layout.observations_path)
    # Prove the staged log decodes with positional identity intact.
    decoded = decode_observation_records(
        layout.observations_path.read_bytes(), store.scope
    )
    if len(decoded) != len(notes):
        raise ConsolidationError("staged observation log lost legacy notes")
    return len(notes)


def _prior_projection(store: MemoryStore) -> dict[str, Any] | None:
    """Return the valid current legacy dream as a labeled review aid, if any."""

    try:
        dream = load_current(store)
        if dream is not None:
            validate_current_sources(store, dream)
    except (DreamError, MemoryError, OSError):
        return None
    if dream is None:
        return None
    return {
        "dream_id": dream.dream_id,
        "source_count": dream.source_count,
        "items": [
            {
                "kind": item.kind,
                "standing": item.standing,
                "provenance": item.provenance,
                "text": item.text,
                "source_ids": list(item.source_ids),
            }
            for item in dream.items
        ],
    }


def request_migration(store: MemoryStore) -> dict[str, Any]:
    """Stage legacy notes and emit the next bounded bootstrap request."""

    if store.uses_observation_log():
        raise ConsolidationError(
            f"{store.scope} store at {store.path} already uses the current layout"
        )
    store.require()
    layout = _staging_layout(store)
    fingerprint = _legacy_fingerprint(store)
    snapshot_path = _staging_root(store) / "snapshot.json"
    snapshot: dict[str, Any] = {}
    if snapshot_path.exists():
        try:
            snapshot = read_json(snapshot_path)
        except MemoryError:
            snapshot = {}

    def staged_log_valid() -> bool:
        try:
            decoded = decode_observation_records(
                layout.observations_path.read_bytes(), store.scope
            )
        except (MemoryError, OSError):
            return False
        return len(decoded) == fingerprint["note_count"]

    if (
        snapshot.get("note_count") != fingerprint["note_count"]
        or snapshot.get("log_digest") != fingerprint["log_digest"]
        or not staged_log_valid()
    ):
        # The legacy log changed (or staging is corrupt): rebuild from scratch.
        if _staging_root(store).exists():
            shutil.rmtree(_staging_root(store))
        staged = _stage_observations(store, layout)
        atomic_write_json(
            snapshot_path,
            {**fingerprint, "staged_at": now(), "staged_count": staged},
        )
        secure_file(snapshot_path)
    base = read_current(layout, store.scope)
    checkpoint = base.source_count if base is not None else 0
    total = fingerprint["note_count"]
    if checkpoint >= total > 0:
        # A previous run finished every batch but crashed before commit.
        _commit(store, layout, fingerprint)
        return {
            "protocol": "memory-migration-complete",
            "scope": store.scope,
            "migrated_observations": total,
        }
    return build_request(
        store,
        layout,
        policy=fetch_policy(),
        base=base,
        migration={
            "legacy_note_count": total,
            "snapshot_digest": fingerprint["log_digest"],
            "instructions": _MIGRATION_RUBRIC_NOTE,
            "prior_projection": _prior_projection(store),
        },
    )


def _commit(
    store: MemoryStore, layout: GenerationLayout, fingerprint: dict[str, Any]
) -> None:
    """Atomically activate the staged result; the observation log is written last."""

    with store.locked():
        if store.uses_observation_log():
            raise ConsolidationError("the store already migrated; staging is obsolete")
        if _fingerprint_payload(store._snapshot_locked()) != fingerprint:
            raise ConsolidationError(
                "the legacy log changed during migration; staging is stale — "
                "run `memory migrate` again"
            )
        current = read_current(layout, store.scope)
        if current is None or current.source_count != fingerprint["note_count"]:
            raise ConsolidationError("staged bootstrap is incomplete")
        live = store.path / "consolidations"
        secure_directory(live / "pending")
        secure_directory(live / "generations")
        for generation_path in sorted(layout.generations_dir.glob("*.json")):
            target = live / "generations" / generation_path.name
            atomic_write_json(target, read_json(generation_path))
        atomic_write_json(live / "current.json", read_json(layout.current_path))
        data = layout.observations_path.read_bytes()
        records = decode_observation_records(data, store.scope)
        if len(records) != current.source_count:
            raise ConsolidationError("staged observation count changed before activation")
        descriptor, name = tempfile.mkstemp(prefix=".OBSERVATIONS-", dir=store.path)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Readers use existence as the format marker: publish only complete bytes.
            os.replace(temporary, store.observations_path)
            directory_fd = os.open(store.path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        shutil.rmtree(_staging_root(store))


def apply_migration(
    request_id: str,
    result: dict[str, Any],
    *,
    context: StoreContext | None = None,
) -> str:
    """Apply one staged bootstrap result; commit on the final batch."""

    if re.fullmatch(r"[0-9a-f]{24}", request_id) is None:
        raise ConsolidationError("request ID must be 24 lowercase hexadecimal characters")
    matches = pending_consolidation_paths(request_id, staged=True, context=context)
    if not matches:
        raise ConsolidationError(f"unknown pending migration {request_id}")
    if len(matches) != 1:
        raise ConsolidationError(f"ambiguous pending migration {request_id}")
    pending_path = matches[0]
    staging = pending_path.parents[1]
    store_path = staging.parents[1]
    pending = read_json(pending_path)
    scope_value = pending.get("scope")
    if scope_value not in ("self", "repo"):
        raise ConsolidationError("pending migration has an invalid scope")
    store = MemoryStore(store_path, scope_value)
    if store.uses_observation_log():
        raise ConsolidationError("the store already migrated; staging is obsolete")
    snapshot_path = staging / "snapshot.json"
    if not snapshot_path.exists():
        raise ConsolidationError("migration staging is missing its snapshot")
    snapshot = read_json(snapshot_path)
    fingerprint = _legacy_fingerprint(store)
    if (
        fingerprint["note_count"] != snapshot.get("note_count")
        or fingerprint["log_digest"] != snapshot.get("log_digest")
    ):
        raise ConsolidationError(
            "the legacy log changed during migration; staging is stale — "
            "run `memory migrate` again"
        )
    layout = _staging_layout(store)
    try:
        generation = apply_pending(pending_path, store, layout, result)
    except (MemoryError, OSError, ValueError, TypeError) as error:
        record_failure(layout, request_id, str(error))
        if isinstance(error, ConsolidationError):
            raise
        raise ConsolidationError(f"cannot apply migration: {error}") from error
    if generation.source_count == snapshot["note_count"]:
        _commit(store, layout, fingerprint)
        return f"migration committed for {store.scope} at {store.path}"
    remaining = snapshot["note_count"] - generation.source_count
    return (
        f"staged bootstrap advanced to {generation.source_count}; "
        f"{remaining} legacy notes remain — run `memory migrate {store.scope}` "
        "for the next batch"
    )


def recover_migration(store: MemoryStore) -> dict[str, Any]:
    """Archive a proven incomplete activation marker and resume intact staging.

    Refuse completed logs, changed legacy evidence, and bytes that do not match
    a strict prefix of the fully reviewed staging log.
    """

    if not store.uses_observation_log():
        return request_migration(store)
    layout = _staging_layout(store)
    with store.locked():
        snapshot = read_json(_staging_root(store) / "snapshot.json")
        fingerprint = _fingerprint_payload(store._snapshot_locked())
        if any(snapshot.get(key) != value for key, value in fingerprint.items()):
            raise ConsolidationError("legacy evidence changed; automatic recovery refused")
        staged = read_current(layout, store.scope)
        data = layout.observations_path.read_bytes()
        observations = decode_observation_records(data, store.scope)
        if (
            staged is None
            or staged.source_count != fingerprint["note_count"]
            or len(observations) != fingerprint["note_count"]
            or read_json(store.path / "consolidations" / "current.json")
            != read_json(layout.current_path)
        ):
            raise ConsolidationError("recovery requires complete, matching migration staging")
        if [observation_payload(record) for record in observations] != [
            _legacy_observation_payload(note) for note in store._snapshot_locked()
        ]:
            raise ConsolidationError("staged observations differ from legacy evidence; recovery refused")
        partial = store.observations_path.read_bytes()
        if len(partial) >= len(data) or not data.startswith(partial):
            raise ConsolidationError("live log is not an incomplete staging prefix; recovery refused")
        suffix = hashlib.sha256(partial).hexdigest()[:16]
        archive = store.path / f"OBSERVATIONS.interrupted-{suffix}.log"
        if archive.exists():
            raise ConsolidationError(f"recovery archive already exists: {archive}")
        os.replace(store.observations_path, archive)
        directory_fd = os.open(store.path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return request_migration(store)
