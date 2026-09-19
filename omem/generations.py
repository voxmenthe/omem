"""Config, policy, layout, codec, and storage for integrated generations.

This is the storage layer beneath the consolidation protocol: the artifact
layout for one store (live or migration staging), the strict JSON codec for
generation payloads, and the atomic pointer/reader that decides which
generation is current. The root policy file is parsed by `omem.config`; this
module re-exposes its policy entry points for callers of the storage layer.
Nothing here judges evidence; the protocol in `consolidation.py` does.
"""

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_NAME,
    DEFAULT_POLICY,
    ITEM_BUDGET,
    config_path,
    fetch_policy,
)
from .layout import (
    canonical_digest,
    read_json,
    secure_directory,
    secure_file,
)
from .models import (
    APPLICABILITY_MODES,
    Applicability,
    ConsolidationError,
    ConsolidationPolicy,
    DREAM_PROVENANCES,
    DeferredObservation,
    IntegratedGeneration,
    IntegratedItem,
    KINDS_BY_SCOPE,
    MAX_APPLICABILITY_CHARS,
    MAX_INTEGRATED_TEXT_CHARS,
    MAX_REASON_CHARS,
    MemoryError,
    Observation,
    PRUNING_PROFILES,
    RejectedObservation,
    RetiredItem,
    Scope,
)
from .store import (
    OBSERVATION_RECORD_BYTES,
    MemoryStore,
    decode_observation_records,
    observation_payload,
)

HEX_64_RE = re.compile(r"[0-9a-f]{64}")
REQUEST_ID_RE = re.compile(r"[0-9a-f]{24}")


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


# --- Root policy file -------------------------------------------------------


def policy_payload(policy: ConsolidationPolicy) -> dict[str, Any]:
    return {
        "catalog_pruning": policy.catalog_pruning,
        "self_catalog_target": policy.self_catalog_target,
        "repo_catalog_target": policy.repo_catalog_target,
    }


def policy_digest(policy: ConsolidationPolicy) -> str:
    return canonical_digest(policy_payload(policy))


# --- Generation storage -----------------------------------------------------


@dataclass(frozen=True)
class GenerationLayout:
    """Artifact locations for one store, live or migration staging."""

    observations_path: Path
    pending_dir: Path
    generations_dir: Path
    current_path: Path
    failures_path: Path

    def secure(self) -> None:
        secure_directory(self.pending_dir)
        secure_directory(self.generations_dir)


def live_layout(store: MemoryStore) -> GenerationLayout:
    base = store.path / "consolidations"
    return GenerationLayout(
        observations_path=store.observations_path,
        pending_dir=base / "pending",
        generations_dir=base / "generations",
        current_path=base / "current.json",
        failures_path=base / "failures.jsonl",
    )


def observations_at(
    path: Path, scope: Scope, lo: int, hi: int
) -> tuple[Observation, ...]:
    if lo < 0 or hi < lo:
        raise ConsolidationError(f"invalid observation range {lo}-{hi}")
    try:
        with path.open("rb") as handle:
            handle.seek(lo * OBSERVATION_RECORD_BYTES)
            data = handle.read((hi - lo) * OBSERVATION_RECORD_BYTES)
    except FileNotFoundError:
        raise ConsolidationError(f"missing observation log {path}")
    records = decode_observation_records(data, scope, first_id=lo)
    if len(records) != hi - lo:
        raise ConsolidationError(f"observation range {lo}-{hi} is outside {path}")
    return records


def generation_name(payload: dict[str, Any]) -> str:
    comparable = {key: value for key, value in payload.items() if key != "created_at"}
    return f"{payload['source_count']:08d}-{canonical_digest(comparable)[:16]}.json"


def _line_field(value: Any, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "\n" in value
        or "\r" in value
        or len(value) > limit
    ):
        description = (
            f"a string of {len(value)} characters"
            if isinstance(value, str)
            else type(value).__name__
        )
        raise ConsolidationError(
            f"expected a non-empty trimmed line of at most {limit} characters, "
            f"got {description}"
        )
    return value


