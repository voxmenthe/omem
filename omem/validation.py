"""The consolidation result contract: shape, citations, accounting, retirement.

`validate_result` is the adversarial boundary between the judging agent and
the store: every key set is exact, every type is strict, every offered
observation must be accounted for exactly once, and every base item must be
retained unchanged or retired under its profile's rules.
"""

import datetime as dt
from typing import Any

from .generations import item_payload, parse_item, parse_rejected, parse_retired
from .layout import canonical_digest
from .models import (
    ConsolidationError,
    ConsolidationPolicy,
    IntegratedItem,
    MemoryError,
    RejectedObservation,
    RetiredItem,
    Scope,
)


def observation_index(
    pending: dict[str, Any], scope: Scope
) -> dict[int, dict[str, Any]]:
    """Index every supplied observation payload, rejecting conflicting duplicates."""

    from .store import validate_observation_fields

    index: dict[int, dict[str, Any]] = {}
    supplied: list[dict[str, Any]] = [
        *pending["observations"],
        *pending["base_evidence"],
        *(entry["observation"] for entry in pending["deferred"]),
    ]
    for payload in supplied:
        if not isinstance(payload, dict) or set(payload) != {
            "id",
            "recorded_at",
            "kind",
            "provenance",
            "text",
            "trigger_context",
        }:
            raise ConsolidationError("supplied observation has unexpected keys")
        observation_id = payload["id"]
        if type(observation_id) is not int or observation_id < 0:
            raise ConsolidationError("supplied observation has an invalid id")
        if not isinstance(payload["recorded_at"], str):
            raise ConsolidationError("supplied observation has an invalid timestamp")
        try:
            parsed = dt.datetime.fromisoformat(payload["recorded_at"])
        except ValueError as error:
            raise ConsolidationError(
                "supplied observation has an invalid timestamp"
            ) from error
        if parsed.utcoffset() is None:
            raise ConsolidationError("supplied observation timestamp lacks a timezone")
        try:
            validate_observation_fields(
                scope,
                payload["kind"],
                payload["provenance"],
                payload["text"],
                payload["trigger_context"],
            )
        except MemoryError as error:
            raise ConsolidationError(f"supplied observation is invalid: {error}") from error
        existing = index.get(observation_id)
        if existing is not None and existing != payload:
            raise ConsolidationError(
                f"conflicting supplied payloads for observation #{observation_id}"
            )
        index[observation_id] = payload
    offered = [entry["id"] for entry in pending["observations"]]
    expected = list(range(pending["source_checkpoint"], pending["source_count"]))
    if offered != expected:
        raise ConsolidationError(
            "pending observations are not the contiguous batch after the checkpoint"
        )
    for entry in pending["deferred"]:
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"source_id", "reason", "first_reviewed", "review_count", "observation"}
            or entry["source_id"] != entry["observation"]["id"]
            or entry["source_id"] >= pending["source_checkpoint"]
        ):
            raise ConsolidationError("pending deferred record is invalid")
    return index


def _derived_provenance(
    source_ids: tuple[int, ...], index: dict[int, dict[str, Any]]
) -> str:
    labels = {index[source_id]["provenance"] for source_id in source_ids}
    return next(iter(labels)) if len(labels) == 1 else "mixed"


