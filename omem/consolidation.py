"""Bounded explicit consolidation: candidate observations to integrated memory.

The CLI never calls a model. `request_consolidation` snapshots a bounded batch
of evidence; the acting agent returns result JSON; `apply_consolidation`
validates it against the pending request, the effective pruning policy, and the
current generation before atomically advancing the current pointer. Nothing is
ever unlinked: observations, generations, pending requests, and failure records
remain as audit history.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from .generations import (
    HEX_64_RE,
    ITEM_BUDGET,
    REQUEST_ID_RE,
    GenerationLayout,
    fetch_policy,
    generation_name,
    item_payload,
    live_layout,
    load_current_generation,
    now,
    observations_at,
    parse_policy_payload,
    policy_digest,
    policy_payload,
    read_current,
    record_failure,
)
from .layout import (
    StoreContext,
    atomic_write_json,
    canonical_digest,
    pending_consolidation_paths,
    read_json,
)
from .models import (
    ConsolidationError,
    ConsolidationPolicy,
    IntegratedGeneration,
    IntegratedItem,
    KINDS_BY_SCOPE,
    MAX_APPLICABILITY_CHARS,
    MAX_INTEGRATED_TEXT_CHARS,
    MAX_REASON_CHARS,
    MemoryError,
    RejectedObservation,
    RetiredItem,
    Scope,
)
from .store import (
    OBSERVATION_RECORD_BYTES,
    MemoryStore,
    observation_payload,
)
from .validation import observation_index, validate_result

CONSOLIDATION_PROTOCOL = "memory-consolidation-request"
MAX_OFFERED_OBSERVATIONS = 256
ROUTINE_DUE_OBSERVATIONS = 12

SELF_CONSOLIDATION_RUBRIC = (
    "Integrate durable user facts, preferences, and meaningful episodes that are "
    "likely to change a later decision. Prefer explicit user statements and "
    "corrections over agent inference. Require a clear future applicability "
    "condition for every integrated item; defer anything plausible but "
    "under-supported."
)
REPO_CONSOLIDATION_RUBRIC = (
    "Integrate repository facts, invariants, procedures, and explicit "
    "preferences that are likely to change a later decision. Require a clear "
    "future applicability condition and adequate support. Prefer explicit user "
    "statements and corrections over agent inference."
)
CONSOLIDATION_INSTRUCTIONS = (
    "Return JSON only. Use only the supplied observations, deferred records, and "
    "base items; never use raw logs, trees, dreams, or outside knowledge. "
    "Cluster observations by the underlying decision rule, not exact wording, "
    "and distinguish independent occasions from repeated narration of one "
    "incident. Weigh source authority, contextual agreement, durability, "
    "expected future frequency and impact, corrections, and contradictions "
    "together; never promote merely because a duplicate count was reached. "
    "Permit a single-source promotion only when evidence is unusually strong, "
    "future use is likely, and a clear applicability condition can be written. "
    "Synthesize applicability.when from the contexts where the lesson will "
    "matter; do not restate the trigger event. Defer plausible but "
    "under-supported observations; reject noise, same-incident duplicates, and "
    "candidates whose repeated review, age, non-recurrence, or changed context "
    "shows low reuse. Review every base item: retain it unchanged or retire it "
    "once with a bounded reason. Safety retirement of contradicted, superseded, "
    "inapplicable, or misleading items is mandatory under every profile, and an "
    "unresolved conflict leaves no active item. Apply the pruning profile only "
    "to still-valid items under its evidence rules; capacity retirement "
    "additionally requires catalog target pressure. Emit the smallest "
    "decision-useful set. Account for every offered observation exactly once: "
    "cited by one item, deferred, or rejected."
)


def _store_key(store: MemoryStore) -> str:
    return hashlib.sha256(str(store.path.resolve()).encode("utf-8")).hexdigest()[:16]


# --- Request construction ---------------------------------------------------


def _request_id(binding: dict[str, Any]) -> str:
    return canonical_digest(binding)[:24]


def _base_binding_payload(base: IntegratedGeneration) -> dict[str, Any]:
    """Canonical base-generation binding hashed into request IDs and digests."""

    return {
        "base_generation": base.generation,
        "policy_digest": base.policy_digest,
        "source_count": base.source_count,
        "items": [item_payload(item) for item in base.items],
        "deferred": [
            {
                "source_id": record.source_id,
                "reason": record.reason,
                "first_reviewed": record.first_reviewed,
                "review_count": record.review_count,
            }
            for record in base.deferred
        ],
    }


def build_request(
    store: MemoryStore,
    layout: GenerationLayout,
    *,
    policy: ConsolidationPolicy,
    base: IntegratedGeneration | None,
    migration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Snapshot the next bounded consolidation batch into a pending request."""

    checkpoint = base.source_count if base is not None else 0
    deferred_records = list(base.deferred) if base is not None else []
    if len(deferred_records) > MAX_OFFERED_OBSERVATIONS:
        raise ConsolidationError(
            f"deferred backlog has {len(deferred_records)} entries; the "
            f"{MAX_OFFERED_OBSERVATIONS}-observation request cap was exceeded"
        )
    deferred: list[dict[str, Any]] = []
    for record in deferred_records:
        observation = observations_at(
            layout.observations_path, store.scope, record.source_id, record.source_id + 1
        )[0]
        deferred.append(
            {
                "source_id": record.source_id,
                "reason": record.reason,
                "first_reviewed": record.first_reviewed,
                "review_count": record.review_count,
                "observation": observation_payload(observation),
            }
        )

    total = layout.observations_path.stat().st_size // OBSERVATION_RECORD_BYTES
    room = MAX_OFFERED_OBSERVATIONS - len(deferred)
    advance = min(total - checkpoint, max(0, room))
    observations = [
        observation_payload(observation)
        for observation in observations_at(
            layout.observations_path, store.scope, checkpoint, checkpoint + advance
        )
    ]
    target = checkpoint + advance

    base_items: list[dict[str, Any]] = []
    base_evidence: list[dict[str, Any]] = []
    if base is not None:
        cited = sorted({source_id for item in base.items for source_id in item.source_ids})
        evidence_by_id = {}
        for source_id in cited:
            evidence_by_id[source_id] = observation_payload(
                observations_at(
                    layout.observations_path, store.scope, source_id, source_id + 1
                )[0]
            )
        base_items = [
            {"base_item_index": index, **item_payload(item)}
            for index, item in enumerate(base.items)
        ]
        base_evidence = [evidence_by_id[source_id] for source_id in cited]

    supplied = {
        "base_items": base_items,
        "base_evidence": base_evidence,
        "deferred": deferred,
        "observations": observations,
    }
    supplied_digest = canonical_digest(supplied)
    digest = policy_digest(policy)
    base_digest = (
        canonical_digest(_base_binding_payload(base)) if base is not None else None
    )
    request_id = _request_id(
        {
            "scope": store.scope,
            "store_key": _store_key(store),
            "base_generation": base.generation if base is not None else None,
            "source_count": target,
            "policy_digest": digest,
            "supplied_digest": supplied_digest,
            "migration": migration is not None,
        }
    )
    rubric = (
        SELF_CONSOLIDATION_RUBRIC if store.scope == "self" else REPO_CONSOLIDATION_RUBRIC
    )
    bundle: dict[str, Any] = {
        "protocol": CONSOLIDATION_PROTOCOL,
        "request_id": request_id,
        "store_key": _store_key(store),
        "scope": store.scope,
        "base_generation": base.generation if base is not None else None,
        "base_digest": base_digest,
        "source_checkpoint": checkpoint,
        "source_count": target,
        "policy": policy_payload(policy),
        "policy_digest": digest,
        "item_budget": ITEM_BUDGET[store.scope],
        "catalog_target": policy.catalog_target(store.scope),
        "headroom_required": room <= 0,
        "rubric": rubric,
        "instructions": CONSOLIDATION_INSTRUCTIONS,
        "result_contract": {
            "scope": store.scope,
            "base_generation": base.generation if base is not None else None,
            "source_count": target,
            "policy_digest": digest,
            "items": [
                {
                    "kind": f"one of {', '.join(KINDS_BY_SCOPE[store.scope])}",
                    "provenance": "derived from cited observations; user, observed, inferred, or mixed",
                    "text": f"one line, at most {MAX_INTEGRATED_TEXT_CHARS} characters",
                    "applicability": {
                        "mode": "general or conditional",
                        "when": f"non-empty line, at most {MAX_APPLICABILITY_CHARS} characters",
                    },
                    "promotion_reason": f"maintenance-only line, at most {MAX_REASON_CHARS} characters",
                    "source_ids": "non-empty sorted unique IDs from supplied evidence",
                }
            ],
            "deferred": [{"source_id": "offered ID", "reason": "bounded line"}],
            "rejected": [{"source_id": "offered ID", "reason": "bounded line"}],
            "retired": [
                {
                    "base_item_index": "index into base_items",
                    "basis": "safety, staleness, or capacity",
                    "reason": "bounded line",
                    "evidence_source_ids": "sorted unique supplied IDs; non-empty for safety",
                }
            ],
        },
        "base_items": base_items,
        "base_evidence": base_evidence,
        "deferred": deferred,
        "observations": observations,
        "supplied_digest": supplied_digest,
    }
    if migration is not None:
        bundle["migration"] = migration
    layout.secure()
    path = layout.pending_dir / f"{request_id}.json"
    if path.exists():
        existing = read_json(path)
        if existing != bundle:
            raise ConsolidationError(f"pending consolidation ID collision at {path}")
    else:
        atomic_write_json(path, bundle)
    return bundle


