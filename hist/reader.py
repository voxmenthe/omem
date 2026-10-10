"""Shared reader for append-only JSONL transcripts, plain or zstd-compressed.

Progress is a cursor: complete records consumed, a chained hash of that
prefix, the first and last consumed records' hashes, the last record's
position, and the file identity with its byte offset as a fast path. A
record is a newline-terminated line; a trailing partial line is never read.

Every sync checks the first and last consumed records in place (two short
reads). A changed file identity, as after an archive move or compression,
triggers full verification of the consumed prefix by its chained hash. An
in-place rewrite inside the prefix that keeps its length and both checked
records goes undetected until `verify`; the stored text is still what was
originally read.
"""

import hashlib
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import BinaryIO, Iterator

ZERO = "0" * 64
BATCH_BYTES = 4 * 1024 * 1024
_SKIP_CHUNK = 1024 * 1024


class SourceMismatch(Exception):
    """The file no longer extends the consumed prefix."""


class UnsupportedFile(Exception):
    """The file cannot be read here, e.g. zstd without the `zstd` command."""


@dataclass(frozen=True)
class Cursor:
    records: int = 0
    offset: int = 0
    chain: str = ZERO
    first: str = ""
    last: str = ""
    last_offset: int = 0
    last_length: int = 0
    device: int = 0
    inode: int = 0
    path: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, object]) -> "Cursor":
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Record:
    index: int
    data: bytes
    # Progress after this record, so a caller can commit any record prefix.
    after: Cursor


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chain(previous: str, record_hash: str) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + bytes.fromhex(record_hash)).hexdigest()


def is_compressed(path: Path) -> bool:
    return path.name.endswith(".zst")


def _open_stream(path: Path) -> tuple[BinaryIO, subprocess.Popen[bytes] | None]:
    if not is_compressed(path):
        return path.open("rb"), None
    binary = shutil.which("zstd")
    if binary is None:
        raise UnsupportedFile(f"{path}: zstd is not installed")
    process = subprocess.Popen(
        [binary, "-dcq", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    return process.stdout, process


def _lines(stream: BinaryIO, start: int) -> Iterator[tuple[int, bytes]]:
    """Complete lines from a stream positioned at byte `start`, with offsets."""

    position = start
    while True:
        line = stream.readline()
        if not line.endswith(b"\n"):
            return
        yield position, line[:-1]
        position += len(line)


def _skip(stream: BinaryIO, count: int) -> None:
    while count > 0:
        chunk = stream.read(min(count, _SKIP_CHUNK))
        if not chunk:
            raise SourceMismatch("file is shorter than the consumed prefix")
        count -= len(chunk)


def _identity(path: Path) -> tuple[int, int, int]:
    info = os.stat(path)
    return info.st_dev, info.st_ino, info.st_size


def check(path: Path, cursor: Cursor) -> tuple[Cursor, bool]:
    """(cursor re-anchored to `path`, whether the whole prefix was re-hashed),
    or raise SourceMismatch."""

    device, inode, size = _identity(path)
    if cursor.records == 0:
        return replace(cursor, device=device, inode=inode, path=str(path)), False
    same_file = (device, inode, str(path)) == (cursor.device, cursor.inode, cursor.path)
    if same_file and not is_compressed(path):
        if size < cursor.offset:
            raise SourceMismatch(f"truncated: {size} < {cursor.offset} bytes")
        with path.open("rb") as handle:
            first = next(_lines(handle, 0), None)
            if first is None or _digest(first[1]) != cursor.first:
                raise SourceMismatch("first record changed")
            handle.seek(cursor.last_offset)
            last = handle.read(cursor.last_length + 1)
        if (
            len(last) != cursor.last_length + 1
            or last[-1:] != b"\n"
            or _digest(last[:-1]) != cursor.last
        ):
            raise SourceMismatch("last consumed record changed or moved")
        return cursor, False
    return verify(path, cursor), True


def verify(path: Path, cursor: Cursor) -> Cursor:
    """Re-hash the consumed prefix of `path`; re-anchor or raise SourceMismatch."""

    device, inode, _ = _identity(path)
    stream, process = _open_stream(path)
    chain = ZERO
    count = 0
    end = 0
    try:
        for offset, line in _lines(stream, 0):
            if count == cursor.records:
                break
            record_hash = _digest(line)
            if count == 0 and record_hash != cursor.first:
                raise SourceMismatch("first record changed")
            chain = _chain(chain, record_hash)
            count += 1
            end = offset + len(line) + 1
    finally:
        _close(stream, process)
    if count != cursor.records:
        raise SourceMismatch(f"only {count} of {cursor.records} consumed records remain")
    if chain != cursor.chain:
        raise SourceMismatch("consumed prefix changed")
    return replace(cursor, offset=end, device=device, inode=inode, path=str(path))


def _close(stream: BinaryIO, process: subprocess.Popen[bytes] | None) -> None:
    stream.close()
    if process is not None:
        process.kill()
        process.wait()


def read(
    path: Path, cursor: Cursor, max_bytes: int = BATCH_BYTES
) -> tuple[list[Record], Cursor]:
    """Complete records after the cursor, about `max_bytes` of input at most.

    A single record larger than the limit is still returned whole.
    """

    stream, process = _open_stream(path)
    records: list[Record] = []
    state = cursor
    taken = 0
    try:
        if process is None:
            stream.seek(cursor.offset)
        else:
            _skip(stream, cursor.offset)
        for offset, line in _lines(stream, cursor.offset):
            record_hash = _digest(line)
            index = state.records
            state = replace(
                state,
                records=state.records + 1,
                offset=offset + len(line) + 1,
                chain=_chain(state.chain, record_hash),
                first=state.first or record_hash,
                last=record_hash,
                last_offset=offset,
                last_length=len(line),
            )
            records.append(Record(index, line, state))
            taken += len(line) + 1
            if taken >= max_bytes:
                break
    finally:
        _close(stream, process)
    return records, state
