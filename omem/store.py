"""Append-only fixed-width raw storage and rebuildable summary trees."""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .layout import secure_directory, secure_file
from .models import (
    INPUT_PROVENANCES,
    KINDS_BY_SCOPE,
    MissingSummary,
    Note,
    Provenance,
    Scope,
    StoreCorrupt,
    StoreMissing,
    TreeItem,
)

LOG_RECORD_BYTES = 320
TREE_RECORD_BYTES = 288
MAX_PAYLOAD_BYTES = 280
NAP_SUMMARY_BYTES = 240
RAW_BLOCK = 2
WAKE_BUDGET = {"self": 24, "repo": 32}
POST_DREAM_RAW = 8

_PAYLOAD = re.compile(
    r"^\[(?P<kind>[a-z]+)\|(?P<provenance>[a-z]+)\] (?P<text>.*)$"
)
_LOG_HEAD = re.compile(r"^#(?P<id>\d+) (?P<date>\d{4}-\d{2}-\d{2}) (?P<body>.*)$")


def _pad(value: str, width: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > width - 1:
        raise StoreCorrupt(
            f"record is {len(encoded)} bytes; fixed record holds {width - 1}"
        )
    return encoded + (b" " * (width - 1 - len(encoded))) + b"\n"


def _repair(path: Path, width: int) -> bool:
    """Remove an unacknowledged partial tail while the caller holds the lock."""

    if not path.exists():
        return False
    size = path.stat().st_size
    remainder = size % width
    if not remainder:
        return False
    with path.open("r+b") as handle:
        handle.truncate(size - remainder)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _record_count(path: Path, width: int) -> int:
    try:
        return path.stat().st_size // width
    except FileNotFoundError:
        return 0


def _cover_raw(total: int, alpha: float) -> list[tuple[int, int]]:
    root = 1
    while root < total:
        root *= 2
    output: list[tuple[int, int]] = []
    stack = [(0, root)]
    while stack:
        lo, hi = stack.pop()
        if lo >= total:
            continue
        size = hi - lo
        if size > 1 and (hi > total or size > alpha * (total - lo)):
            middle = (lo + hi) // 2
            stack.append((middle, hi))
            stack.append((lo, middle))
        else:
            output.append((lo, hi))
    return sorted(output)


def cover(total: int, budget: int) -> list[tuple[int, int]]:
    """Return an old-coarse/recent-fine power-of-two cover."""

    if total <= 0:
        return []
    if total <= budget:
        return [(index, index + 1) for index in range(total)]
    low, high = 0.0, 1.0
    for _ in range(60):
        middle = (low + high) / 2
        if len(_cover_raw(total, middle)) > budget:
            low = middle
        else:
            high = middle
    output = _cover_raw(total, high)
    while len(output) < budget:
        index = max(
            (
                index
                for index, (lo, hi) in enumerate(output)
                if hi - lo > 1
            ),
            default=None,
        )
        if index is None:
            break
        lo, hi = output[index]
        middle = (lo + hi) // 2
        output[index : index + 1] = [(lo, middle), (middle, hi)]
    return output


class MemoryStore:
    """One private append-only scope store."""

    def __init__(self, path: Path, scope: Scope) -> None:
        self.path = path
        self.scope = scope
        self.log_path = path / "LOG.txt"
        self.tree_path = path / "TREE"
        self.lock_path = path / ".store.lock"

    def initialize(self) -> None:
        secure_directory(self.path)
        secure_directory(self.tree_path)
        secure_directory(self.path / "dreams")
        secure_directory(self.path / "dreams" / "pending")
        secure_directory(self.path / "dreams" / "generations")
        if not self.log_path.exists():
            self.log_path.touch(mode=0o600)
        if not self.lock_path.exists():
            self.lock_path.touch(mode=0o600)
        secure_file(self.log_path)
        secure_file(self.lock_path)

    def require(self) -> None:
        if (
            self.path.is_symlink()
            or self.log_path.is_symlink()
            or self.lock_path.is_symlink()
        ):
            raise StoreCorrupt(
                f"refusing symlink in managed {self.scope} store {self.path}"
            )
        if not self.path.is_dir() or not self.log_path.is_file():
            raise StoreMissing(
                f"no {self.scope} memory at {self.path}; run `memory init`"
            )

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.require()
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def count(self) -> int:
        self.require()
        size = self.log_path.stat().st_size
        if size % LOG_RECORD_BYTES:
            raise StoreCorrupt(
                f"{self.log_path} has a partial record; append once to repair it"
            )
        return size // LOG_RECORD_BYTES

    def append(self, kind: str, provenance: str, text: str) -> Note:
        self._validate_note(kind, provenance, text)
        payload = f"[{kind}|{provenance}] {text}"
        date = dt.datetime.now(dt.UTC).date().isoformat()
        with self.locked():
            _repair(self.log_path, LOG_RECORD_BYTES)
            note_id = _record_count(self.log_path, LOG_RECORD_BYTES)
            line = f"#{note_id} {date} {payload}"
            with self.log_path.open("ab") as handle:
                handle.write(_pad(line, LOG_RECORD_BYTES))
                handle.flush()
                os.fsync(handle.fileno())
        return Note(
            id=note_id,
            date=date,
            scope=self.scope,
            kind=kind,
            provenance=provenance,  # type: ignore[arg-type]
            text=text,
        )

    def _validate_note(self, kind: str, provenance: str, text: str) -> None:
        if kind not in KINDS_BY_SCOPE[self.scope]:
            allowed = ", ".join(KINDS_BY_SCOPE[self.scope])
            raise StoreCorrupt(
                f"kind {kind!r} is invalid for {self.scope}; use: {allowed}"
            )
        if provenance not in INPUT_PROVENANCES:
            allowed = ", ".join(INPUT_PROVENANCES)
            raise StoreCorrupt(
                f"provenance {provenance!r} is invalid; use: {allowed}"
            )
        if not text or text.strip() != text or "\n" in text or "\r" in text:
            raise StoreCorrupt("note text must be one non-empty trimmed line")
        payload = f"[{kind}|{provenance}] {text}".encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise StoreCorrupt(
                f"note payload is {len(payload)} bytes; maximum is "
                f"{MAX_PAYLOAD_BYTES}"
            )

    def snapshot(self, limit: int | None = None) -> tuple[Note, ...]:
        """Read a consistent prefix under the store lock."""

        with self.locked():
            _repair(self.log_path, LOG_RECORD_BYTES)
            total = _record_count(self.log_path, LOG_RECORD_BYTES)
            if limit is not None:
                total = min(total, limit)
            with self.log_path.open("rb") as handle:
                data = handle.read(total * LOG_RECORD_BYTES)
        return self._decode_records(data)

    def slice(self, lo: int, hi: int) -> tuple[Note, ...]:
        self.require()
        if lo < 0 or hi < lo:
            raise StoreCorrupt(f"invalid raw range {lo}-{hi}")
        with self.log_path.open("rb") as handle:
            handle.seek(lo * LOG_RECORD_BYTES)
            data = handle.read((hi - lo) * LOG_RECORD_BYTES)
        records = self._decode_records(data, first_id=lo)
        if len(records) != hi - lo:
            raise StoreCorrupt(f"raw range {lo}-{hi} is outside the log")
        return records

    def _decode_records(
        self, data: bytes, first_id: int = 0
    ) -> tuple[Note, ...]:
        if len(data) % LOG_RECORD_BYTES:
            raise StoreCorrupt("raw read ended inside a fixed-width record")
        notes: list[Note] = []
        for offset in range(0, len(data), LOG_RECORD_BYTES):
            raw = data[offset : offset + LOG_RECORD_BYTES].rstrip()
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise StoreCorrupt("raw log contains invalid UTF-8") from error
            head = _LOG_HEAD.fullmatch(line)
            if head is None:
                raise StoreCorrupt(f"invalid raw record at byte {offset}")
            try:
                dt.date.fromisoformat(head.group("date"))
            except ValueError as error:
                raise StoreCorrupt(
                    f"invalid date in raw record at byte {offset}"
                ) from error
            note_id = int(head.group("id"))
            expected_id = first_id + len(notes)
            if note_id != expected_id:
                raise StoreCorrupt(
                    f"raw record identity mismatch: expected #{expected_id}, "
                    f"found #{note_id}"
                )
            payload = _PAYLOAD.fullmatch(head.group("body"))
            if payload is None:
                raise StoreCorrupt(f"invalid metadata for raw note #{note_id}")
            kind = payload.group("kind")
            provenance = payload.group("provenance")
            text = payload.group("text")
            self._validate_note(kind, provenance, text)
            notes.append(
                Note(
                    id=note_id,
                    date=head.group("date"),
                    scope=self.scope,
                    kind=kind,
                    provenance=provenance,  # type: ignore[arg-type]
                    text=text,
                )
            )
        return tuple(notes)

    def recall(self, pattern: str) -> tuple[Note, ...]:
        try:
            expression = re.compile(pattern)
        except re.error as error:
            raise StoreCorrupt(f"invalid recall regex: {error}") from error
        return tuple(
            note for note in self.snapshot() if expression.search(note.payload)
        )

    def _tree_file(self, size: int) -> Path:
        path = self.tree_path / str(size)
        if self.tree_path.is_symlink() or path.is_symlink():
            raise StoreCorrupt(f"refusing summary tree symlink: {path}")
        return path

    def tree_get(self, lo: int, hi: int) -> str | None:
        size = hi - lo
        path = self._tree_file(size)
        try:
            with path.open("rb") as handle:
                handle.seek((lo // size) * TREE_RECORD_BYTES)
                record = handle.read(TREE_RECORD_BYTES)
        except FileNotFoundError:
            return None
        if len(record) != TREE_RECORD_BYTES:
            return None
        try:
            return record.decode("utf-8").rstrip() or None
        except UnicodeDecodeError as error:
            raise StoreCorrupt(
                f"summary tree record {lo}-{hi} is invalid UTF-8"
            ) from error

    def pending_nap(self) -> tuple[int, int, tuple[TreeItem, TreeItem]] | None:
        total = self.count()
        size = RAW_BLOCK
        while size <= total:
            complete = total // size
            built = _record_count(self._tree_file(size), TREE_RECORD_BYTES)
            if built < complete:
                lo = built * size
                hi = lo + size
                middle = (lo + hi) // 2
                left = self._nap_source(lo, middle)
                right = self._nap_source(middle, hi)
                return lo, hi, (left, right)
            size *= 2
        return None

    def _nap_source(self, lo: int, hi: int) -> TreeItem:
        if hi - lo == 1:
            note = self.slice(lo, hi)[0]
            return TreeItem(
                lo=lo,
                hi=hi,
                text=note.payload,
                provenance=note.provenance,
                raw=True,
            )
        summary = self.tree_get(lo, hi)
        if summary is None:
            raise MissingSummary(
                f"missing prerequisite summary #{lo}-{hi - 1}"
            )
        provenances = {note.provenance for note in self.slice(lo, hi)}
        provenance: Provenance = (
            next(iter(provenances)) if len(provenances) == 1 else "mixed"
        )
        return TreeItem(
            lo=lo,
            hi=hi,
            text=summary,
            provenance=provenance,
            raw=False,
        )

    def apply_nap(self, lo: int, hi: int, summary: str) -> bool:
        if hi <= lo or hi - lo < 2 or (hi - lo) & (hi - lo - 1):
            raise StoreCorrupt("nap range must be an aligned power-of-two block")
        if lo % (hi - lo):
            raise StoreCorrupt("nap range must align to its block size")
        if not summary or summary.strip() != summary or "\n" in summary:
            raise StoreCorrupt("nap summary must be one non-empty trimmed line")
        if len(summary.encode("utf-8")) > NAP_SUMMARY_BYTES:
            raise StoreCorrupt(
                f"nap summary exceeds {NAP_SUMMARY_BYTES} UTF-8 bytes"
            )
        with self.locked():
            pending = self.pending_nap()
            if pending is None or pending[:2] != (lo, hi):
                wanted = "none" if pending is None else f"{pending[0]}-{pending[1]}"
                raise StoreCorrupt(
                    f"nap result is stale; next required range is {wanted}"
                )
            path = self._tree_file(hi - lo)
            _repair(path, TREE_RECORD_BYTES)
            position = lo // (hi - lo)
            existing = _record_count(path, TREE_RECORD_BYTES)
            if existing != position:
                raise StoreCorrupt(
                    f"summary tree expected position {position}, found {existing}"
                )
            secure_directory(path.parent)
            with path.open("ab") as handle:
                handle.write(_pad(summary, TREE_RECORD_BYTES))
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(path)
        return True

    def chronological(self, budget: int | None = None) -> tuple[TreeItem, ...]:
        total = self.count()
        chosen = cover(total, budget or WAKE_BUDGET[self.scope])
        output: list[TreeItem] = []
        for lo, hi in chosen:
            if hi - lo == 1:
                note = self.slice(lo, hi)[0]
                output.append(
                    TreeItem(
                        lo=lo,
                        hi=hi,
                        text=note.payload,
                        provenance=note.provenance,
                        raw=True,
                    )
                )
                continue
            summary = self.tree_get(lo, hi)
            if summary is None:
                raise MissingSummary(
                    f"{self.scope} projection needs missing summary "
                    f"#{lo}-{hi - 1}; run `memory nap {self.scope}`"
                )
            provenances = {note.provenance for note in self.slice(lo, hi)}
            provenance: Provenance = (
                next(iter(provenances)) if len(provenances) == 1 else "mixed"
            )
            output.append(
                TreeItem(
                    lo=lo,
                    hi=hi,
                    text=summary,
                    provenance=provenance,
                    raw=False,
                )
            )
        return tuple(output)

    def nap_debt(self) -> int:
        total = self.count()
        built = 0
        size = RAW_BLOCK
        while size <= total:
            built += min(
                total // size,
                _record_count(self._tree_file(size), TREE_RECORD_BYTES),
            )
            size *= 2
        expected = sum(total // (2**power) for power in range(1, total.bit_length()))
        return max(0, expected - built)

    def bytes_used(self) -> int:
        self.require()
        total = 0
        for path in self.path.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total
