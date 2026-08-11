"""Domain types and validation for the memory MVP."""

from dataclasses import dataclass
from typing import Final, Literal

Scope = Literal["self", "repo"]
Provenance = Literal["user", "observed", "inferred", "mixed"]
Standing = Literal["current", "uncertain"]

SCOPES: Final = ("self", "repo")
INPUT_PROVENANCES: Final = ("user", "observed", "inferred")
DREAM_PROVENANCES: Final = (*INPUT_PROVENANCES, "mixed")
STANDINGS: Final = ("current", "uncertain")
KINDS_BY_SCOPE: Final = {
    "self": ("fact", "preference", "episode"),
    "repo": ("fact", "invariant", "procedure", "preference"),
}


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