def parse_item(raw: Any, scope: Scope, index: int) -> IntegratedItem:
    if not isinstance(raw, dict) or set(raw) != {
        "kind",
        "provenance",
        "text",
        "applicability",
        "promotion_reason",
        "source_ids",
    }:
        raise ConsolidationError(f"integrated item {index} has unexpected keys")
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in KINDS_BY_SCOPE[scope]:
        raise ConsolidationError(f"integrated item {index} has invalid kind {kind!r}")
    provenance = raw["provenance"]
    if not isinstance(provenance, str) or provenance not in DREAM_PROVENANCES:
        raise ConsolidationError(
            f"integrated item {index} has invalid provenance {provenance!r}"
        )
    text = _line_field(raw["text"], MAX_INTEGRATED_TEXT_CHARS)
    applicability = raw["applicability"]
    if not isinstance(applicability, dict) or set(applicability) != {"mode", "when"}:
        raise ConsolidationError(
            f"integrated item {index} applicability must have exactly mode and when"
        )
    mode = applicability["mode"]
    if mode not in APPLICABILITY_MODES:
        raise ConsolidationError(
            f"integrated item {index} has invalid applicability mode {mode!r}"
        )
    when = _line_field(applicability["when"], MAX_APPLICABILITY_CHARS)
    promotion_reason = _line_field(raw["promotion_reason"], MAX_REASON_CHARS)
    source_ids = raw["source_ids"]
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or any(type(source_id) is not int for source_id in source_ids)
        or source_ids != sorted(set(source_ids))
    ):
        raise ConsolidationError(
            f"integrated item {index} source_ids must be sorted unique integers"
        )
    return IntegratedItem(
        kind=kind,
        provenance=provenance,
        text=text,
        applicability=Applicability(mode=mode, when=when),
        promotion_reason=promotion_reason,
        source_ids=tuple(source_ids),
    )


def item_payload(item: IntegratedItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "provenance": item.provenance,
        "text": item.text,
        "applicability": {"mode": item.applicability.mode, "when": item.applicability.when},
        "promotion_reason": item.promotion_reason,
        "source_ids": list(item.source_ids),
    }


def parse_policy_payload(raw: Any) -> ConsolidationPolicy:
    if not isinstance(raw, dict) or set(raw) != {
        "catalog_pruning",
        "self_catalog_target",
        "repo_catalog_target",
    }:
        raise ConsolidationError("policy snapshot has unexpected keys")
    profile = raw["catalog_pruning"]
    if profile not in PRUNING_PROFILES:
        raise ConsolidationError(f"policy snapshot has invalid profile {profile!r}")
    self_target = raw["self_catalog_target"]
    repo_target = raw["repo_catalog_target"]
    if (
        type(self_target) is not int
        or type(repo_target) is not int
        or not 1 <= self_target <= ITEM_BUDGET["self"]
        or not 1 <= repo_target <= ITEM_BUDGET["repo"]
    ):
        raise ConsolidationError("policy snapshot has invalid catalog targets")
    return ConsolidationPolicy(
        catalog_pruning=profile,
        self_catalog_target=self_target,
        repo_catalog_target=repo_target,
    )


def _parse_deferred(raw: Any) -> DeferredObservation:
    if not isinstance(raw, dict) or set(raw) != {
        "source_id",
        "reason",
        "first_reviewed",
        "review_count",
    }:
        raise ConsolidationError("deferred record has unexpected keys")
    if type(raw["source_id"]) is not int or raw["source_id"] < 0:
        raise ConsolidationError("deferred record has an invalid source_id")
    reason = _line_field(raw["reason"], MAX_REASON_CHARS)
    if (
        type(raw["review_count"]) is not int
        or raw["review_count"] < 1
        or not isinstance(raw["first_reviewed"], str)
    ):
        raise ConsolidationError("deferred record has invalid review metadata")
    return DeferredObservation(
        source_id=raw["source_id"],
        reason=reason,
        first_reviewed=raw["first_reviewed"],
        review_count=raw["review_count"],
    )


