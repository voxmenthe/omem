"""`hist hook <host> <event>`: SessionStart and SubagentStart.

The hook records a shell-launched worker's parent link and alarms for
transcripts outside every source. In repositories listed in `view.inject`,
it also syncs (this session's transcript first) and prints the view for an
interactive session or the short header for a worker. It never blocks the
host: any failure prints a warning on stderr and nothing on stdout.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .adapters import HookInput, adapter
from .commands import current_timeline, repository_identity, view, worker_header
from .config import Config, load_config
from .render import Timeline
from .store import Store, StoreBusy, StoreError, utc_now
from .sync import alarm, sync

HOOK_SECONDS = 1.8
EVENTS = frozenset({"SessionStart", "SubagentStart"})
HOSTS = frozenset({"codex", "claude"})
# Variables a host sets for commands it runs, naming the running session.
_SESSION_VARIABLES = {"CODEX_THREAD_ID": "codex", "CLAUDE_CODE_SESSION_ID": "claude"}
_NO_VIEW_STARTS = frozenset({"resume", "fork"})


def parent_from_environment(
    host: str, own: str, environment: Mapping[str, str]
) -> tuple[str, str, str] | None:
    """(adapter or '*', session id, via) for a shell-launched worker's parent.

    `HIST_PARENT` wins unless an inherited variable of the same host names a
    different session: a launcher set it in a parent's shell, and an
    intermediate session's worker inherited it stale. Otherwise a single
    inherited session variable names the parent; a host's variable inside its
    own hooks names the hook's own session and is ignored. Conflicting
    candidates record nothing.
    """

    candidates = {
        (owner, value.strip(), variable)
        for variable, owner in _SESSION_VARIABLES.items()
        if (value := environment.get(variable, "").strip())
        and not (owner == host == "claude")
        and value != own
    }
    explicit = environment.get("HIST_PARENT", "").strip()
    if explicit:
        name, _, rest = explicit.partition(":")
        named = (name, rest) if rest and name in HOSTS else ("*", explicit)
        if not any(owner == named[0] and value != named[1] for owner, value, _ in candidates):
            return named[0], named[1], "HIST_PARENT"
    if len({(owner, value) for owner, value, _ in candidates}) == 1:
        return next(iter(candidates))
    return None


def _owning_label(config: Config, path: Path) -> str | None:
    real = os.path.realpath(path)
    for source in config.sources:
        for root in source.roots:
            base = os.path.realpath(root)
            if real == base or real.startswith(base + "/"):
                return source.label
    return None


def _injects(config: Config, db: sqlite3.Connection, cwd: Path) -> bool:
    if config.inject == "all":
        return True
    identity = repository_identity(db, cwd)
    names = {cwd.name, identity.rstrip("/").rsplit("/", 1)[-1]}
    row = db.execute("SELECT name FROM timelines WHERE identity = ?", (identity,)).fetchone()
    if row is not None:
        names.add(row[0])
    return bool(names & set(config.inject))


def _session(db: sqlite3.Connection, host: str, native: str) -> tuple[Any, ...] | None:
    return db.execute(
        "SELECT x.class, x.timeline_id, p.alias, p.label, pt.name FROM sessions x "
        "LEFT JOIN sessions p ON p.id = x.parent_id LEFT JOIN timelines pt ON pt.id = p.timeline_id "
        "WHERE x.adapter = ? AND x.native_id = ?",
        (host, native),
    ).fetchone()


def _indexed_parent(db: sqlite3.Connection, parent: tuple[str, str, str]) -> tuple[str, str, str] | None:
    """(alias, label, timeline name) of a linked parent, once it is indexed."""

    adapter_name, native, _ = parent
    return db.execute(
        "SELECT p.alias, p.label, t.name FROM sessions p JOIN timelines t ON t.id = p.timeline_id "
        "WHERE p.native_id = ? AND (p.adapter = ? OR ? = '*') ORDER BY p.adapter = ? DESC LIMIT 1",
        (native, adapter_name, adapter_name, adapter_name),
    ).fetchone()


def _context(
    store: Store,
    config: Config,
    host: str,
    found: HookInput,
    parent: tuple[str, str, str] | None,
    environment: Mapping[str, str],
) -> str | None:
    with store.read() as db:
        row = _session(db, host, found.session_id)
        cwd = found.cwd or Path.cwd()
        if row is not None:
            klass, timeline_id = row[0], row[1]
        elif found.subagent:
            klass, timeline_id = "subagent", None
        elif host == "claude" and environment.get("CLAUDE_CODE_ENTRYPOINT") in ("sdk-cli", "sdk-py", "sdk-ts"):
            klass, timeline_id = "worker", None
        else:
            klass, timeline_id = ("worker" if parent is not None else "interactive"), None
        if klass == "interactive":
            timeline: Timeline | None = None
            if timeline_id is not None:
                found_row = db.execute(
                    "SELECT id, name, kind, display, length, repo_identity FROM timelines WHERE id = ?",
                    (timeline_id,),
                ).fetchone()
                generation = int(db.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()[0])
                timeline = Timeline(*found_row, generation=generation)
            else:
                timeline = current_timeline(db, cwd)
            if timeline is None:
                return None
            return view(db, timeline, config.view_bytes)
        if config.worker_view == "none":
            return None
        if config.worker_view == "view":
            timeline = current_timeline(db, cwd)
            if timeline is not None:
                return view(db, timeline, config.view_bytes)
        # A new worker is rarely indexed yet; the link just recorded still names its parent.
        named = (row[2], row[3], row[4]) if row is not None and row[2] else None
        if named is None and parent is not None:
            named = _indexed_parent(db, parent)
        return worker_header(named)


def run(host: str, event: str, payload: dict[str, Any], environment: Mapping[str, str], home: Path) -> str | None:
    """The additional context to print, or None."""

    deadline = time.monotonic() + HOOK_SECONDS
    config = load_config(home)
    found = adapter(host).hook_input(payload, event)
    store = Store.open(home)
    try:
        parent = None if found.subagent else parent_from_environment(host, found.session_id, environment)
        with store.write() as db:
            if parent is not None:
                db.execute(
                    "INSERT OR IGNORE INTO links (child_adapter, child_native, parent_adapter, "
                    "parent_native, via, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (host, found.session_id, parent[0], parent[1], parent[2], utc_now()),
                )
            if found.transcript is not None and _owning_label(config, found.transcript) is None:
                alarm(db, "location", str(found.transcript),
                      f"{host} hook named a transcript outside every configured source")
        with store.read() as db:
            injects = _injects(config, db, found.cwd or Path.cwd())
        if not injects or found.start in _NO_VIEW_STARTS:
            return None
        sync(store, config, deadline=deadline, first={(host, found.session_id)})
        return _context(store, config, host, found, parent, environment)
    finally:
        store.close()


def main(host: str, event: str, home: Path) -> int:
    if host not in HOSTS or event not in EVENTS:
        print(f"hist hook: unsupported {host} {event}", file=sys.stderr)
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        context = run(host, event, payload, os.environ, home)
    except (StoreBusy, StoreError, ValueError, OSError, sqlite3.Error) as error:
        print(f"hist hook: {type(error).__name__}: {error}; the agent continues without history",
              file=sys.stderr)
        return 0
    except Exception as error:  # noqa: BLE001 - a hook must never break the host
        print(f"hist hook: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return 0
    if context:
        try:
            print(json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
                ensure_ascii=False, separators=(",", ":"),
            ))
        except (BrokenPipeError, OSError):
            pass
    return 0
