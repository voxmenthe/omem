"""Discovery and sync: index new transcript records into the store.

One process syncs at a time (`sync.lock`); others read what is indexed.
Parsing happens outside transactions. Each commit rechecks every involved
source's stored progress, appends messages, and advances progress in one
transaction, so a stale batch is discarded rather than stored twice.

Within a timeline, messages are merged by turn start time: a source is read
in batches, lowest frontier first, and a message is emitted only once no
other unfinished source can still produce an earlier turn. Commits cut at
record boundaries, so memory holds only the batches of overlapping sessions.
"""

import calendar
import fcntl
import heapq
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISLNK
from typing import Any, Iterator

from omem.layout import is_codex_native_memory_path

from . import reader
from .adapters import Expectation, Found, Head, Message, adapter
from .config import Config, Source
from .redact import redact
from .registry import (
    Place,
    lookup_directory,
    place_of,
    remember_directory,
    session_for,
)
from .scan import CachedListing
from .store import Store, StoreBusy, utc_now

LOCK_NAME = "sync.lock"
LOCK_WAIT_SECONDS = 0.2
COMMIT_BYTES = 4 * 1024 * 1024
FOREVER = "9999"


class StaleProgress(RuntimeError):
    """Another process advanced a source this sync was reading."""


_FAULTS: dict[str, int] = {}


def _fault(point: str) -> None:
    """Crash injection for tests: `HIST_FAULT=<point>:<n>` exits at the nth hit."""

    wanted = os.environ.get("HIST_FAULT")
    if not wanted:
        return
    name, _, count = wanted.partition(":")
    if name == point:
        _FAULTS[point] = _FAULTS.get(point, 0) + 1
        if _FAULTS[point] >= int(count or 1):
            os._exit(91)


@dataclass
class Report:
    busy: bool = False
    complete: bool = True
    added: int = 0
    sources: int = 0
    pending: dict[str, int] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "complete": self.complete,
            "added": self.added,
            "sources": self.sources,
            "pending": self.pending,
            "stopped": self.stopped,
        }


@contextmanager
def _sync_lock(home: Path, wait: float) -> Iterator[bool]:
    path = home / LOCK_NAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.01)
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def alarm(db: Any, kind: str, subject: str, detail: str) -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO alarms (kind, subject, detail, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (kind, subject) DO UPDATE SET "
        "detail = excluded.detail, last_seen = excluded.last_seen, count = count + 1",
        (kind, subject, detail, now, now),
    )


@dataclass(frozen=True)
class _Candidate:
    source: Source
    found: Found
    token: str
    mtime: float


def _token(path: str) -> tuple[str, float, bool] | None:
    """(change token, mtime, whether the path is a symlink); lstat costs what stat does."""

    try:
        info = os.lstat(path)
    except OSError:
        return None
    return f"{info.st_size}:{info.st_mtime_ns}:{info.st_ino}", info.st_mtime, S_ISLNK(info.st_mode)


def _discover(
    config: Config, listing: CachedListing, known_paths: dict[tuple[str, str], str]
) -> tuple[dict[tuple[str, str], _Candidate], list[tuple[str, str, str]]]:
    """Logical sources by (adapter, native id), and alarms to record."""

    alarms: list[tuple[str, str, str]] = []
    real_roots: list[tuple[str, str]] = []
    for source in config.sources:
        for root in source.roots:
            real_roots.append((os.path.realpath(root), source.label))
    blocked: set[str] = set()
    for k, (root, label) in enumerate(real_roots):
        for other, other_label in real_roots[k + 1 :]:
            if label != other_label and (
                root == other or root.startswith(other + "/") or other.startswith(root + "/")
            ):
                blocked.update({label, other_label})
                alarms.append(
                    ("overlap", f"{label}+{other_label}", f"{root} and {other} reach the same files")
                )
    grouped: dict[tuple[str, str], list[tuple[Source, Found]]] = {}
    for source in config.sources:
        if source.label in blocked:
            continue
        for found in adapter(source.adapter).discover(source.roots, listing):
            grouped.setdefault((source.adapter, found.native_id), []).append((source, found))
    chosen: dict[tuple[str, str], _Candidate] = {}
    for key, options in grouped.items():
        if len(options) > 1:
            current = known_paths.get(key)
            options.sort(key=lambda o: (o[1].path != current, o[1].rank, o[1].path))
        if len(options) > 1 and len({o[0].label for o in options}) > 1:
            alarms.append(
                ("duplicate", f"{key[0]}:{key[1]}", "found under several sources: "
                 + ", ".join(sorted({o[0].label for o in options})))
            )
        for source, found in options:
            stat = _token(found.path)
            if stat is None:
                continue
            if stat[2]:
                # Not following symlinks keeps two sources from ever reaching one file.
                alarms.append(("symlink", found.path, "transcript symlinks are not followed or indexed"))
                continue
            chosen[key] = _Candidate(source, found, stat[0], stat[1])
            break
    return chosen, alarms