def parse_rejected(raw: Any) -> RejectedObservation:
    if not isinstance(raw, dict) or set(raw) != {"source_id", "reason"}:
        raise ConsolidationError("rejected record has unexpected keys")
    if type(raw["source_id"]) is not int or raw["source_id"] < 0:
        raise ConsolidationError("rejected record has an invalid source_id")
    return RejectedObservation(
        source_id=raw["source_id"],
        reason=_line_field(raw["reason"], MAX_REASON_CHARS),
    )


def parse_retired(raw: Any) -> RetiredItem:
    if not isinstance(raw, dict) or set(raw) != {
        "base_item_index",
        "basis",
        "reason",
        "evidence_source_ids",
    }:
        raise ConsolidationError("retirement record has unexpected keys")
    if type(raw["base_item_index"]) is not int or raw["base_item_index"] < 0:
        raise ConsolidationError("retirement record has an invalid base_item_index")
    if raw["basis"] not in ("safety", "staleness", "capacity"):
        raise ConsolidationError(
            f"retirement basis must be safety, staleness, or capacity, got {raw['basis']!r}"
        )
    evidence = raw["evidence_source_ids"]
    if (
        not isinstance(evidence, list)
        or any(type(source_id) is not int for source_id in evidence)
        or evidence != sorted(set(evidence))
    ):
        raise ConsolidationError(
            "retirement evidence_source_ids must be sorted unique integers"
        )
    return RetiredItem(
        base_item_index=raw["base_item_index"],
        basis=raw["basis"],
        reason=_line_field(raw["reason"], MAX_REASON_CHARS),
        evidence_source_ids=tuple(evidence),
    )


def _generation_from_payload(
    name: str, payload: dict[str, Any], scope: Scope
) -> IntegratedGeneration:
    required = {
        "base_digest",
        "base_generation",
        "citation_digests",
        "created_at",
        "deferred",
        "items",
        "policy",
        "policy_digest",
        "rejected",
        "request_id",
        "retired",
        "scope",
        "source_count",
    }
    if set(payload) != required:
        raise ConsolidationError("integrated generation has unexpected keys")
    if payload["scope"] != scope:
        raise ConsolidationError("integrated generation is for a different scope")
    if (
        type(payload["source_count"]) is not int
        or payload["source_count"] < 0
        or not isinstance(payload["request_id"], str)
        or REQUEST_ID_RE.fullmatch(payload["request_id"]) is None
        or not isinstance(payload["created_at"], str)
        or not isinstance(payload["policy_digest"], str)
        or HEX_64_RE.fullmatch(payload["policy_digest"]) is None
    ):
        raise ConsolidationError("integrated generation has invalid metadata")
    base_generation = payload["base_generation"]
    base_digest = payload["base_digest"]
    if base_generation is not None and (
        not isinstance(base_generation, str)
        or Path(base_generation).name != base_generation
        or not isinstance(base_digest, str)
        or HEX_64_RE.fullmatch(base_digest) is None
    ):
        raise ConsolidationError("integrated generation has an invalid base reference")
    if base_generation is None and base_digest is not None:
        raise ConsolidationError("base digest without a base generation")
    try:
        created = dt.datetime.fromisoformat(payload["created_at"])
    except ValueError as error:
        raise ConsolidationError("integrated generation has an invalid timestamp") from error
    if created.utcoffset() is None:
        raise ConsolidationError("integrated generation timestamp lacks a timezone")
    policy = parse_policy_payload(payload["policy"])
    if canonical_digest(payload["policy"]) != payload["policy_digest"]:
        raise ConsolidationError("integrated generation policy digest mismatch")
    if not isinstance(payload["items"], list) or len(payload["items"]) > ITEM_BUDGET[scope]:
        raise ConsolidationError("integrated generation items exceed the budget")
    items = tuple(
        parse_item(raw, scope, index) for index, raw in enumerate(payload["items"])
    )
    citation_digests = payload["citation_digests"]
    if not isinstance(citation_digests, dict):
        raise ConsolidationError("integrated generation citation digests are invalid")
    digests: dict[int, str] = {}
    for key, value in citation_digests.items():
        if (
            not isinstance(key, str)
            or not key.isdigit()
            or not isinstance(value, str)
            or HEX_64_RE.fullmatch(value) is None
        ):
            raise ConsolidationError("integrated generation citation digest is invalid")
        digests[int(key)] = value
    cited = {source_id for item in items for source_id in item.source_ids}
    if not cited.issubset(digests):
        raise ConsolidationError("integrated generation is missing citation digests")
    deferred = tuple(_parse_deferred(raw) for raw in payload["deferred"])
    rejected = tuple(parse_rejected(raw) for raw in payload["rejected"])
    retired = tuple(parse_retired(raw) for raw in payload["retired"])
    if name != generation_name(payload):
        raise ConsolidationError("generation name does not match its content")
    return IntegratedGeneration(
        generation=name,
        request_id=payload["request_id"],
        scope=scope,
        base_generation=base_generation,
        source_count=payload["source_count"],
        created_at=payload["created_at"],
        policy=policy,
        policy_digest=payload["policy_digest"],
        items=items,
        citation_digests=digests,
        deferred=deferred,
        rejected=rejected,
        retired=retired,
    )


