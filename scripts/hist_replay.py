"""Replay past `hist find` queries and `hist zoom` references against the store.

A regression and latency set for the session-history plan's M2: every
query agents ran is searched again in-process, in the scope it ran in
(its session's timeline unless it said --all or named one), and every
reference they zoomed is opened again. Nothing is synced or written.

Reported: how many replays succeed, latency percentiles, and the queries
that once found hits and now find none in the same scope (`find` leaves no
caller out here, so a lost hit is a real regression).

Usage: uv run python scripts/hist_replay.py [--home ~/.hist] [--show 10]
"""

import argparse
import os
import re
import shlex
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hist import commands  # noqa: E402
from hist_rates import _HIST, _HIT, SHELL_TOOLS, command_text, output_of  # noqa: E402

# `2>`, `>`, `2>&1`, `>/tmp/out`; a bare operator takes the next word as its target.
_REDIRECT = re.compile(r"\d*[<>]+&?\d*")


def _arguments(text: str) -> list[str] | None:
    """The command's arguments without shell redirections; None when a
    shell variable or unbalanced quote makes them unknowable."""

    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    args: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif _REDIRECT.match(token):
            skip = _REDIRECT.fullmatch(token) is not None
        elif "$" in token:
            return None
        else:
            args.append(token)
    return args


def replay(db: sqlite3.Connection, show: int) -> None:
    marks = ",".join("?" * len(SHELL_TOOLS))
    rows = db.execute(
        "SELECT m.session_id, m.position, m.tool, m.text, m.timeline_id FROM messages m "
        f"WHERE m.kind = 'tool_call' AND m.tool IN ({marks}) "
        "AND (instr(m.text, 'hist find') > 0 OR instr(m.text, 'hist zoom') > 0)",
        list(SHELL_TOOLS),
    ).fetchall()
    finds: list[float] = []
    zooms: list[float] = []
    failed: list[str] = []
    lost: list[str] = []
    slow: list[tuple[float, str]] = []
    for session, position, tool, text, timeline_id in rows:
        calls = list(_HIST.finditer(command_text(tool, text)))
        old_hits = None
        for call in calls:
            args = _arguments(call.group(2))
            if not args:
                continue
            started = time.perf_counter()
            try:
                if call.group(1) == "find":
                    positional = [arg for arg in args if not arg.startswith("-")]
                    if "--all" in args:
                        scope = None
                    elif len(positional) > 1:
                        scope = commands.timeline_named(db, positional[1].partition("~")[0])
                    else:
                        row = db.execute(
                            f"SELECT {commands._TIMELINE_COLUMNS} FROM timelines WHERE id = ?", (timeline_id,)
                        ).fetchone()
                        scope = commands._timeline(db, row)
                    out = commands.find(db, positional[0], scope, None)
                    finds.append(time.perf_counter() - started)
                    slow.append((finds[-1], call.group(0).strip()[:100]))
                    if old_hits is None:
                        old_hits = len(_HIT.findall(output_of(db, session, position)))
                    if old_hits and not _HIT.findall(out):
                        lost.append(f"{call.group(0).strip()[:120]}")
                elif call.group(1) == "zoom":
                    timeline, start, size = commands.parse_ref(db, args[0], Path.cwd())
                    if size is None:
                        commands.zoom_message(db, timeline, start, 1)
                    else:
                        commands.zoom(db, timeline, start, size, 1)
                    zooms.append(time.perf_counter() - started)
            except (commands.UsageError, IndexError) as error:
                failed.append(f"{call.group(0).strip()[:100]}: {error}")
    for name, times in (("find", finds), ("zoom", zooms)):
        if times:
            cuts = statistics.quantiles(times, n=20) if len(times) > 1 else times * 19
            print(f"{name}: {len(times)} replayed; median {statistics.median(times) * 1000:.0f} ms, "
                  f"p95 {cuts[18] * 1000:.0f} ms, max {max(times) * 1000:.0f} ms")
    for seconds, line in sorted(slow, reverse=True)[: min(show, 5)]:
        print(f"  slowest find {seconds * 1000:.0f} ms: {line}")
    print(f"refused: {len(failed)} (a usage error now, such as a reference to a message not yet indexed)")
    for line in failed[:show]:
        print(f"  {line}")
    print(f"lost: {len(lost)} queries found hits then and none now in the same scope")
    for line in lost[:show]:
        print(f"  {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=os.environ.get("HIST_HOME", "~/.hist"))
    parser.add_argument("--show", type=int, default=10, help="examples to list per finding")
    args = parser.parse_args()
    path = Path(args.home).expanduser() / "hist.db"
    replay(sqlite3.connect(f"file:{path}?mode=ro", uri=True), args.show)


if __name__ == "__main__":
    main()