def request_consolidation(store: MemoryStore) -> dict[str, Any]:
    """Persist and return the next consolidation request for a current store."""

    if not store.uses_observation_log():
        raise ConsolidationError(
            f"{store.scope} store at {store.path} is a legacy store; "
            "run `memory migrate` first"
        )
    layout = live_layout(store)
    with store.locked():
        base = read_current(layout, store.scope)
        return build_request(store, layout, policy=fetch_policy(), base=base)


# --- Apply ------------------------------------------------------------------


def _validate_pending(pending: dict[str, Any], request_id: str) -> Scope:
    required = {
        "protocol",
        "request_id",
        "store_key",
        "scope",
        "base_generation",
        "base_digest",
        "source_checkpoint",
        "source_count",
        "policy",
        "policy_digest",
        "item_budget",
        "catalog_target",
        "headroom_required",
        "rubric",
        "instructions",
        "result_contract",
        "base_items",
        "base_evidence",
        "deferred",
        "observations",
        "supplied_digest",
    }
    optional = {"migration"}
    keys = set(pending)
    if not required.issubset(keys) or keys - required - optional:
        raise ConsolidationError("pending consolidation has unexpected keys")
    if pending["protocol"] != CONSOLIDATION_PROTOCOL:
        raise ConsolidationError("pending consolidation has an invalid protocol")
    if pending["request_id"] != request_id:
        raise ConsolidationError("pending consolidation file and payload IDs disagree")
    scope = pending["scope"]
    if scope not in ("self", "repo"):
        raise ConsolidationError("pending consolidation has an invalid scope")
    if (
        type(pending["source_checkpoint"]) is not int
        or type(pending["source_count"]) is not int
        or pending["source_checkpoint"] < 0
        or pending["source_count"] < pending["source_checkpoint"]
    ):
        raise ConsolidationError("pending consolidation has invalid source bounds")
    policy = parse_policy_payload(pending["policy"])
    if policy_digest(policy) != pending["policy_digest"]:
        raise ConsolidationError("pending consolidation policy digest mismatch")
    if pending["item_budget"] != ITEM_BUDGET[scope] or pending[
        "catalog_target"
    ] != policy.catalog_target(scope):
        raise ConsolidationError("pending consolidation has invalid budgets")
    supplied = {
        key: pending[key]
        for key in ("base_items", "base_evidence", "deferred", "observations")
    }
    if not all(isinstance(value, list) for value in supplied.values()):
        raise ConsolidationError("pending consolidation evidence must be arrays")
    if canonical_digest(supplied) != pending["supplied_digest"]:
        raise ConsolidationError("pending consolidation evidence digest is invalid")
    base_generation = pending["base_generation"]
    if base_generation is not None and (
        not isinstance(base_generation, str)
        or Path(base_generation).name != base_generation
        or not isinstance(pending["base_digest"], str)
        or HEX_64_RE.fullmatch(pending["base_digest"]) is None
    ):
        raise ConsolidationError("pending consolidation has an invalid base reference")
    if base_generation is None and pending["base_digest"] is not None:
        raise ConsolidationError("pending base digest without a base generation")
    expected = _request_id(
        {
            "scope": scope,
            "store_key": pending["store_key"],
            "base_generation": base_generation,
            "source_count": pending["source_count"],
            "policy_digest": pending["policy_digest"],
            "supplied_digest": pending["supplied_digest"],
            "migration": "migration" in pending,
        }
    )
    if expected != request_id:
        raise ConsolidationError("pending consolidation ID does not bind its contents")
    if len(pending["observations"]) + len(pending["deferred"]) > MAX_OFFERED_OBSERVATIONS:
        raise ConsolidationError("pending consolidation exceeds the offered cap")
    return scope  # type: ignore[return-value]


