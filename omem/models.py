"""Domain types and validation for the memory MVP."""

from dataclasses import dataclass
from typing import Final, Literal

Scope = Literal["self", "repo"]
Provenance = Literal["user", "observed", "inferred", "mixed"]
Standing = Literal["current", "uncertain"]
ApplicabilityMode = Literal["general", "conditional"]
RetirementBasis = Literal["safety", "staleness", "capacity"]
PruningProfile = Literal["off", "conservative", "balanced", "aggressive"]

SCOPES: Final = ("self", "repo")
INPUT_PROVENANCES: Final = ("user", "observed", "inferred")
DREAM_PROVENANCES: Final = (*INPUT_PROVENANCES, "mixed")
STANDINGS: Final = ("current", "uncertain")
KINDS_BY_SCOPE: Final = {
    "self": ("fact", "preference", "episode"),
    "repo": ("fact", "invariant", "procedure", "preference"),
}
APPLICABILITY_MODES: Final = ("general", "conditional")
RETIREMENT_BASES: Final = ("safety", "staleness", "capacity")
PRUNING_PROFILES: Final = ("off", "conservative", "balanced", "aggressive")

MAX_OBSERVATION_TEXT_BYTES: Final = 280
MAX_TRIGGER_CONTEXT_BYTES: Final = 768
MAX_INTEGRATED_TEXT_CHARS: Final = 280
MAX_APPLICABILITY_CHARS: Final = 280
MAX_REASON_CHARS: Final = 600


class MemoryError(Exception):
    """A user-actionable memory error."""


class StoreMissing(MemoryError):
    """The requested durable store has not been initialized."""


class StoreCorrupt(MemoryError):
    """A fixed-width store or derived artifact is invalid."""


class MissingSummary(MemoryError):
    """A chronological projection needs a summary that is not available."""


class DreamError(MemoryError):
    """A dream request or result violated its contract."""


class ConsolidationError(MemoryError):
    """A consolidation request, result, or policy violated its contract."""


@dataclass(frozen=True)
class RepoIdentity:
    """Stable repository identity and its local diagnostic context."""

    key: str
    display: str
    store_id: str
    root: str
    origin: str | None


@dataclass(frozen=True)
class Note:
    """One canonical raw note."""

    id: int
    date: str
    scope: Scope
    kind: str
    provenance: Provenance
    text: str

    @property
    def payload(self) -> str:
        return f"[{self.kind}|{self.provenance}] {self.text}"


@dataclass(frozen=True)
class TreeItem:
    """One raw or compressed item in a chronological projection."""

    lo: int
    hi: int
    text: str
    provenance: Provenance
    raw: bool


@dataclass(frozen=True)
class DreamItem:
    """One source-cited item in a model-produced dream."""

    kind: str
    standing: Standing
    provenance: Provenance
    text: str
    source_ids: tuple[int, ...]


@dataclass(frozen=True)
class DreamArtifact:
    """Validated immutable dream generation."""

    dream_id: str
    scope: Scope
    source_count: int
    source_digest: str
    created_at: str
    items: tuple[DreamItem, ...]


@dataclass(frozen=True)
class Observation:
    """One canonical candidate observation: source-labeled evidence plus context."""

    id: int
    recorded_at: str
    scope: Scope
    kind: str
    provenance: Provenance
    text: str
    trigger_context: str


@dataclass(frozen=True)
class Applicability:
    """When an integrated memory applies."""

    mode: ApplicabilityMode
    when: str


@dataclass(frozen=True)
class IntegratedItem:
    """One source-cited integrated memory eligible for agent-facing retrieval."""

    kind: str
    provenance: Provenance
    text: str
    applicability: Applicability
    promotion_reason: str
    source_ids: tuple[int, ...]


@dataclass(frozen=True)
class DeferredObservation:
    """A candidate retained for future comparison, with apply-derived history."""

    source_id: int
    reason: str
    first_reviewed: str
    review_count: int


@dataclass(frozen=True)
class RejectedObservation:
    """A candidate removed from the active backlog for a recorded reason."""

    source_id: int
    reason: str


@dataclass(frozen=True)
class RetiredItem:
    """An integrated item omitted from the new generation for a recorded reason."""

    base_item_index: int
    basis: RetirementBasis
    reason: str
    evidence_source_ids: tuple[int, ...]


@dataclass(frozen=True)
class ConsolidationPolicy:
    """Effective pruning policy from the optional root config file."""

    catalog_pruning: PruningProfile
    self_catalog_target: int
    repo_catalog_target: int

    def catalog_target(self, scope: Scope) -> int:
        return self.self_catalog_target if scope == "self" else self.repo_catalog_target


@dataclass(frozen=True)
class IntegratedGeneration:
    """Validated immutable integrated generation: the sole agent-facing catalog."""

    generation: str
    request_id: str
    scope: Scope
    base_generation: str | None
    source_count: int
    created_at: str
    policy: ConsolidationPolicy
    policy_digest: str
    items: tuple[IntegratedItem, ...]
    citation_digests: dict[int, str]
    deferred: tuple[DeferredObservation, ...]
    rejected: tuple[RejectedObservation, ...]
    retired: tuple[RetiredItem, ...]
