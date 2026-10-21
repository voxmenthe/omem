"""The agent session a `hist` command runs inside, by exact evidence only.

`find` leaves the caller's own session out by default: its search and
the output it gets back are indexed within about a second, so otherwise
an agent mostly finds itself. A wrong guess would hide someone else's
live session, so nothing is guessed from the directory or recent activity.
The rules follow flopwire's `caller.go`. Walking the ancestors nearest-first,
at most eight levels up from the parent process, the first host found wins:

- a process with a Claude Code session file, `<claude dir>/sessions/<pid>.json`:
  its `sessionId`, which follows `/clear` where the environment does not;
- a process named codex: `CODEX_THREAD_ID`, or else the one rollout file the
  process holds open under a Codex root. If neither names a thread, nobody:
  climbing further would find the session that launched Codex.

Only when the walk finds no host, `CLAUDE_CODE_SESSION_ID` or
`CODEX_THREAD_ID`; with both set, nobody, as either may be inherited.
"""

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Source

MAX_LEVELS = 8
_ROLLOUT = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

Process = Callable[[int], tuple[int, str] | None]
OpenFiles = Callable[[int], list[str]]


@dataclass(frozen=True)
class Caller:
    adapter: str
    native_id: str
    rule: str


def ps_process(pid: int) -> tuple[int, str] | None:
    """A process's parent pid and command name, or None if it is gone."""

    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parent, _, name = out.partition(" ")
    return (int(parent), name.strip()) if parent.isdigit() else None


def lsof_files(pid: int) -> list[str]:
    try:
        out = subprocess.run(
            ["lsof", "-Fn", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


def _claude_session(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("sessionId")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _roots(sources: Iterable[Source], adapter: str) -> list[str]:
    return [
        os.path.realpath(root) for source in sources if source.adapter == adapter for root in source.roots
    ]


def detect(
    sources: Iterable[Source],
    *,
    pid: int | None = None,
    environ: Mapping[str, str] = os.environ,
    process: Process = ps_process,
    open_files: OpenFiles = lsof_files,
) -> Caller | None:
    # Claude Code keeps sessions/ beside the projects/ root a source names.
    sources = list(sources)
    claude_dirs = {Path(root).parent / "sessions" for root in _roots(sources, "claude")}
    codex_roots = _roots(sources, "codex")
    current = os.getppid() if pid is None else pid
    for _ in range(MAX_LEVELS):
        if current <= 1:
            break
        for directory in sorted(claude_dirs):
            found = _claude_session(directory / f"{current}.json")
            if found is not None:
                return Caller("claude", found, "claude-sessions")
        info = process(current)
        if info is None:
            break
        parent, name = info
        if "codex" in os.path.basename(name).lower():
            if environ.get("CODEX_THREAD_ID"):
                return Caller("codex", environ["CODEX_THREAD_ID"], "codex-env")
            threads = {
                match.group(1)
                for path in open_files(current)
                if any(os.path.realpath(path).startswith(root + os.sep) for root in codex_roots)
                and (match := _ROLLOUT.search(path))
            }
            return Caller("codex", threads.pop(), "codex-rollout") if len(threads) == 1 else None
        current = parent
    claude, codex = environ.get("CLAUDE_CODE_SESSION_ID"), environ.get("CODEX_THREAD_ID")
    if claude and codex:
        return None
    if claude:
        return Caller("claude", claude, "claude-env")
    if codex:
        return Caller("codex", codex, "codex-env")
    return None