def _enrich_validated_result(
    pending: dict[str, Any],
    result: dict[str, Any],
    *,
    request_id: str,
    base: IntegratedGeneration | None,
    index: dict[int, dict[str, Any]],
    items: tuple[IntegratedItem, ...],
    rejected: tuple[RejectedObservation, ...],
    retired: tuple[RetiredItem, ...],
    deferred_ids: tuple[int, ...],
) -> tuple[dict[str, Any], str]:
    """Derive apply-side metadata and assemble the immutable generation payload."""

    scope: Scope = pending["scope"]
    created_at = now()
    prior_deferred = {
        record.source_id: record for record in (base.deferred if base else ())
    }
    deferred_records = []
    for source_id in deferred_ids:
        reason = next(
            entry["reason"] for entry in result["deferred"] if entry["source_id"] == source_id
        )
        prior = prior_deferred.get(source_id)
        deferred_records.append(
            {
                "source_id": source_id,
                "reason": reason,
                "first_reviewed": prior.first_reviewed if prior else created_at,
                "review_count": prior.review_count + 1 if prior else 1,
            }
        )
    citation_digests = {}
    for item in items:
        for source_id in item.source_ids:
            citation_digests[str(source_id)] = canonical_digest(index[source_id])
    payload = {
        "base_digest": pending["base_digest"],
        "base_generation": pending["base_generation"],
        "citation_digests": citation_digests,
        "created_at": created_at,
        "deferred": deferred_records,
        "items": [item_payload(item) for item in items],
        "policy": pending["policy"],
        "policy_digest": pending["policy_digest"],
        "rejected": [
            {"source_id": record.source_id, "reason": record.reason}
            for record in rejected
        ],
        "request_id": request_id,
        "retired": [
            {
                "base_item_index": record.base_item_index,
                "basis": record.basis,
                "reason": record.reason,
                "evidence_source_ids": list(record.evidence_source_ids),
            }
            for record in retired
        ],
        "scope": scope,
        "source_count": pending["source_count"],
    }
    return payload, generation_name(payload)