def _excluded(config: Config, cwd: str | None, place: Place | None) -> bool:
    for entry in config.exclude:
        if entry.startswith(("/", "~")):
            root = os.path.expanduser(entry).rstrip("/")
            if cwd and (cwd == root or cwd.startswith(root + "/")):
                return True
        elif place is not None and place.kind == "repository" and (
            entry == place.display or place.display.endswith("/" + entry)
        ):
            return True
    return False


@dataclass
class _Live:
    """A source being read in this sync."""

    row: int
    adapter: str
    path: Path
    label: str
    session: int
    timeline: int
    token: str
    committed: str
    cursor: Any
    state0: dict[str, Any]
    state: dict[str, Any]
    frontier: str
    records: list[Any] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    done: bool = False
    # Progress, path, and token are saved through the end of the file.
    settled: bool = False
    # The whole consumed prefix was re-hashed in this sync.
    verified: bool = False
    stopped: str | None = None


def _load_live(row: tuple[Any, ...], candidate: _Candidate) -> _Live:
    source_id, adapter_name, label, session, timeline, cursor_json, started, alias, klass, cwd = row
    stored = json.loads(cursor_json)
    cursor = adapter(adapter_name).reader.Cursor.from_json(stored["reader"])
    state = stored["state"]
    live = _Live(
        row=source_id,
        adapter=adapter_name,
        path=Path(candidate.found.path),
        label=label,
        session=session,
        timeline=timeline,
        token=candidate.token,
        committed=cursor_json,
        cursor=cursor,
        state0=state,
        state=state,
        frontier=state.get("turn", started),
    )
    if cursor.records == 0:
        where = f" in {cwd}" if cwd else ""
        live.messages.append(
            Message(-1, 0, started, started, "event", None,
                    f"session {alias} started: {label} {klass}{where}")
        )
    return live


def _cursor_json(cursor: Any, state: dict[str, Any]) -> str:
    return json.dumps({"reader": cursor.to_json(), "state": state}, sort_keys=True)


