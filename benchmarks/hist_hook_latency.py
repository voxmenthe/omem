#!/usr/bin/env python3
"""Measure `hist hook` latency, view included, on a synthetic million-message timeline.

One real session is indexed through `hist sync`, so the repository's identity,
timeline, session, and source rows come from production code. The timeline is
then extended in bulk with structurally valid synthetic sessions and messages.
Search rows are omitted: the hook never searches. Each trial runs the real
`hist hook claude SessionStart` command as a subprocess, as a host would.

With --real-roots, the sources are this machine's Codex and Claude Code roots,
so each hook also pays real discovery; transcripts written during the run are
indexed into the temporary store, which is deleted afterwards.
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.hist.fixtures import Codex, git_repo, write_jsonl  # noqa: E402

SESSION_MESSAGES = 200
KINDS = [
    ("user", None, "please look at the failing guard in module {n} and fix the path check"),
    ("tool_call", "exec_command", '{{"cmd": "rg -n guard src/module_{n}.py"}}'),
    ("tool_result", "exec_command", "src/module_{n}.py:12: def guard(path):\n" + "    context line\n" * 60),
    ("agent", None, "The guard in module {n} compared unresolved paths; I resolved both sides and the "
                    "tests pass. " * 3),
]
REAL_ROOTS = {
    "codex": ["~/.codex", "~/.codex-state"],
    "codex2": ["~/.codex2", "~/.codex-state2"],
    "claude": ["~/.claude/projects"],
}


def _config(home: Path, roots: dict[str, list[str]]) -> None:
    sources = "".join(
        f'[[sources]]\nadapter = "{"claude" if label == "claude" else "codex"}"\n'
        f'label = "{label}"\nroots = {json.dumps([str(Path(r).expanduser()) for r in paths])}\n\n'
        for label, paths in roots.items()
        if any(Path(r).expanduser().is_dir() for r in paths)
    )
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(sources + '[view]\ninject = ["bench"]\n')


def _hist(home: Path, cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "CLAUDE_CODE_", "HIST_"))}
    environment.update({"HIST_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)})
    result = subprocess.run(
        [sys.executable, "-m", "hist", *args], cwd=cwd, input=stdin, capture_output=True,
        text=True, env=environment,
    )
    if result.returncode != 0 or result.stderr:
        raise SystemExit(f"hist {' '.join(args)} exited {result.returncode}: {result.stderr}")
    return result


def _extend(db_path: Path, total: int) -> None:
    """Grow the only repository timeline to `total` messages."""

    db = sqlite3.connect(db_path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    timeline, length = db.execute(
        "SELECT id, length FROM timelines WHERE kind = 'repository'"
    ).fetchone()
    template = db.execute(
        "SELECT adapter, label, repo_identity, cwd, cli_version FROM sessions WHERE timeline_id = ?",
        (timeline,),
    ).fetchone()
    position = length
    number = 0
    epoch = time.time() - total * 20
    while position < total:
        number += 1
        native = f"bench-{number:08x}"
        count = min(SESSION_MESSAGES, total - position)
        first_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch + position * 20))
        last_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch + (position + count - 1) * 20))
        session = db.execute(
            "INSERT INTO sessions (adapter, native_id, label, class, alias, timeline_id, repo_identity, "
            "cwd, cli_version, started, first_time, last_time, first_position, last_position, "
            "message_count) VALUES (?, ?, ?, 'interactive', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (template[0], native, template[1], f"b{number:07x}", timeline, template[2], template[3],
             template[4], first_time, first_time, last_time, position, position + count - 1, count),
        ).lastrowid
        source = db.execute(
            "INSERT INTO sources (adapter, native_id, label, path, token, cursor, session_id, state) "
            "VALUES (?, ?, ?, ?, '', '{}', ?, 'active')",
            (template[0], native, template[1], f"/nonexistent/{native}.jsonl", session),
        ).lastrowid
        rows = []
        for k in range(count):
            kind, tool, text = KINDS[k % len(KINDS)]
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch + (position + k) * 20))
            rows.append((timeline, position + k, source, k, 0, session, kind, tool, stamp,
                         text.format(n=number)))
        db.executemany(
            "INSERT INTO messages (timeline_id, position, source_id, record, part, session_id, kind, "
            "tool, time, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        position += count
    db.execute("UPDATE timelines SET length = ?, last_time = ? WHERE id = ?", (position, last_time, timeline))
    db.execute("COMMIT")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3),
        "max": round(ordered[-1], 3),
    }


def _trials(home: Path, repo: Path, count: int) -> dict[str, object]:
    hook, view, sizes = [], [], set()
    for trial in range(count):
        payload = json.dumps({
            "session_id": f"6c000000-0000-4000-8000-{trial:012x}", "cwd": str(repo),
            "hook_event_name": "SessionStart", "source": "startup",
            "transcript_path": str(Path.home() / ".claude" / "projects" / "bench" / f"{trial}.jsonl"),
        })
        started = time.perf_counter()
        out = _hist(home, repo, "hook", "claude", "SessionStart", stdin=payload).stdout
        hook.append(time.perf_counter() - started)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        sizes.add(len(context.encode()))
        started = time.perf_counter()
        _hist(home, repo, "view")
        view.append(time.perf_counter() - started)
    return {"hook_s": _percentiles(hook), "view_s": _percentiles(view), "view_bytes": sorted(sizes)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=1_000_000)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--real-roots", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw).resolve()
        home = base / "home"
        repo = git_repo(base / "bench", "git@github.com:bench/bench.git")
        codex = base / ".codex"
        codex.mkdir()
        roots = {"bench": [str(codex)], **(REAL_ROOTS if args.real_roots else {})}
        _config(home, roots)
        _hist(home, repo, "status")
        thread = "019a0000-0000-7000-8000-00000000be01"
        write_jsonl(Codex.path(codex, thread), [
            Codex.meta(thread, str(repo), "cli", "https://github.com/bench/bench.git"),
            Codex.user("the first real message", 1),
        ])
        _hist(home, repo, "sync")
        started = time.perf_counter()
        _extend(home / "hist.db", args.messages)
        build = time.perf_counter() - started
        result = {
            "messages": args.messages,
            "real_roots": args.real_roots,
            "store_mb": round(sum(p.stat().st_size for p in home.glob("hist.db*")) / 1e6),
            "extend_s": round(build, 1),
            **_trials(home, repo, args.trials),
        }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