def apply_pending(
    pending_path: Path,
    store: MemoryStore,
    layout: GenerationLayout,
    result: dict[str, Any],
) -> IntegratedGeneration:
    request_id = pending_path.stem
    pending = read_json(pending_path)
    scope = _validate_pending(pending, request_id)
    if store.scope != scope:
        raise ConsolidationError("pending consolidation belongs to a different scope")
    with store.locked():
        policy = fetch_policy()
        if policy_digest(policy) != pending["policy_digest"]:
            raise ConsolidationError(
                "effective pruning policy changed since the request; "
                "discarding the stale result"
            )
        base = read_current(layout, scope)
        already_applied = base is not None and base.request_id == request_id
        base_name = base.generation if base is not None else None
        if not already_applied and base_name != pending["base_generation"]:
            raise ConsolidationError(
                "current generation changed since the request; "
                "discarding the stale result"
            )
        if not already_applied and base is not None:
            expected_base_digest = canonical_digest(_base_binding_payload(base))
            if expected_base_digest != pending["base_digest"]:
                raise ConsolidationError("base generation no longer matches the request")
        index = observation_index(pending, scope)
        for source_id, payload in sorted(index.items()):
            stored = observations_at(
                layout.observations_path, scope, source_id, source_id + 1
            )[0]
            if observation_payload(stored) != payload:
                raise ConsolidationError(
                    f"observation #{source_id} no longer matches the request"
                )
        items, rejected, retired, deferred_ids = validate_result(pending, result, policy)
        payload, name = _enrich_validated_result(
            pending,
            result,
            request_id=request_id,
            base=base,
            index=index,
            items=items,
            rejected=rejected,
            retired=retired,
            deferred_ids=deferred_ids,
        )
        if already_applied:
            if base.generation == name:
                return base
            raise ConsolidationError(
                "conflicting result for an already applied consolidation request"
            )
        layout.secure()
        generation_path = layout.generations_dir / name
        if generation_path.exists():
            existing = read_json(generation_path)
            comparable = dict(existing)
            comparable["created_at"] = payload["created_at"]
            if comparable != payload:
                raise ConsolidationError(
                    f"immutable generation collision at {generation_path}"
                )
        else:
            atomic_write_json(generation_path, payload)
        atomic_write_json(
            layout.current_path, {"request_id": request_id, "generation": name}
        )
    generation = read_current(layout, scope)
    if generation is None:  # unreachable: the pointer was just written
        raise ConsolidationError("current generation missing after apply")
    return generation