def read_current(layout: GenerationLayout, scope: Scope) -> IntegratedGeneration | None:
    if not layout.current_path.exists():
        return None
    try:
        pointer = read_json(layout.current_path)
    except MemoryError as error:
        raise ConsolidationError(str(error)) from error
    if set(pointer) != {"request_id", "generation"}:
        raise ConsolidationError("current pointer has unexpected keys")
    generation = pointer["generation"]
    if (
        not isinstance(generation, str)
        or Path(generation).name != generation
        or not isinstance(pointer["request_id"], str)
        or REQUEST_ID_RE.fullmatch(pointer["request_id"]) is None
    ):
        raise ConsolidationError("current pointer is invalid")
    try:
        payload = read_json(layout.generations_dir / generation)
    except MemoryError as error:
        raise ConsolidationError(str(error)) from error
    generation_obj = _generation_from_payload(generation, payload, scope)
    if generation_obj.request_id != pointer["request_id"]:
        raise ConsolidationError("current pointer and generation disagree")
    return generation_obj


def load_current_generation(store: MemoryStore) -> IntegratedGeneration | None:
    """Load and validate the current integrated generation for reads/status."""

    return read_current(live_layout(store), store.scope)


def validate_generation_sources(
    store: MemoryStore, generation: IntegratedGeneration
) -> None:
    """Prove every cited observation record still matches its apply-time digest."""

    for source_id, expected in sorted(generation.citation_digests.items()):
        try:
            observation = store.observation_at(source_id)
        except MemoryError as error:
            raise ConsolidationError(
                f"cited observation #{source_id} is unreadable"
            ) from error
        if canonical_digest(observation_payload(observation)) != expected:
            raise ConsolidationError(
                f"cited observation #{source_id} no longer matches its digest"
            )


def catalog_outcome(store: MemoryStore, generation: IntegratedGeneration) -> str:
    """Compare usable item content with the preceding immutable generation."""

    if generation.base_generation is None:
        return "initial"
    name = generation.base_generation
    previous = _generation_from_payload(
        name, read_json(live_layout(store).generations_dir / name), store.scope
    )
    before = sorted(canonical_digest(item_payload(item)) for item in previous.items)
    after = sorted(canonical_digest(item_payload(item)) for item in generation.items)
    return "unchanged" if before == after else "changed"


def record_failure(layout: GenerationLayout, request_id: str, reason: str) -> None:
    layout.secure()
    entry = json.dumps(
        {"at": now(), "request_id": request_id, "reason": reason},
        sort_keys=True,
        separators=(",", ":"),
    )
    with layout.failures_path.open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    secure_file(layout.failures_path)
