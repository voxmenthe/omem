"""Where a session's messages go: repository identity, timelines, aliases.

Interactive sessions in one repository share its timeline across hosts.
Each worker, subagent, and unclassified session gets its own timeline,
named after its alias: the trailing hex of its native id, eight characters
unless that collides, assigned once and never reassigned.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from omem.layout import current_repo, normalize_origin

from .adapters import Head
from .store import utc_now

ALIAS_LENGTH = 8
NAME_LENGTH = 40


@dataclass(frozen=True)
class Place:
    """A repository, or a directory outside any repository."""

    kind: str
    identity: str
    display: str
    hint: str


def place_of(db: sqlite3.Connection, cwd: str | None, origin: str | None) -> Place | None:
    """Resolve a session's repository, caching one Git lookup per directory.

    Runs Git, so call it outside write transactions; new lookups are cached
    by `remember_directory` inside one.
    """

    if origin:
        base = Path(cwd) if cwd else None
        normalized = normalize_origin(origin, base)
        return Place("repository", f"remote:{normalized}", normalized, normalized)
    if not cwd:
        return None
    row = db.execute(
        "SELECT repo_key, repo_display FROM directories WHERE path = ?", (cwd,)
    ).fetchone()
    if row is not None:
        return _place(cwd, row[0], row[1])
    return None


def lookup_directory(cwd: str) -> tuple[str | None, str | None, str | None]:
    """(repo key, display, root) for a directory, or Nones outside a repository."""

    path = Path(cwd)
    if not path.is_dir():
        return None, None, None
    repo = current_repo(path)
    if repo is None:
        return None, None, None
    return repo.key, repo.display, repo.root


def remember_directory(
    db: sqlite3.Connection, cwd: str, found: tuple[str | None, str | None, str | None]
) -> Place:
    db.execute(
        "INSERT OR REPLACE INTO directories (path, repo_key, repo_display, repo_root, "
        "checked_at) VALUES (?, ?, ?, ?, ?)",
        (cwd, *found, utc_now()),
    )
    return _place(cwd, found[0], found[1])


def _place(cwd: str, key: str | None, display: str | None) -> Place:
    if key is None or display is None:
        return Place("path", f"path:{cwd}", cwd, cwd)
    return Place("repository", key, display, display)


def _slug(hint: str) -> str:
    tail = hint.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", tail).strip("-._")[:NAME_LENGTH]
    if not slug or not slug[0].isalpha() or re.match(r"[wu]-", slug):
        slug = f"r-{slug}" if slug else "root"
    return slug


def _unique_name(db: sqlite3.Connection, base: str) -> str:
    name, suffix = base, 2
    while db.execute("SELECT 1 FROM timelines WHERE name = ?", (name,)).fetchone():
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


def _timeline(
    db: sqlite3.Connection,
    kind: str,
    identity: str,
    display: str,
    name: str,
    repo_identity: str | None,
) -> int:
    row = db.execute("SELECT id FROM timelines WHERE identity = ?", (identity,)).fetchone()
    if row is not None:
        return int(row[0])
    cursor = db.execute(
        "INSERT INTO timelines (name, kind, identity, display, repo_identity) "
        "VALUES (?, ?, ?, ?, ?)",
        (_unique_name(db, name), kind, identity, display, repo_identity),
    )
    return int(cursor.lastrowid)  # type: ignore[arg-type]


def _alias(db: sqlite3.Connection, native_id: str) -> str:
    digits = re.sub(r"[^0-9a-f]", "", native_id.lower())
    candidates = [digits[-n:] for n in range(ALIAS_LENGTH, len(digits) + 1)]
    digest = hashlib.sha256(native_id.encode()).hexdigest()
    candidates += [digest[:n] for n in range(ALIAS_LENGTH, len(digest) + 1)]
    for alias in candidates:
        if not db.execute("SELECT 1 FROM sessions WHERE alias = ?", (alias,)).fetchone():
            return alias
    raise RuntimeError(f"no free alias for {native_id}")


def session_for(
    db: sqlite3.Connection, adapter: str, label: str, head: Head, place: Place | None
) -> int:
    """The session row for a head, creating it and its timeline if new."""

    row = db.execute(
        "SELECT id FROM sessions WHERE adapter = ? AND native_id = ?",
        (adapter, head.session_id),
    ).fetchone()
    if row is not None:
        return int(row[0])
    alias = _alias(db, head.session_id)
    repo_identity = place.identity if place and place.kind == "repository" else None
    if head.session_class == "interactive":
        if place is None:
            place = Place("path", "path:(unknown)", "(unknown directory)", "unknown")
        timeline = _timeline(
            db, place.kind, place.identity, place.display, _slug(place.hint), repo_identity
        )
    else:
        prefix = "u" if head.session_class == "unclassified" else "w"
        timeline = _timeline(
            db,
            "unclassified" if prefix == "u" else "worker",
            f"session:{adapter}:{head.session_id}",
            f"{label} {head.session_class} in {head.cwd or '(unknown directory)'}",
            f"{prefix}-{alias}",
            repo_identity,
        )
    cursor = db.execute(
        "INSERT INTO sessions (adapter, native_id, label, class, alias, timeline_id, "
        "repo_identity, cwd, cli_version, started) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            adapter,
            head.session_id,
            label,
            head.session_class,
            alias,
            timeline,
            repo_identity,
            head.cwd,
            head.cli_version,
            head.started,
        ),
    )
    return int(cursor.lastrowid)  # type: ignore[arg-type]
