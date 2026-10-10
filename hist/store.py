"""The machine-wide SQLite store: schema, durability settings, transactions.

One process writes at a time (`BEGIN IMMEDIATE`, waiting at most
`WRITE_WAIT_MS`); any number read under write-ahead logging. Every write
transaction is short: parsing and summarizer calls happen outside them.
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
DATABASE_NAME = "hist.db"
WRITE_WAIT_MS = 200
READ_RETRY_SECONDS = 1.0

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

CREATE TABLE timelines (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('repository','path','worker','unclassified')),
  identity TEXT NOT NULL UNIQUE,
  display TEXT NOT NULL,
  repo_identity TEXT,
  length INTEGER NOT NULL DEFAULT 0,
  last_time TEXT,
  purge_counter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  adapter TEXT NOT NULL,
  native_id TEXT NOT NULL,
  label TEXT NOT NULL,
  class TEXT NOT NULL CHECK (class IN ('interactive','worker','subagent','unclassified')),
  alias TEXT NOT NULL UNIQUE,
  timeline_id INTEGER NOT NULL REFERENCES timelines(id),
  parent_id INTEGER REFERENCES sessions(id),
  repo_identity TEXT,
  cwd TEXT,
  cli_version TEXT,
  started TEXT NOT NULL,
  first_time TEXT,
  last_time TEXT,
  first_position INTEGER,
  last_position INTEGER,
  message_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (adapter, native_id)
);
CREATE INDEX sessions_by_timeline ON sessions (timeline_id, first_position);
CREATE INDEX sessions_by_native ON sessions (native_id);

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  adapter TEXT NOT NULL,
  native_id TEXT NOT NULL,
  label TEXT NOT NULL,
  path TEXT NOT NULL,
  token TEXT NOT NULL,
  cursor TEXT NOT NULL,
  session_id INTEGER REFERENCES sessions(id),
  state TEXT NOT NULL CHECK (state IN ('active','skipped','stopped')),
  reason TEXT,
  unknown TEXT NOT NULL DEFAULT '{}',
  verified_at TEXT,
  UNIQUE (adapter, native_id)
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL REFERENCES timelines(id),
  position INTEGER NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  record INTEGER NOT NULL,
  part INTEGER NOT NULL,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  kind TEXT NOT NULL
    CHECK (kind IN ('user','agent','tool_call','tool_result','work','event')),
  tool TEXT,
  time TEXT NOT NULL,
  text TEXT NOT NULL,
  key TEXT,
  UNIQUE (timeline_id, position),
  UNIQUE (source_id, record, part)
);
CREATE INDEX messages_by_kind ON messages (timeline_id, kind, position, tool);
CREATE INDEX messages_by_session ON messages (session_id, position);
CREATE UNIQUE INDEX messages_by_key ON messages (key) WHERE key IS NOT NULL;

CREATE VIRTUAL TABLE search USING fts5 (
  text, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE nodes (
  timeline_id INTEGER NOT NULL,
  level INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('exact','summary')),
  text TEXT NOT NULL,
  receipt TEXT,
  PRIMARY KEY (timeline_id, level, idx)
) WITHOUT ROWID;

CREATE TABLE links (
  child_adapter TEXT NOT NULL,
  child_native TEXT NOT NULL,
  parent_adapter TEXT NOT NULL,
  parent_native TEXT NOT NULL,
  via TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (child_adapter, child_native)
) WITHOUT ROWID;

CREATE TABLE directories (
  path TEXT PRIMARY KEY,
  repo_key TEXT,
  repo_display TEXT,
  repo_root TEXT,
  checked_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE scan_dirs (
  path TEXT PRIMARY KEY,
  mtime_ns INTEGER NOT NULL,
  subdirs TEXT NOT NULL,
  files TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE alarms (
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  detail TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (kind, subject)
) WITHOUT ROWID;
"""


class StoreError(RuntimeError):
    """The store is damaged, from another version, or unusable."""


class StoreBusy(RuntimeError):
    """Another process holds the write lock past the wait budget."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise StoreError(f"refusing symlinked store directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


class Store:
    """An open store. Use `write()` for every mutation."""

    def __init__(self, connection: sqlite3.Connection, home: Path) -> None:
        self.connection = connection
        self.home = home

    @classmethod
    def open(cls, home: Path) -> "Store":
        _secure_directory(home)
        path = home / DATABASE_NAME
        if path.is_symlink():
            raise StoreError(f"refusing symlinked database: {path}")
        connection = sqlite3.connect(path, isolation_level=None, timeout=0)
        try:
            path.chmod(0o600)
            connection.execute(f"PRAGMA busy_timeout = {WRITE_WAIT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA fullfsync = ON")
            connection.execute("PRAGMA checkpoint_fullfsync = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            store = cls(connection, home)
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'meta'"
            ).fetchone():
                store._create()
            store._check_version()
        except sqlite3.DatabaseError as error:
            connection.close()
            raise StoreError(f"cannot open {path}: {error}") from error
        except BaseException:
            connection.close()
            raise
        return store

    def _create(self) -> None:
        with self.write() as db:
            if db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                return
            for statement in _SCHEMA.split(";\n"):
                if statement.strip():
                    db.execute(statement)
            db.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("generation", "1"),
                    ("install_time", utc_now()),
                ],
            )

    def _check_version(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise StoreError(
                f"store schema {row and row[0]} is not {SCHEMA_VERSION}; "
                "it was written by another hist version"
            )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction, or StoreBusy after the wait budget."""

        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" in str(error) or "busy" in str(error):
                raise StoreBusy("the store is busy") from error
            raise
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self.connection.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent snapshot; retries briefly through recovery or checkpoints."""

        deadline = time.monotonic() + READ_RETRY_SECONDS
        while True:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
                break
            except sqlite3.OperationalError as error:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                if "locked" not in str(error) and "busy" not in str(error):
                    raise
                if time.monotonic() >= deadline:
                    raise StoreBusy("the store is busy") from error
                time.sleep(0.02)
        try:
            yield self.connection
        finally:
            self.connection.execute("COMMIT")

    def meta(self, key: str) -> str:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise StoreError(f"store is missing meta {key!r}")
        return row[0]


def database_bytes(home: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(home / f"{DATABASE_NAME}{suffix}").st_size
        except FileNotFoundError:
            pass
    return total
