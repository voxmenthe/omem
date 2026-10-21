"""Host adapters: the narrow contract between host transcripts and the core.

An adapter finds its host's transcript files, reads a transcript's head to
classify the session, and maps transcript records to normalized messages.
Mapping is pure: records plus the adapter's opaque state in, messages and
new state out. The store, timelines, tree, and commands never see a host's
record shapes.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from ..reader import Record

Kind = Literal["user", "agent", "tool_call", "tool_result", "work", "event"]
SessionClass = Literal["interactive", "worker", "subagent", "maintenance", "unclassified"]

TOOL_OUTPUT_CHARS = 30_000
TEXT_PART_CHARS = 30_000
IMAGE_OMITTED = "[image omitted]"


class Expectation(Exception):
    """A transcript broke an adapter's expectations; stop that source."""


@dataclass(frozen=True)
class Found:
    """One physical transcript file of a logical source.

    Paths stay strings during discovery: tens of thousands are found per
    sync and only changed ones are opened.
    """

    native_id: str
    session_id: str
    path: str
    # Lower ranks win when one logical source has several files.
    rank: tuple[int, ...] = ()
    # A database source's change token and last activity, which the host's
    # tables give; a transcript file's come from its lstat.
    token: str | None = None
    mtime: float | None = None


@dataclass(frozen=True)
class Head:
    """What a transcript's beginning says about its session."""

    session_id: str
    session_class: SessionClass
    started: str
    parent: str | None = None
    cwd: str | None = None
    origin: str | None = None
    cli_version: str | None = None
    skip: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    record: int
    part: int
    time: str
    turn: str
    kind: Kind
    tool: str | None
    text: str
    key: str | None = None


@dataclass(frozen=True)
class HookInput:
    session_id: str
    transcript: Path | None
    cwd: Path | None
    start: str
    subagent: bool


class Listing(Protocol):
    def __call__(self, path: str) -> tuple[list[str], list[str]] | None: ...


class Reader(Protocol):
    """Progress through a source: `hist.reader` for transcript files,
    `hist.rows.Reader` for database rows."""

    Cursor: Any

    def check(self, path: Path, cursor: Any) -> tuple[Any, bool]: ...

    def read(self, path: Path, cursor: Any, max_bytes: int = ...) -> tuple[list[Any], Any]: ...

    def verify(self, path: Path, cursor: Any) -> Any: ...


class Adapter(Protocol):
    name: str
    reader: Reader

    def discover(self, roots: tuple[Path, ...], listing: Listing) -> list[Found]: ...

    def head(self, path: Path, locate: Callable[[str], Path | None]) -> Head | None: ...

    def map(
        self, records: list[Record], state: dict[str, Any]
    ) -> tuple[list[Message], dict[str, Any]]: ...

    # Only hosts with a start hook (`hook.HOSTS`) define this.
    def hook_input(self, payload: dict[str, Any], event: str) -> HookInput: ...

    # Only hosts that record large outputs apart from the transcript define
    # this: the whole text a mapped message's text stands for, or None. It
    # may read files beside the transcript, so it runs after mapping, and
    # only for messages being indexed.
    def complete(self, transcript: Path, text: str) -> str | None: ...


def normalize_time(value: object, fallback: str) -> str:
    """ISO-8601 UTC with milliseconds, so times compare as strings."""

    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def utc_time(milliseconds: int) -> str:
    """normalize_time's format for a time since the epoch."""

    seconds, millis = divmod(milliseconds, 1000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def clip_output(text: str) -> str:
    """Tool output keeps its head and tail, TOOL_OUTPUT_CHARS in all."""

    if len(text) <= TOOL_OUTPUT_CHARS:
        return text
    half = TOOL_OUTPUT_CHARS // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n[... {dropped} characters clipped ...]\n{text[-half:]}"


def parts(
    record: int,
    first_part: int,
    time: str,
    turn: str,
    kind: Kind,
    tool: str | None,
    text: str,
    key: str | None,
) -> list[Message]:
    """One message, or consecutive parts when non-tool-output text is long."""

    if kind == "tool_result":
        chunks = [clip_output(text)]
    else:
        chunks = [
            text[k : k + TEXT_PART_CHARS] for k in range(0, len(text), TEXT_PART_CHARS)
        ] or [""]
    return [
        Message(
            record,
            first_part + n,
            time,
            turn,
            kind,
            tool,
            chunk,
            None if key is None else f"{key}#{first_part + n}",
        )
        for n, chunk in enumerate(chunks)
    ]


# Only these can produce a lone surrogate: a `\uD800`-`\uDFFF` escape, or raw
# surrogate bytes, which json.loads decodes with "surrogatepass".
_SURROGATE_MARKS = {bytes: (b"\\ud", b"\\uD", b"\xed"), str: ("\\ud", "\\uD")}
_SURROGATE = re.compile("[\ud800-\udfff]")


def loads(data: bytes | str) -> Any:
    """Parse transcript JSON so that every string in it is valid Unicode.

    JSON may escape half of a surrogate pair, as when a tool output is cut
    mid-emoji. Python keeps it, SQLite cannot encode it, and the insert would
    fail on every sync. Each lone surrogate becomes U+FFFD. Raises ValueError
    as json.loads does.
    """

    value = json.loads(data)
    if any(mark in data for mark in _SURROGATE_MARKS[type(data)]):
        value = _valid(value)
    return value


def _valid(value: Any) -> Any:
    if isinstance(value, str):
        return _SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_valid(item) for item in value]
    if isinstance(value, dict):
        return {_valid(key): _valid(item) for key, item in value.items()}
    return value


def count_unknown(state: dict[str, Any], what: str) -> None:
    unknown = state.setdefault("unknown", {})
    unknown[what] = unknown.get(what, 0) + 1


def adapter(name: str) -> Adapter:
    from . import antigravity, claude, codex, commandcode, devin, opencode

    adapters: dict[str, Adapter] = {
        "antigravity": antigravity.ADAPTER,
        "codex": codex.ADAPTER,
        "claude": claude.ADAPTER,
        "commandcode": commandcode.ADAPTER,
        "devin": devin.ADAPTER,
        "opencode": opencode.ADAPTER,
    }
    return adapters[name]
