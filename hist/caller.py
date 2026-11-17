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
- a process named kimi or kimi-code: nobody. Kimi exports no session id and
  holds no journal open, so nothing names the agent running the command.

Only when the walk finds no host, `CLAUDE_CODE_SESSION_ID` or
`CODEX_THREAD_ID`; with both set, nobody, as either may be inherited.
"""

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Source

MAX_LEVELS = 8
KIMI_NAMES = frozenset({"kimi", "kimi-code"})
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


def ps_process(pid: int, timeout: float = 2) -> tuple[int, str] | None:
    """A process's parent pid and command name, or None if it is gone."""

    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=timeout, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parent, _, name = out.partition(" ")
    return (int(parent), name.strip()) if parent.isdigit() else None


def lsof_files(pid: int, timeout: float = 5) -> list[str]:
    try:
        out = subprocess.run(
            ["lsof", "-Fn", "-p", str(pid)], capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


def bounded(
    deadline: float,
    *,
    process: Callable[..., tuple[int, str] | None] = ps_process,
    open_files: Callable[..., list[str]] = lsof_files,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Process, OpenFiles]:
    """`ps` and `lsof` that end by `deadline` (`clock`'s time). Past it, a
    process looks gone and holds no files, so a walk ends with nobody named
    rather than late; a host that kills a slow hook gets its answer in time."""

    def bounded_process(pid: int) -> tuple[int, str] | None:
        left = deadline - clock()
        return process(pid, timeout=left) if left > 0 else None

    def bounded_files(pid: int) -> list[str]:
        left = deadline - clock()
        return open_files(pid, timeout=left) if left > 0 else []

    return bounded_process, bounded_files


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


def hosts(
    sources: Iterable[Source],
    *,
    pid: int | None = None,
    environ: Mapping[str, str] = os.environ,
    process: Process = ps_process,
    open_files: OpenFiles = lsof_files,
) -> Iterator[tuple[str, Caller | None]]:
    """Each host process among the ancestors, nearest first: its host, and the
    session it runs when exact evidence names one."""

    # Claude Code keeps sessions/ beside the projects/ root a source names.
    sources = list(sources)
    claude_dirs = {Path(root).parent / "sessions" for root in _roots(sources, "claude")}
    codex_roots = _roots(sources, "codex")
    current = os.getppid() if pid is None else pid
    for _ in range(MAX_LEVELS):
        if current <= 1:
            return
        claude = None
        for directory in sorted(claude_dirs):
            claude = _claude_session(directory / f"{current}.json")
            if claude is not None:
                yield "claude", Caller("claude", claude, "claude-sessions")
                break
        info = process(current)
        if info is None:
            return
        parent, name = info
        base = os.path.basename(name).lower()
        if claude is None and base in KIMI_NAMES:
            yield "kimi", None
        elif claude is None and "codex" in base:
            if environ.get("CODEX_THREAD_ID"):
                yield "codex", Caller("codex", environ["CODEX_THREAD_ID"], "codex-env")
            else:
                threads = {
                    match.group(1)
                    for path in open_files(current)
                    if any(os.path.realpath(path).startswith(root + os.sep) for root in codex_roots)
                    and (match := _ROLLOUT.search(path))
                }
                yield "codex", Caller("codex", threads.pop(), "codex-rollout") if len(threads) == 1 else None
        current = parent


def detect(
    sources: Iterable[Source],
    *,
    pid: int | None = None,
    environ: Mapping[str, str] = os.environ,
    process: Process = ps_process,
    open_files: OpenFiles = lsof_files,
) -> Caller | None:
    for _, found in hosts(sources, pid=pid, environ=environ, process=process, open_files=open_files):
        return found
    claude, codex = environ.get("CLAUDE_CODE_SESSION_ID"), environ.get("CODEX_THREAD_ID")
    if claude and codex:
        return None
    if claude:
        return Caller("claude", claude, "claude-env")
    if codex:
        return Caller("codex", codex, "codex-env")
    return None


def direct_parent(
    sources: Iterable[Source],
    host: str,
    *,
    pid: int | None = None,
    environ: Mapping[str, str] = os.environ,
    process: Process = ps_process,
    open_files: OpenFiles = lsof_files,
) -> Caller | None:
    """The session that started the nearest `host` process above this one: the
    session the next host up runs, by the walk's rules. For Claude, then Kimi
    A, then Kimi B, that host is A, which names nobody; the environment
    fallback would name Claude, so it never decides."""

    found = hosts(sources, pid=pid, environ=environ, process=process, open_files=open_files)
    nearest = next(found, None)
    if nearest is None or nearest[0] != host:
        return None
    above = next(found, None)
    return above[1] if above is not None else None
