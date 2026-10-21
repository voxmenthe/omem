"""Weekly rates of how agents use `hist`, computed from the `hist` store itself.

The session-history plan's M2 tracks these instead of running a trial now.
Each is counted per ISO week (Monday, UTC) of the shell call:

  calls      shell calls that run a `hist` command
  find       of those, `hist find`
  hits       find calls whose output lists at least one hit
  self       find calls with hits where every hit is from the calling
             session (find leaves the caller out since R1, so this falls
             to zero; `noted` and `rerun` take over)
  noted      find calls whose output says the calling session was left out
  rerun      find calls with --include-self or --session self
  head_cut   find calls piped to `head` whose output reached head's line
             limit; an upper bound on cut continuations
  grep       hist calls whose output is piped to grep or rg
  bad_flag   hist calls rejected for an unknown or malformed argument
  stale      hist calls whose output warned that the index may be behind
  raw_grep   grep or rg over raw transcripts (Claude projects, Codex sessions)

A call is paired with its output by order: the k-th of a run of
consecutive calls with the k-th output after the run. Hosts that run
several commands in one call (Codex `exec`) count once per call.

Usage: uv run python scripts/hist_rates.py [--home ~/.hist] [--since 2026-09-28]
The store is opened read-only.
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

SHELL_TOOLS = (
    "Bash", "bash", "exec", "exec_command", "shell", "local_shell", "run_command", "execute",
    "run_shell_command", "shell_command",
)
COLUMNS = ("calls", "find", "hits", "self", "noted", "rerun", "head_cut", "grep", "bad_flag", "stale", "raw_grep")
_HIST = re.compile(
    r"(?:^|[\s;&|(`\"'])hist\s+(find|view|zoom|sessions|read|timelines|status|sync|verify|refresh|build|backup)\b"
    r"([^\n;&|]*)((?:\|[^\n;&]*)*)"
)
_HEAD = re.compile(r"\|\s*head(?:\s+-n\s*(\d+)|\s+-(\d+))?(?=\s|$|\|)")
_GREP = re.compile(r"\|\s*(?:grep|rg|egrep)\b")
_RAW = re.compile(r"\b(?:grep|rg|egrep|zgrep)\b[^\n|;&]*(?:\.claude/projects|\.codex[\w-]*/(?:archived_)?sessions|rollout-[^\s]*\.jsonl)")
_HIT = re.compile(r"^[\w.~-]+:\d+\+1\|\[s:([0-9a-z]+) ", re.MULTILINE)


def command_text(tool: str, text: str) -> str:
    """The shell command a call ran, or the call's whole text when it is code."""

    try:
        value = json.loads(text)
    except ValueError:
        return text
    if not isinstance(value, dict):
        return text
    command = value.get("command", value.get("cmd", ""))
    return " ".join(map(str, command)) if isinstance(command, list) else str(command)


def week(stamp: str) -> str:
    day = datetime.date.fromisoformat(stamp[:10])
    return (day - datetime.timedelta(days=day.weekday())).isoformat()


def output_of(db: sqlite3.Connection, session: int, position: int) -> str:
    """The output paired with the call at `position` in its session."""

    before = db.execute(
        "SELECT kind FROM messages WHERE session_id = ? AND position < ? ORDER BY position DESC LIMIT 30",
        (session, position),
    ).fetchall()
    index = next((n for n, (kind,) in enumerate(before) if kind != "tool_call"), len(before))
    after = db.execute(
        "SELECT kind, text FROM messages WHERE session_id = ? AND position > ? ORDER BY position LIMIT 60",
        (session, position),
    ).fetchall()
    results = [text for kind, text in after if kind == "tool_result"]
    return results[index] if index < len(results) else ""


def rates(db: sqlite3.Connection, since: str) -> dict[str, Counter[str]]:
    weeks: dict[str, Counter[str]] = {}
    marks = ",".join("?" * len(SHELL_TOOLS))
    rows = db.execute(
        "SELECT m.session_id, m.position, m.time, m.tool, m.text, x.alias FROM messages m "
        "JOIN sessions x ON x.id = m.session_id "
        f"WHERE m.kind = 'tool_call' AND m.tool IN ({marks}) AND m.time >= ? "
        "AND (instr(m.text, 'hist ') > 0 OR instr(m.text, 'grep') > 0 OR instr(m.text, 'rg ') > 0)",
        [*SHELL_TOOLS, since],
    ).fetchall()
    for session, position, stamp, tool, text, alias in rows:
        command = command_text(tool, text)
        counts = weeks.setdefault(week(stamp), Counter())
        if _RAW.search(command):
            counts["raw_grep"] += 1
        calls = list(_HIST.finditer(command))
        if not calls:
            continue
        out = output_of(db, session, position)
        counts["calls"] += 1
        if any(_GREP.search(call.group(3)) for call in calls):
            counts["grep"] += 1
        if "unrecognized arguments" in out or re.search(r"^hist \w+: error:", out, re.MULTILINE):
            counts["bad_flag"] += 1
        if out.startswith("(index may be behind"):
            counts["stale"] += 1
        finds = [call for call in calls if call.group(1) == "find"]
        if not finds:
            continue
        counts["find"] += 1
        hits = _HIT.findall(out)
        if hits:
            counts["hits"] += 1
            if set(hits) == {alias}:
                counts["self"] += 1
        if "(left out this session" in out:
            counts["noted"] += 1
        if any("--include-self" in call.group(2) or "--session self" in call.group(2) for call in finds):
            counts["rerun"] += 1
        for call in finds:
            head = _HEAD.search(call.group(3))
            if head is not None:
                limit = int(head.group(1) or head.group(2) or 10)
                if len(out.splitlines()) >= limit:
                    counts["head_cut"] += 1
                break
    return weeks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=os.environ.get("HIST_HOME", "~/.hist"))
    parser.add_argument("--since", default="2026-09-01", help="first day counted (UTC)")
    args = parser.parse_args()
    path = Path(args.home).expanduser() / "hist.db"
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    weeks = rates(db, args.since)
    print("week        " + " ".join(f"{name:>8}" for name in COLUMNS))
    for start in sorted(weeks):
        print(f"{start}  " + " ".join(f"{weeks[start][name]:>8}" for name in COLUMNS))


if __name__ == "__main__":
    main()
