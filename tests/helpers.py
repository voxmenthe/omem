"""Shared test helpers."""

import os
import subprocess
import sys
from pathlib import Path

from omem.layout import secure_directory
from omem.store import MemoryStore

PROJECT = Path(__file__).resolve().parents[1]
MEMORY = PROJECT / "memory"


def run_memory(
    root: Path,
    *args: str,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["MEMORY_V0_DIR"] = str(root)
    return subprocess.run(
        [sys.executable, str(MEMORY), *args],
        cwd=cwd or PROJECT,
        env=environment,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
    )


def make_legacy_store(path: Path, scope: str) -> MemoryStore:
    """Build a legacy-format store (raw LOG.txt, no observation log)."""

    store = MemoryStore(path, scope)  # type: ignore[arg-type]
    secure_directory(path)
    secure_directory(store.tree_path)
    secure_directory(path / "dreams" / "pending")
    secure_directory(path / "dreams" / "generations")
    store.log_path.touch(mode=0o600)
    store.lock_path.touch(mode=0o600)
    return store


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Memory Test")
    git(path, "config", "user.email", "memory@example.invalid")
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    return path