def _register(
    store: Store,
    config: Config,
    new: list[_Candidate],
    locate: Any,
    deadline: float,
    report: Report,
) -> None:
    """Classify new sources and create their rows; skip what is never stored."""

    heads: list[tuple[_Candidate, Head | None, str | None]] = []
    for candidate in new:
        if time.monotonic() >= deadline:
            report.complete = False
            break
        try:
            head = adapter(candidate.source.adapter).head(Path(candidate.found.path), locate)
        except (Expectation, reader.UnsupportedFile) as error:
            heads.append((candidate, None, f"{type(error).__name__}: {error}"))
            continue
        except OSError:
            continue
        if head is not None:
            heads.append((candidate, head, None))
    lookups: dict[str, tuple[str | None, str | None, str | None]] = {}
    with store.read() as db:
        for _, head, _ in heads:
            if head is None or not head.cwd or head.origin:
                continue
            if place_of(db, head.cwd, None) is None and head.cwd not in lookups:
                lookups[head.cwd] = ("pending", None, None)
    for cwd in lookups:
        if is_codex_native_memory_path(Path(cwd)):
            lookups[cwd] = (None, None, None)
        else:
            lookups[cwd] = lookup_directory(cwd)
    with store.write() as db:
        for cwd, found in lookups.items():
            remember_directory(db, cwd, found)
        for candidate, head, failure in heads:
            source, found = candidate.source, candidate.found
            insert = (
                "INSERT INTO sources (adapter, native_id, label, path, token, cursor, "
                "session_id, state, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            start = _cursor_json(adapter(source.adapter).reader.Cursor(), {})
            if head is None:
                db.execute(insert, (source.adapter, found.native_id, source.label,
                                    found.path, candidate.token, start, None,
                                    "stopped", failure))
                alarm(db, "adapter", f"{source.label}:{found.native_id}", str(failure))
                report.stopped.append(f"{source.label}:{found.native_id}")
                continue
            place = place_of(db, head.cwd, head.origin)
            reason = None
            if head.session_class == "maintenance" or (
                head.cwd and is_codex_native_memory_path(Path(head.cwd))
            ):
                reason = "native maintenance session"
            elif head.skip:
                reason = head.skip
            elif _excluded(config, head.cwd, place):
                reason = "excluded by configuration"
            if reason is not None:
                db.execute(insert, (source.adapter, found.native_id, source.label,
                                    found.path, candidate.token, start, None,
                                    "skipped", reason))
                continue
            session = session_for(db, source.adapter, source.label, head, place)
            cursor = adapter(source.adapter).reader.Cursor()
            db.execute(insert, (source.adapter, found.native_id, source.label,
                                found.path, "", _cursor_json(cursor, head.state),
                                session, "active", None))
            if head.parent:
                db.execute(
                    "INSERT OR IGNORE INTO links (child_adapter, child_native, parent_adapter, "
                    "parent_native, via, recorded_at) VALUES (?, ?, ?, ?, 'native', ?)",
                    (source.adapter, head.session_id, source.adapter, head.parent, utc_now()),
                )


def _take(live: _Live, bound: str) -> tuple[list[Message], int]:
    """Messages emittable under `bound`, cut at a record boundary, and the
    number of buffered records they consume."""

    for n, message in enumerate(live.messages):
        if message.turn > bound:
            cut_record = message.record
            taken = [m for m in live.messages[:n] if m.record < cut_record]
            consumed = sum(1 for r in live.records if r.index < cut_record)
            return taken, consumed
    return list(live.messages), len(live.records)


def _commit(store: Store, group: list[_Live], report: Report) -> int:
    active = [s for s in group if s.stopped is None]
    pending = [s for s in active if not s.done]
    bound = min((s.frontier for s in pending), default=FOREVER)
    plans = []
    for live in active:
        taken, consumed = _take(live, bound)
        # A source with nothing new may still need its progress re-anchored, as after a move.
        if not taken and consumed == 0 and (live.records or not live.done or live.settled):
            continue
        if consumed == len(live.records):
            cursor, state = live.cursor, live.state
        else:
            cursor = (
                live.records[consumed - 1].after
                if consumed
                else adapter(live.adapter).reader.Cursor.from_json(json.loads(live.committed)["reader"])
            )
            state = adapter(live.adapter).map(live.records[:consumed], live.state0)[1]
        plans.append((live, taken, consumed, cursor, state))
    if not plans:
        return 0
    ordered = list(
        heapq.merge(
            *[[(m.turn, k, n, m) for n, m in enumerate(taken)] for k, (_, taken, *_r) in enumerate(plans)]
        )
    )
    added = 0
    with store.write() as db:
        for live, *_ in plans:
            stored = db.execute("SELECT cursor FROM sources WHERE id = ?", (live.row,)).fetchone()
            if stored is None or stored[0] != live.committed:
                raise StaleProgress(f"source {live.row} advanced in another process")
        timeline = group[0].timeline
        length, last_time = db.execute(
            "SELECT length, last_time FROM timelines WHERE id = ?", (timeline,)
        ).fetchone()
        sessions: dict[int, list[Any]] = {}
        for _, k, _, message in ordered:
            live = plans[k][0]
            if db.execute(
                "SELECT 1 FROM messages WHERE source_id = ? AND record = ? AND part = ?",
                (live.row, message.record, message.part),
            ).fetchone() or (
                message.key is not None
                and db.execute("SELECT 1 FROM messages WHERE key = ?", (message.key,)).fetchone()
            ):
                continue
            text = redact(message.text)
            cursor = db.execute(
                "INSERT INTO messages (timeline_id, position, source_id, record, part, "
                "session_id, kind, tool, time, text, key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timeline, length, live.row, message.record, message.part, live.session,
                 message.kind, message.tool, message.time, text, message.key),
            )
            db.execute("INSERT INTO search (rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))
            stats = sessions.setdefault(live.session, [length, length, message.time, message.time, 0])
            stats[1] = length
            stats[2] = min(stats[2], message.time)
            stats[3] = max(stats[3], message.time)
            stats[4] += 1
            last_time = max(last_time or message.time, message.time)
            length += 1
            added += 1
        db.execute(
            "UPDATE timelines SET length = ?, last_time = ? WHERE id = ?",
            (length, last_time, timeline),
        )
        for session, (first, last, first_time, last_time_s, count) in sessions.items():
            db.execute(
                "UPDATE sessions SET first_position = coalesce(first_position, ?), "
                "last_position = ?, first_time = min(coalesce(first_time, ?), ?), "
                "last_time = max(coalesce(last_time, ?), ?), message_count = message_count + ? "
                "WHERE id = ?",
                (first, last, first_time, first_time, last_time_s, last_time_s, count, session),
            )
        for live, taken, consumed, cursor, state in plans:
            caught_up = live.done and consumed == len(live.records)
            committed = _cursor_json(cursor, state)
            db.execute(
                "UPDATE sources SET cursor = ?, token = ?, path = ?, unknown = ?, "
                "verified_at = CASE WHEN ? THEN ? ELSE verified_at END WHERE id = ?",
                (committed, live.token if caught_up else "", str(live.path),
                 json.dumps(state.get("unknown", {}), sort_keys=True), live.verified, utc_now(),
                 live.row),
            )
            live.verified = False
            live.committed = committed
            live.state0 = state
            live.settled = caught_up
            cut = {id(m) for m in taken}
            live.messages = [m for m in live.messages if id(m) not in cut]
            live.records = live.records[consumed:]
    report.added += added
    _fault("commit")
    return added


def _stop_source(db: Any, row: int, name: str, reason: str) -> None:
    db.execute("UPDATE sources SET state = 'stopped', reason = ? WHERE id = ?", (reason, row))
    alarm(db, "source", name, reason)


def _stop(store: Store, live: _Live, reason: str, report: Report) -> None:
    live.stopped = reason
    name = f"{live.label}:{live.path.name}"
    with store.write() as db:
        _stop_source(db, live.row, name, reason)
    report.stopped.append(name)


def _ingest(store: Store, group: list[_Live], deadline: float, report: Report) -> None:
    for live in group:
        try:
            live.cursor, live.verified = adapter(live.adapter).reader.check(live.path, live.cursor)
        except reader.SourceMismatch as error:
            _stop(store, live, f"transcript changed under its indexed prefix: {error}", report)
        except reader.UnsupportedFile as error:
            _stop(store, live, str(error), report)
        except OSError:
            live.done = True
            live.token = ""
    buffered = 0
    while True:
        open_sources = [s for s in group if not s.done and s.stopped is None]
        if not open_sources:
            break
        if time.monotonic() >= deadline:
            report.complete = False
            break
        live = min(open_sources, key=lambda s: s.frontier)
        try:
            records, cursor = adapter(live.adapter).reader.read(live.path, live.cursor)
            messages, state = adapter(live.adapter).map(records, live.state)
        except Expectation as error:
            _stop(store, live, f"adapter expectation failed: {error}", report)
            continue
        except (OSError, reader.UnsupportedFile) as error:
            live.done, live.token = True, ""
            del error
            continue
        size = sum(len(r.data) + 1 for r in records)
        live.done = size < reader.BATCH_BYTES
        live.records += records
        live.messages += messages
        live.cursor, live.state = cursor, state
        if messages:
            live.frontier = max(live.frontier, messages[-1].turn)
        buffered += size
        if buffered >= COMMIT_BYTES:
            _commit(store, group, report)
            buffered = 0
    _commit(store, group, report)


def _resolve_links(store: Store) -> None:
    with store.write() as db:
        rows = db.execute(
            "SELECT child_adapter, child_native, parent_adapter, parent_native, via "
            "FROM links WHERE resolved = 0"
        ).fetchall()
        for child_adapter, child_native, parent_adapter, parent_native, via in rows:
            child = db.execute(
                "SELECT id, alias, class, timeline_id FROM sessions WHERE adapter = ? AND native_id = ?",
                (child_adapter, child_native),
            ).fetchone()
            parent = db.execute(
                "SELECT id, timeline_id FROM sessions WHERE native_id = ? AND "
                "(adapter = ? OR ? = '*') ORDER BY adapter = ? DESC LIMIT 1",
                (parent_native, parent_adapter, parent_adapter, parent_adapter),
            ).fetchone()
            if child is None or parent is None:
                continue
            child_id, alias, klass, _ = child
            if klass not in ("worker", "subagent"):
                db.execute(
                    "UPDATE links SET resolved = 1 WHERE child_adapter = ? AND child_native = ?",
                    (child_adapter, child_native),
                )
                continue
            task = db.execute(
                "SELECT text FROM messages WHERE session_id = ? AND kind = 'user' "
                "ORDER BY position LIMIT 1",
                (child_id,),
            ).fetchone()
            if task is None:
                continue
            source = db.execute(
                "SELECT id FROM sources WHERE session_id = ? ORDER BY id LIMIT 1", (child_id,)
            ).fetchone()
            parent_id, parent_timeline = parent
            db.execute("UPDATE sessions SET parent_id = ? WHERE id = ?", (parent_id, child_id))
            length, = db.execute(
                "SELECT length FROM timelines WHERE id = ?", (parent_timeline,)
            ).fetchone()
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            text = redact(f"worker w-{alias} started: {' '.join(task[0].split())[:200]}")
            inserted = db.execute(
                "INSERT INTO messages (timeline_id, position, source_id, record, part, "
                "session_id, kind, tool, time, text) VALUES (?, ?, ?, -2, 0, ?, 'event', ?, ?, ?)",
                (parent_timeline, length, source[0], parent_id, via, now, text),
            )
            db.execute("INSERT INTO search (rowid, text) VALUES (?, ?)", (inserted.lastrowid, text))
            db.execute(
                "UPDATE timelines SET length = length + 1, last_time = max(coalesce(last_time, ?), ?) "
                "WHERE id = ?",
                (now, now, parent_timeline),
            )
            db.execute(
                "UPDATE sessions SET last_position = ?, last_time = max(coalesce(last_time, ?), ?), "
                "message_count = message_count + 1, first_position = coalesce(first_position, ?) "
                "WHERE id = ?",
                (length, now, now, length, parent_id),
            )
            db.execute(
                "UPDATE links SET resolved = 1 WHERE child_adapter = ? AND child_native = ?",
                (child_adapter, child_native),
            )


def sync(
    store: Store,
    config: Config,
    *,
    deadline: float,
    backfill: bool = False,
    first: set[tuple[str, str]] | None = None,
    wait: float = LOCK_WAIT_SECONDS,
) -> Report:
    """Index new records; sessions in `first`, by (adapter, session id), go first."""

    report = Report()
    with _sync_lock(store.home, wait) as acquired:
        if not acquired:
            report.busy = True
            report.complete = False
            return report
        try:
            _sync_locked(store, config, deadline, backfill, first or set(), report)
        except (StoreBusy, StaleProgress):
            report.busy = True
            report.complete = False
    return report


def _sync_locked(
    store: Store,
    config: Config,
    deadline: float,
    backfill: bool,
    first: set[tuple[str, str]],
    report: Report,
) -> None:
    install = store.meta("install_time")
    with store.read() as db:
        # Older sources wait for a backfill; once one has finished, none wait.
        defer = not backfill and db.execute(
            "SELECT 1 FROM meta WHERE key = 'backfilled'"
        ).fetchone() is None
        listing = CachedListing(db)
        known = {
            (row[1], row[2]): row
            for row in db.execute(
                "SELECT id, adapter, native_id, label, path, token, state FROM sources"
            )
        }
    found, alarms = _discover(config, listing, {k: v[4] for k, v in known.items()})
    install_epoch = calendar.timegm(time.strptime(install, "%Y-%m-%dT%H:%M:%SZ"))
    threads = {
        candidate.found.session_id: Path(candidate.found.path)
        for (name, _), candidate in found.items()
        if name == "codex"
    }
    new: list[_Candidate] = []
    changed: list[_Candidate] = []
    for key, candidate in found.items():
        row = known.get(key)
        if row is None:
            if defer and candidate.mtime < install_epoch:
                label = candidate.source.label
                report.pending[label] = report.pending.get(label, 0) + 1
                continue
            new.append(candidate)
        elif row[6] == "active" and (row[5] != candidate.token or row[4] != candidate.found.path):
            changed.append(candidate)
    def later(candidate: _Candidate) -> bool:
        return (candidate.source.adapter, candidate.found.session_id) not in first

    new.sort(key=later)
    if new:
        _register(store, config, new, threads.get, deadline, report)
    groups: dict[int, list[_Live]] = {}
    preferred: set[int] = set()
    with store.read() as db:
        for candidate in new + changed:
            row = db.execute(
                "SELECT s.id, s.adapter, s.label, s.session_id, x.timeline_id, s.cursor, x.started, "
                "x.alias, x.class, x.cwd FROM sources s JOIN sessions x ON x.id = s.session_id "
                "WHERE s.adapter = ? AND s.native_id = ? AND s.state = 'active'",
                (candidate.source.adapter, candidate.found.native_id),
            ).fetchone()
            if row is not None:
                live = _load_live(row, candidate)
                groups.setdefault(live.timeline, []).append(live)
                if not later(candidate):
                    preferred.add(live.timeline)
    report.sources = sum(len(group) for group in groups.values())
    for timeline, group in sorted(
        groups.items(),
        key=lambda item: (item[0] not in preferred, min(s.frontier for s in item[1])),
    ):
        if time.monotonic() >= deadline:
            report.complete = False
            break
        _ingest(store, group, deadline, report)
    _resolve_links(store)
    with store.write() as db:
        listing.save(db)
        for kind, subject, detail in alarms:
            alarm(db, kind, subject, detail)
        if backfill and report.complete:
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('backfilled', ?)", (utc_now(),)
            )
        if not first:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (json.dumps({"time": utc_now(), **report.to_json()}),),
            )