def validate_result(
    pending: dict[str, Any],
    result: dict[str, Any],
    policy: ConsolidationPolicy,
) -> tuple[
    tuple[IntegratedItem, ...],
    tuple[RejectedObservation, ...],
    tuple[RetiredItem, ...],
    tuple[int, ...],
]:
    scope: Scope = pending["scope"]
    if set(result) != {
        "scope",
        "base_generation",
        "source_count",
        "policy_digest",
        "items",
        "deferred",
        "rejected",
        "retired",
    }:
        raise ConsolidationError(
            "consolidation result keys must be exactly: scope, base_generation, "
            "source_count, policy_digest, items, deferred, rejected, retired"
        )
    for key in ("scope", "base_generation", "source_count", "policy_digest"):
        expected = pending[key]
        actual = result[key]
        if type(actual) is not type(expected) or actual != expected:
            raise ConsolidationError(
                f"consolidation result {key} must be {expected!r}, got {actual!r}"
            )
    index = observation_index(pending, scope)

    raw_items = result["items"]
    if not isinstance(raw_items, list):
        raise ConsolidationError("consolidation result items must be an array")
    if len(raw_items) > pending["item_budget"]:
        raise ConsolidationError(
            f"consolidation has {len(raw_items)} items; budget is {pending['item_budget']}"
        )
    items = tuple(parse_item(raw, scope, position) for position, raw in enumerate(raw_items))
    for position, item in enumerate(items):
        unknown = [
            source_id for source_id in item.source_ids if source_id not in index
        ]
        if unknown:
            raise ConsolidationError(
                f"integrated item {position} cites unsupplied observations {unknown}"
            )
        derived = _derived_provenance(item.source_ids, index)
        if item.provenance != derived:
            raise ConsolidationError(
                f"integrated item {position} provenance must be {derived!r} "
                f"from its citations, got {item.provenance!r}"
            )

    offered_ids = [
        *range(pending["source_checkpoint"], pending["source_count"]),
        *(entry["source_id"] for entry in pending["deferred"]),
    ]
    promoted: dict[int, int] = {}
    for position, item in enumerate(items):
        for source_id in item.source_ids:
            if source_id in offered_ids:
                if source_id in promoted:
                    raise ConsolidationError(
                        f"observation #{source_id} supports two integrated items"
                    )
                promoted[source_id] = position

    deferred_ids: list[int] = []
    for raw in result["deferred"]:
        entry = parse_rejected(raw)  # same shape: source_id + reason
        deferred_ids.append(entry.source_id)
    rejected = tuple(parse_rejected(raw) for raw in result["rejected"])
    for label, ids in (("deferred", deferred_ids), ("rejected", [r.source_id for r in rejected])):
        if len(ids) != len(set(ids)):
            raise ConsolidationError(f"consolidation result {label} repeat a source_id")
        outside = [source_id for source_id in ids if source_id not in offered_ids]
        if outside:
            raise ConsolidationError(
                f"consolidation result {label} name unoffered observations {outside}"
            )
    accounted = set(promoted) | set(deferred_ids) | {r.source_id for r in rejected}
    if len(accounted) != len(promoted) + len(deferred_ids) + len(rejected):
        raise ConsolidationError(
            "an offered observation is promoted and deferred or rejected"
        )
    missing = [source_id for source_id in offered_ids if source_id not in accounted]
    if missing:
        raise ConsolidationError(
            f"offered observations lack a disposition: {missing[:5]}"
        )

    retired = tuple(parse_retired(raw) for raw in result["retired"])
    retired_indices = [record.base_item_index for record in retired]
    if len(retired_indices) != len(set(retired_indices)):
        raise ConsolidationError("a base item is retired more than once")
    base_items = pending["base_items"]
    for position, raw in enumerate(base_items):
        if (
            not isinstance(raw, dict)
            or raw.get("base_item_index") != position
            or not {"kind", "provenance", "text", "applicability", "promotion_reason", "source_ids"}.issubset(raw)
        ):
            raise ConsolidationError("pending base_items are invalid")
    for record in retired:
        if record.base_item_index >= len(base_items):
            raise ConsolidationError(
                f"retirement names unknown base item {record.base_item_index}"
            )
        unknown = [
            source_id
            for source_id in record.evidence_source_ids
            if source_id not in index
        ]
        if unknown:
            raise ConsolidationError(
                f"retirement of base item {record.base_item_index} cites "
                f"unsupplied evidence {unknown}"
            )
    pressure = len(items) + sum(
        1 for record in retired if record.basis == "capacity"
    ) > pending["catalog_target"]
    for record in retired:
        if record.basis == "safety" and not record.evidence_source_ids:
            raise ConsolidationError("safety retirement requires cited evidence")
        if record.basis == "staleness" and policy.catalog_pruning == "off":
            raise ConsolidationError(
                "staleness retirement is disabled while catalog_pruning is off"
            )
        if record.basis == "capacity":
            if policy.catalog_pruning == "off":
                raise ConsolidationError(
                    "capacity retirement is disabled while catalog_pruning is off"
                )
            if not pressure:
                raise ConsolidationError(
                    "capacity retirement requires catalog target pressure"
                )

    # Every base item is either present unchanged in the result or retired once.
    def content_key(payload: dict[str, Any]) -> str:
        return canonical_digest(
            {
                "kind": payload["kind"],
                "provenance": payload["provenance"],
                "text": payload["text"],
                "applicability": payload["applicability"],
                "promotion_reason": payload["promotion_reason"],
                "source_ids": payload["source_ids"],
            }
        )

    remaining = {}
    for item in items:
        key = content_key(item_payload(item))
        remaining[key] = remaining.get(key, 0) + 1
    for position, base_item in enumerate(base_items):
        key = content_key(base_item)
        retained = bool(remaining.get(key))
        if position in retired_indices:
            if retained:
                raise ConsolidationError(
                    f"base item {position} is both retained and retired"
                )
            continue
        if not retained:
            raise ConsolidationError(
                f"base item {position} is neither retained unchanged nor retired"
            )
        remaining[key] -= 1
    return items, rejected, retired, tuple(deferred_ids)