def apply_consolidation(
    request_id: str,
    result: dict[str, Any],
    *,
    context: StoreContext | None = None,
) -> IntegratedGeneration:
    """Validate and atomically apply a pending consolidation result."""

    if REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ConsolidationError("request ID must be 24 lowercase hexadecimal characters")
    matches = pending_consolidation_paths(request_id, context=context)
    if not matches:
        raise ConsolidationError(f"unknown pending consolidation {request_id}")
    if len(matches) != 1:
        raise ConsolidationError(f"ambiguous pending consolidation {request_id}")
    pending_path = matches[0]
    store_path = pending_path.parents[2]
    scope_value = read_json(pending_path).get("scope")
    if scope_value not in ("self", "repo"):
        raise ConsolidationError("pending consolidation has an invalid scope")
    store = MemoryStore(store_path, scope_value)
    layout = live_layout(store)
    try:
        return apply_pending(pending_path, store, layout, result)
    except (MemoryError, OSError, ValueError, TypeError) as error:
        record_failure(layout, request_id, str(error))
        if isinstance(error, ConsolidationError):
            raise
        raise ConsolidationError(f"cannot apply consolidation: {error}") from error


# --- Cadence and status -----------------------------------------------------


def consolidation_due(store: MemoryStore) -> tuple[int, bool]:
    """Return new observations since the checkpoint and whether routine work is due."""

    total = store.observation_count()
    try:
        current = load_current_generation(store)
    except ConsolidationError:
        current = None
    checkpoint = current.source_count if current is not None else 0
    new = max(0, total - checkpoint)
    return new, new >= ROUTINE_DUE_OBSERVATIONS


def pending_consolidation_counts(store: MemoryStore) -> tuple[int, int]:
    """Return actionable and total retained consolidation request counts."""

    layout = live_layout(store)
    current = read_current(layout, store.scope)
    checkpoint = current.source_count if current is not None else -1
    if not layout.pending_dir.is_dir():
        return 0, 0
    actionable = 0
    retained = 0
    for path in sorted(layout.pending_dir.glob("*.json")):
        pending = read_json(path)
        _validate_pending(pending, path.stem)
        retained += 1
        if pending["source_count"] > checkpoint:
            actionable += 1
    return actionable, retained


def consolidation_failure_summary(
    store: MemoryStore,
) -> tuple[int, dict[str, Any] | None]:
    """Return the retained failure count and latest entry."""

    path = live_layout(store).failures_path
    if not path.exists():
        return 0, None
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConsolidationError(
                f"invalid consolidation failure log {path} line {line_number}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise ConsolidationError(
                f"invalid consolidation failure entry in {path} line {line_number}"
            )
        entries.append(value)
    return len(entries), (entries[-1] if entries else None)
