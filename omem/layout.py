"""Filesystem layout and repository identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .models import MemoryError, RepoIdentity, Scope

ROOT_ENV = "MEMORY_V0_DIR"
CODEX_NATIVE_MEMORY_SCOPE_ERROR = (
    "repo scope is unavailable from a Codex native-memory directory; "
    "run the command from the intended task repository"
)


def memory_root() -> Path:
    """Return the configured root without creating it."""

    configured = os.environ.get(ROOT_ENV)
    return (
        Path(os.path.abspath(Path(configured).expanduser()))
        if configured
        else Path.home() / ".memory-v0"
    )


def is_codex_native_memory_path(path: Path) -> bool:
    """Return whether path is in a recognized Codex-owned memory tree."""

    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name != "memories":
            continue
        parent = candidate.parent
        if parent.name == ".codex":
            return True
        if parent.parent.name == ".codex-state" and parent.name.startswith("bare"):
            return True
        if (
            parent.parent.name == "repos"
            and parent.parent.parent.name == ".codex-state"
        ):
            return True
    return False


def _git(args: list[str], cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def normalize_origin(value: str, base: Path | None = None) -> str:
    """Normalize common Git origin spellings without network access."""

    raw = value.strip()
    scp = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", raw)
    if scp and "://" not in raw:
        host, path = scp.groups()
        return _remote_identity(host, path)

    parsed = urlsplit(raw)
    if parsed.scheme == "file":
        return str(Path(unquote(parsed.path)).expanduser().resolve())
    if parsed.scheme and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        host = f"{parsed.hostname.lower()}{port}"
        return _remote_identity(host, unquote(parsed.path))
    if parsed.scheme:
        sanitized = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")
        )
        return sanitized.rstrip("/")
    local = Path(raw).expanduser()
    if not local.is_absolute() and base is not None:
        local = base / local
    return str(local.resolve())


def _remote_identity(host: str, path: str) -> str:
    clean_path = re.sub(r"/+", "/", path).strip("/")
    if clean_path.endswith(".git"):
        clean_path = clean_path[:-4]
    return f"{host.lower()}/{clean_path}"


def current_repo(cwd: Path | None = None) -> RepoIdentity | None:
    """Resolve a repo to origin identity, falling back to its real root."""

    here = (cwd or Path.cwd()).resolve()
    root_text = _git(["rev-parse", "--show-toplevel"], here)
    if root_text is None:
        return None
    repo_root = Path(root_text).resolve()
    origin = _git(["remote", "get-url", "origin"], repo_root)
    if origin:
        normalized = normalize_origin(origin, repo_root)
        key = f"remote:{normalized}"
        display = normalized
    else:
        normalized = str(repo_root)
        key = f"root:{normalized}"
        display = normalized
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", display).strip("-")
    slug = slug[-48:] or "repo"
    return RepoIdentity(
        key=key,
        display=display,
        store_id=f"{slug}-{digest}",
        root=str(repo_root),
        origin=origin,
    )


def scope_path(scope: Scope, repo: RepoIdentity | None = None) -> Path:
    """Map a public scope to its durable store."""

    root = memory_root()
    if scope == "self":
        return root / "self"
    resolved = repo or current_repo()
    if resolved is None:
        raise MemoryError("repo scope requires running inside a Git repository")
    if is_codex_native_memory_path(Path(resolved.root)):
        raise MemoryError(CODEX_NATIVE_MEMORY_SCOPE_ERROR)
    return root / "repos" / resolved.store_id


def secure_directory(path: Path) -> None:
    """Create a private directory and tighten existing permissions."""

    if path.is_symlink():
        raise MemoryError(f"refusing managed directory symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def secure_file(path: Path) -> None:
    """Tighten a store file after it has been created."""

    if path.is_symlink():
        raise MemoryError(f"refusing managed file symlink: {path}")
    path.chmod(0o600)


def atomic_write_json(path: Path, value: Any) -> None:
    """Durably replace a small JSON file in the same directory."""

    secure_directory(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(temp_path)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object or report its exact path."""

    if path.is_symlink():
        raise MemoryError(f"refusing managed JSON symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MemoryError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise MemoryError(f"expected a JSON object in {path}")
    return value


def initialize_layout(repo: RepoIdentity | None) -> tuple[Path, Path | None]:
    """Create the self store and, when available, the current repo store."""

    root = memory_root()
    secure_directory(root)
    secure_directory(root / "repos")
    self_path = scope_path("self")
    secure_directory(self_path)
    repo_path: Path | None = None
    if repo is not None:
        repo_path = scope_path("repo", repo)
        secure_directory(repo_path)
        atomic_write_json(
            repo_path / "identity.json",
            {
                "version": 1,
                "key": repo.key,
                "display": repo.display,
                "store_id": repo.store_id,
                "root_at_init": repo.root,
                "origin_at_init": repo.origin,
            },
        )
    return self_path, repo_path


def pending_paths(dream_id: str) -> list[Path]:
    """Find a pending dream without trusting the current working directory."""

    root = memory_root()
    candidates = [root / "self" / "dreams" / "pending" / f"{dream_id}.json"]
    repos = root / "repos"
    if repos.is_dir():
        candidates.extend(
            child / "dreams" / "pending" / f"{dream_id}.json"
            for child in repos.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    return [path for path in candidates if path.is_file()]
