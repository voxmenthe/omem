"""`hist refresh`: bring stored text up to the current text rules.

Rows keep their positions, so every reference stays valid; only text and
search rows change:

- outputs: a saved-output preview becomes the whole output, when its file
  is still a regular file in the session's own directory;
- redaction: the current rules mask what older ones missed;
- search: text whose JSON escapes were indexed undecoded is reindexed.

Each batch commits with the scan's position, so an interrupted refresh
resumes where it stopped, and every step can repeat. A complete run records
`TEXT_VERSION`; from then on rows are indexed under the current rules, and
only the outputs and redaction steps can still find work.
Replaced text is overwritten with zeros where SQLite frees it, and a final
checkpoint moves the new pages into the database file. The full-text index
keeps the old words until it is optimized, which purge's cleanup (M4) does.
"""

import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import adapter
from .redact import redact
from .render import clip_line, flat
from .store import TEXT_VERSION, Store, StoreBusy, reindex_message, replace_text, search_text, text_version

READ_ROWS = 2000
WRITE_ROWS = 200
PROGRESS_ROWS = 100_000
BUSY_SECONDS = 60.0
SAMPLES = 3
_MARKER = re.compile(r"\[redacted:([a-z0-9-]+)\]")


@dataclass
class RefreshReport:
    scanned: int = 0
    outputs: int = 0
    outputs_kept: int = 0
    redacted: int = 0
    masks: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list[str]] = field(default_factory=dict)
    reindexed: int = 0
    checkpointed: bool = False

    def summary(self, dry_run: bool, before: int) -> str:
        would = "would be" if dry_run else "were"
        lines = [
            f"scanned {self.scanned} messages (text rules version {before}; current {TEXT_VERSION})",
            (
                f"  saved outputs: {self.outputs} previews {would} replaced by the whole output; "
                f"{self.outputs_kept} stay previews (file gone, unreadable, or outside its session)"
            ),
            f"  redaction: {self.redacted} messages {would} masked further"
            + (": " + ", ".join(f"{kind} ×{n}" for kind, n in self.masks.most_common()) if self.masks else ""),
        ]
        for kind, samples in sorted(self.samples.items()):
            lines += [f"    {kind}: {sample}" for sample in samples]
        lines.append(f"  search: {self.reindexed} messages {would} reindexed with JSON escapes decoded")
        if dry_run:
            lines.append("nothing was changed (dry run)")
        else:
            lines.append(f"text rules are now version {TEXT_VERSION}")
            if not self.checkpointed:
                lines.append("  readers held the write-ahead log; replaced pages reach the database file at its next checkpoint")
        return "\n".join(lines) + "\n"


def _write(store: Store, apply: Callable[[sqlite3.Connection], None]) -> None:
    """One write transaction that waits out syncs instead of failing."""

    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            with store.write() as db:
                apply(db)
            return
        except StoreBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _apply(changes: list[tuple[int, str | None, str]], after: int | None) -> Callable[[sqlite3.Connection], None]:
    def apply(db: sqlite3.Connection) -> None:
        for message_id, new, old in changes:
            if new is None:
                reindex_message(db, message_id, old)
            else:
                replace_text(db, message_id, new)
        if after is not None:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('refresh_cursor', ?)",
                (json.dumps({"version": TEXT_VERSION, "after": after}),),
            )

    return apply


def _finish(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM meta WHERE key = 'refresh_cursor'")
    if text_version(db) < TEXT_VERSION:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('text_version', ?)", (str(TEXT_VERSION),))


def _resume(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM meta WHERE key = 'refresh_cursor'").fetchone()
    cursor = json.loads(row[0]) if row else {}
    return cursor["after"] if cursor.get("version") == TEXT_VERSION else 0


def _sample(text: str, kind: str, ref: str) -> str:
    marker = text.find(f"[redacted:{kind}]")
    return clip_line(f"{ref} …{flat(text[max(0, marker - 60) : marker + 90])}…", 220)


def refresh(store: Store, dry_run: bool, progress: Callable[[str], None] = lambda line: None) -> RefreshReport:
    report = RefreshReport()
    if not dry_run:
        store.connection.execute("PRAGMA secure_delete = ON")
        # Each commit reaches the disk before it returns, so masked text that
        # a refresh reported cannot come back after a power loss.
        store.connection.execute("PRAGMA synchronous = FULL")
    with store.read() as db:
        last = db.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
        # Search rows written under version 2 rules are already decoded.
        reindex = text_version(db) < 2
        after = 0 if dry_run else _resume(db)
        sources = {row[0]: (row[1], Path(row[2])) for row in db.execute("SELECT id, adapter, path FROM sources")}
        names = dict(db.execute("SELECT id, name FROM timelines").fetchall())
    # Hosts that keep large outputs apart from the transcript.
    completes = {
        name: host.complete
        for name in {name for name, _ in sources.values()}
        if hasattr(host := adapter(name), "complete")
    }
    while after < last:
        with store.read() as db:
            rows = db.execute(
                "SELECT id, source_id, timeline_id, position, text FROM messages "
                "WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                (after, last, READ_ROWS),
            ).fetchall()
        if not rows:
            break
        after = rows[-1][0]
        report.scanned += len(rows)
        changes: list[tuple[int, str | None, str]] = []
        for message_id, source_id, timeline_id, position, text in rows:
            host, path = sources[source_id]
            whole = completes[host](path, text) if host in completes else None
            if text.startswith("<persisted-output>") and whole is None:
                report.outputs_kept += 1
            new = redact(text if whole is None else whole)
            if whole is not None:
                report.outputs += 1
            elif new != text:
                report.redacted += 1
                added = Counter(_MARKER.findall(new)) - Counter(_MARKER.findall(text))
                report.masks.update(added)
                for kind in added:
                    found = report.samples.setdefault(kind, [])
                    if len(found) < SAMPLES:
                        found.append(_sample(new, kind, f"{names.get(timeline_id, '?')}:{position}"))
            if new != text:
                changes.append((message_id, new, text))
            elif reindex and search_text(text) != text:
                report.reindexed += 1
                changes.append((message_id, None, text))
        milestone = report.scanned % PROGRESS_ROWS < len(rows)
        if not dry_run:
            batches = [changes[k : k + WRITE_ROWS] for k in range(0, len(changes), WRITE_ROWS)]
            for n, batch in enumerate(batches or ([[]] if milestone else [])):
                _write(store, _apply(batch, after if n == max(len(batches) - 1, 0) else None))
        if milestone:
            progress(f"refresh: scanned {report.scanned} messages, through id {after} of {last}")
    if not dry_run:
        _write(store, _finish)
        busy, _, _ = store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        report.checkpointed = not busy
    return report
