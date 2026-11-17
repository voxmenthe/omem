"""`hist hook <host> <event>`: SessionStart and SubagentStart for Codex and
Claude Code; UserPromptSubmit for Kimi Code, which has no start hook.

The hook records a shell-launched worker's parent link and alarms for
transcripts outside every source. In repositories listed in `view.inject`,
it prints the indexed view for an interactive session or the short header
for a worker, reading only the supplied session's metadata when its class is
not indexed yet. Indexing and transcript verification belong to normal commands,
not startup hooks. Any failure prints a warning on stderr and nothing on stdout.

Kimi's hook runs before every prompt. A marker file under `HIST_HOME`, made
before anything else is read, lets only a session's first prompt through:
at most once, so a failed print loses that session's view and a retry cannot
double it. Kimi exports no session variables, so a variable inherited from
above Kimi would name the wrong session: the parent is the session that the
process walk (`caller.direct_parent`) finds just above the Kimi process.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .adapters import HookInput, adapter
from .caller import bounded, direct_parent
from .commands import current_timeline, repository_identity, view, worker_header
from .config import Config, load_config
from .render import Timeline
from .store import Store, StoreBusy, StoreError, utc_now
from .sync import alarm

# Each host's hook events, and how it reads a hook's stdout: a JSON envelope,
# or the context itself (Kimi would keep only a JSON reply's `message`).
_STARTS = frozenset({"SessionStart", "SubagentStart"})
HOSTS: dict[str, tuple[frozenset[str], str]] = {
    "codex": (_STARTS, "json"),
    "claude": (_STARTS, "json"),
    "kimi": (frozenset({"UserPromptSubmit"}), "text"),
}
# Variables a host sets for commands it runs, naming the running session.
_SESSION_VARIABLES = {"CODEX_THREAD_ID": "codex", "CLAUDE_CODE_SESSION_ID": "claude"}
_NO_VIEW_STARTS = frozenset({"resume", "fork"})
# What `ps`, `lsof`, and `git` may take of Kimi's hook timeout (3 s in
# integrations/kimi.toml): the first prompt's marker is already taken, so a
# killed hook loses the view.
DEADLINE_SECONDS = 1.5


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


def _injects(config: Config, db: sqlite3.Connection, cwd: Path, deadline: float | None = None) -> bool:
    if config.inject == "all":
        return True
    identity = repository_identity(db, cwd, deadline)
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
    deadline: float | None = None,
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
            if klass == "interactive" and host == "codex" and found.transcript is not None:
                try:
                    # Classify this session without locating or reading a fork parent's history.
                    head = adapter(host).head(found.transcript, lambda _parent: None)
                except FileNotFoundError:
                    head = None
                if head is not None:
                    klass = head.session_class
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
                timeline = current_timeline(db, cwd, deadline)
            if timeline is None:
                return None
            return view(db, timeline, config.view_bytes)
        if config.worker_view == "none":
            return None
        if config.worker_view == "view":
            timeline = current_timeline(db, cwd, deadline)
            if timeline is not None:
                return view(db, timeline, config.view_bytes)
        # A new worker is rarely indexed yet; the link just recorded still names its parent.
        named = (row[2], row[3], row[4]) if row is not None and row[2] else None
        if named is None and parent is not None:
            named = _indexed_parent(db, parent)
        return worker_header(named)


def _first_prompt(home: Path, session: str) -> bool:
    """Claim a Kimi session's first prompt: true once per session and store."""

    directory = home / "hook-state" / "kimi"
    # Not parents=True: a missing HIST_HOME fails here rather than being made.
    (home / "hook-state").mkdir(exist_ok=True)
    directory.mkdir(exist_ok=True)
    try:
        os.close(os.open(directory / session, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return False
    return True


def _prompt(payload: dict[str, Any], environment: Mapping[str, str], home: Path) -> str | None:
    """Kimi's UserPromptSubmit: the view or worker header, on a session's first prompt only."""

    if payload.get("is_steer") is True:
        return None
    found = adapter("kimi").hook_input(payload, "UserPromptSubmit")
    if not _first_prompt(home, found.session_id):
        return None
    config = load_config(home)
    store = Store.open(home)
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        process, open_files = bounded(deadline)
        caller = direct_parent(config.sources, "kimi", environ=environment, process=process,
                               open_files=open_files)
        parent = None if caller is None else (caller.adapter, caller.native_id, f"walk:{caller.rule}")
        with store.write() as db:
            if parent is not None:
                db.execute(
                    "INSERT OR IGNORE INTO links (child_adapter, child_native, parent_adapter, "
                    "parent_native, via, recorded_at) VALUES ('kimi', ?, ?, ?, ?, ?)",
                    (found.session_id, *parent, utc_now()),
                )
        with store.read() as db:
            # A record of the journal indexed already: resumed, or older than the hook. Kimi's
            # payload has no start source. hist's own start note (record -1) does not count:
            # Kimi writes the first prompt only after this hook runs.
            indexed = db.execute(
                "SELECT 1 FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.adapter = 'kimi' AND s.native_id = ? AND m.record >= 0 LIMIT 1",
                (found.session_id,),
            ).fetchone()
            if indexed is not None or not _injects(config, db, found.cwd or Path.cwd(), deadline):
                return None
        return _context(store, config, "kimi", found, parent, environment, deadline)
    finally:
        store.close()


def run(host: str, event: str, payload: dict[str, Any], environment: Mapping[str, str], home: Path) -> str | None:
    """The additional context to print, or None."""

    if event == "UserPromptSubmit":
        return _prompt(payload, environment, home)
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
        return _context(store, config, host, found, parent, environment)
    finally:
        store.close()


def main(host: str, event: str, home: Path) -> int:
    events, codec = HOSTS.get(host, (frozenset(), "json"))
    if event not in events:
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
            print(context if codec == "text" else json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
                ensure_ascii=False, separators=(",", ":"),
            ), flush=True)
        except (BrokenPipeError, OSError):
            # The reader is gone. What is left in the buffer goes nowhere, so the
            # interpreter's own flush at exit cannot fail and exit with status 120.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
