"""Shared reader for transcripts kept as rows of a host's SQLite database.

A source is one session's rows in the order of an integer key that only
grows: Devin's `row_id`, OpenCode's `seq`, an Antigravity step's `idx`. The
host's table decides which rows are final; a row that may still change is
not read, and neither is any row after it, until it is final. Progress is a
cursor: rows consumed, the last consumed key, a chained hash of the consumed
rows, and the first row's key with the first and last rows' hashes.

Every sync re-reads the first and last consumed rows; `verify` re-hashes
them all. A host that rewrites or deletes a consumed row raises
SourceMismatch, as a rewritten transcript file does. A session that is gone
raises FileNotFoundError.
"""

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Generator, Iterator, Protocol

from .reader import BATCH_BYTES, SourceMismatch, UnsupportedFile

ZERO = "0" * 64
# A busy or unopenable database is retried at the next sync; any other
# SQLite error means the host's layout is not the one this reader knows.
_TRANSIENT = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_CANTOPEN"})


class Table(Protocol):
    def rows(
        self, path: Path, after: int | None, through: int | None
    ) -> Generator[tuple[int, bytes], None, None]:
        """Rows as (key, data) with after < key <= through, in key order.
        Without `through`, rows stop before the first one that may still
        change; with it, consumed rows are re-read as they are. Raises
        FileNotFoundError when the session is gone."""
        ...


@dataclass(frozen=True)
class Cursor:
    records: int = 0
    key: int | None = None
    chain: str = ZERO
    first_key: int | None = None
    first: str = ""
    last: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, object]) -> "Cursor":
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Record:
    # The row's key.
    index: int
    data: bytes
    after: Cursor


def connect(path: Path) -> sqlite3.Connection:
    """A read-only connection to a host's database.

    Without a `-wal` file the host has closed the database, which then opens
    immutable: a read-only open of a WAL database needs that file to exist.
    """

    if not path.is_file():
        raise FileNotFoundError(path)
    mode = "ro" if path.with_name(f"{path.name}-wal").exists() else "ro&immutable=1"
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)


def fetch(database: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    """All rows of one query. A database that is busy, locked, or missing
    raises OSError; one whose layout is unexpected raises UnsupportedFile."""

    try:
        with closing(connect(database)) as db:
            return db.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise _host_error(error) from error


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chain(previous: str, row_hash: str) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + bytes.fromhex(row_hash)).hexdigest()


def _host_error(error: sqlite3.Error) -> OSError | UnsupportedFile:
    if error.sqlite_errorname in _TRANSIENT:
        return OSError(f"database unavailable: {error}")
    return UnsupportedFile(f"unexpected database layout: {error}")


class Reader:
    """`check`, `read`, and `verify`, as the transcript file reader has, over
    one host's table."""

    Cursor = Cursor

    def __init__(self, table: Table) -> None:
        self._table = table

    def _rows(self, path: Path, after: int | None, through: int | None) -> Iterator[tuple[int, bytes]]:
        try:
            with closing(self._table.rows(path, after, through)) as rows:
                yield from rows
        except sqlite3.Error as error:
            raise _host_error(error) from error

    def _row(self, path: Path, key: int) -> bytes | None:
        for _, data in self._rows(path, key - 1, key):
            return data
        return None

    def check(self, path: Path, cursor: Cursor) -> tuple[Cursor, bool]:
        """(cursor, False), or raise SourceMismatch: the first and last
        consumed rows are re-read."""

        if cursor.records == 0:
            return cursor, False
        assert cursor.key is not None and cursor.first_key is not None
        first = self._row(path, cursor.first_key)
        if first is None or _digest(first) != cursor.first:
            raise SourceMismatch("first consumed row changed or is gone")
        last = self._row(path, cursor.key)
        if last is None or _digest(last) != cursor.last:
            raise SourceMismatch("last consumed row changed or is gone")
        return cursor, False

    def verify(self, path: Path, cursor: Cursor) -> Cursor:
        """Re-hash every consumed row; raise SourceMismatch if any changed."""

        chain, count = ZERO, 0
        if cursor.records:
            for _, data in self._rows(path, None, cursor.key):
                chain = _chain(chain, _digest(data))
                count += 1
        if count != cursor.records:
            raise SourceMismatch(f"{count} rows where {cursor.records} were consumed")
        if chain != cursor.chain:
            raise SourceMismatch("consumed rows changed")
        return cursor

    def read(self, path: Path, cursor: Cursor, max_bytes: int = BATCH_BYTES) -> tuple[list[Record], Cursor]:
        """Final rows after the cursor, about `max_bytes` of data at most."""

        records: list[Record] = []
        state = cursor
        taken = 0
        for key, data in self._rows(path, cursor.key, None):
            row_hash = _digest(data)
            state = replace(
                state,
                records=state.records + 1,
                key=key,
                chain=_chain(state.chain, row_hash),
                first_key=key if state.first_key is None else state.first_key,
                first=state.first or row_hash,
                last=row_hash,
            )
            records.append(Record(key, data, state))
            taken += len(data) + 1
            if taken >= max_bytes:
                break
        return records, state