@dataclass
class VerifyReport:
    verified: int = 0
    missing: int = 0
    busy: bool = False
    stopped: list[str] = field(default_factory=list)


def verify(store: Store, session: str | None = None, wait: float = LOCK_WAIT_SECONDS) -> VerifyReport:
    """Re-hash every indexed source's consumed prefix, or one session's.

    Catches in-place rewrites that the per-sync checks cannot see. Holds the
    sync lock, so no sync advances a cursor being verified.
    """

    report = VerifyReport()
    with _sync_lock(store.home, wait) as acquired:
        if not acquired:
            report.busy = True
            return report
        with store.read() as db:
            rows = db.execute(
                "SELECT s.id, s.adapter, s.label, s.path, s.cursor FROM sources s "
                "JOIN sessions x ON x.id = s.session_id WHERE s.state = 'active' "
                "AND (? IS NULL OR x.alias = ?) ORDER BY s.id",
                (session, session),
            ).fetchall()
        for row, adapter_name, label, path, cursor_json in rows:
            source_reader = adapter(adapter_name).reader
            cursor = source_reader.Cursor.from_json(json.loads(cursor_json)["reader"])
            name = f"{label}:{Path(path).name}"
            try:
                source_reader.verify(Path(path), cursor)
            except reader.SourceMismatch as error:
                with store.write() as db:
                    _stop_source(db, row, name, f"transcript changed under its indexed prefix: {error}")
                report.stopped.append(name)
                continue
            except (OSError, reader.UnsupportedFile):
                report.missing += 1
                continue
            with store.write() as db:
                db.execute("UPDATE sources SET verified_at = ? WHERE id = ?", (utc_now(), row))
            report.verified += 1
    return report
