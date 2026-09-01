"""Filesystem layout and repository identity."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import (
    HOSTS,
    HOST_ENV,
    ROOT_ENV,
    Host,
    StorageMode,
    config_path,
    load_storage_mode,
    memory_root,
    parse_host,
)
from .models import MemoryError, RepoIdentity, Scope

__all__ = [
    "CODEX_NATIVE_MEMORY_SCOPE_ERROR",
    "ROOT_ENV",
    "Host",
    "StoreContext",
    "atomic_write_json",
    "canonical_digest",
    "config_path",
    "current_repo",
    "default_context",
    "initialize_layout",
    "is_codex_native_memory_path",
    "memory_root",
    "normalize_origin",
    "pending_consolidation_paths",
    "pending_paths",
    "read_json",
    "resolve_store_context",
    "scope_path",
    "scope_path_in",
    "secure_directory",
    "secure_file",
]


class _GitDeadlineExpired(Exception):
    """Internal signal that repository identity could not finish in time."""


CODEX_NATIVE_MEMORY_SCOPE_ERROR = (
    "repo scope is unavailable from a Codex native-memory directory; "
    "run the command from the intended task repository"
)


@dataclass(frozen=True)
class StoreContext:
    """One immutable storage decision resolved once per invocation.

    Host identity selects a namespace before any scope path is built; it is
    never a record field, a semantic scope, or a path fragment outside the
    validated allow-list.
    """

    config_root: Path
    store_root: Path
    mode: StorageMode
    host: Host | None


def resolve_store_context(
    explicit_host: str | None,
    environment: Mapping[str, str] | None = None,
) -> StoreContext:
    """Resolve mode plus host into one store root, failing before I/O.

    A supplied host is validated even in pooled mode, an explicit host that
    disagrees with OMEM_HOST is rejected, and per-host mode without a host
    fails before any path is constructed. Invalid storage configuration is
    never downgraded to the pooled default.
    """

    env = os.environ if environment is None else environment
    env_raw = env.get(HOST_ENV)
    env_host = None if env_raw is None else parse_host(env_raw)
    host: Host | None = None
    if explicit_host is not None:
        host = parse_host(explicit_host)
        if env_host is not None and env_host != host:
            raise MemoryError(
                f"--host {explicit_host} conflicts with {HOST_ENV}={env_raw}"
            )
    else:
        host = env_host
    config_root = memory_root(env)
    mode = load_storage_mode(env)
    if mode == "per-host":
        if host is None:
            raise MemoryError(
                f"per-host storage requires a host: pass --host <{'|'.join(HOSTS)}> "
                f"or set {HOST_ENV} inside the host's own settings"
            )
        store_root = config_root / "hosts" / host
    else:
        store_root = config_root
    return StoreContext(
        config_root=config_root, store_root=store_root, mode=mode, host=host
    )


def default_context(environment: Mapping[str, str] | None = None) -> StoreContext:
    """Resolve a context from the process environment alone."""

    return resolve_store_context(None, environment)


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


def _git(
    args: list[str],
    cwd: Path,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    timeout: float | None = None
    if deadline is not None:
        timeout = deadline - clock()
        if timeout <= 0:
            raise _GitDeadlineExpired
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise _GitDeadlineExpired from error
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def canonical_digest(value: Any) -> str:
    """SHA-256 over the canonical compact JSON encoding of a value."""

    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def current_repo(
    cwd: Path | None = None,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RepoIdentity | None:
    """Resolve a repo to origin identity, falling back to its real root."""

    if deadline is not None and clock() >= deadline:
        return None
    here = (cwd or Path.cwd()).resolve()
    try:
        root_text = _git(
            ["rev-parse", "--show-toplevel"],
            here,
            deadline=deadline,
            clock=clock,
        )
        if root_text is None:
            return None
        repo_root = Path(root_text).resolve()
        origin = _git(
            ["remote", "get-url", "origin"],
            repo_root,
            deadline=deadline,
            clock=clock,
        )
    except _GitDeadlineExpired:
        return None
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


def scope_path_in(
    root: Path, scope: Scope, repo: RepoIdentity | None = None
) -> Path:
    """Map a public scope to its durable store below an explicit root."""

    if scope == "self":
        return root / "self"
    resolved = repo or current_repo()
    if resolved is None:
        raise MemoryError("repo scope requires running inside a Git repository")
    if is_codex_native_memory_path(Path(resolved.root)):
        raise MemoryError(CODEX_NATIVE_MEMORY_SCOPE_ERROR)
    return root / "repos" / resolved.store_id


def scope_path(
    scope: Scope,
    repo: RepoIdentity | None = None,
    context: StoreContext | None = None,
) -> Path:
    """Map a public scope to its durable store in the selected namespace."""

    resolved = context if context is not None else default_context()
    return scope_path_in(resolved.store_root, scope, repo)


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


def initialize_layout(
    repo: RepoIdentity | None,
    context: StoreContext | None = None,
) -> tuple[Path, Path | None]:
    """Create the self store and, when available, the current repo store.

    Only the selected namespace is created; the other host namespace and any
    inactive pooled tree are left untouched.
    """

    resolved = context if context is not None else default_context()
    root = resolved.store_root
    secure_directory(root)
    secure_directory(root / "repos")
    self_path = scope_path_in(root, "self")
    secure_directory(self_path)
    repo_path: Path | None = None
    if repo is not None:
        repo_path = scope_path_in(root, "repo", repo)
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


def pending_paths(
    dream_id: str, context: StoreContext | None = None
) -> list[Path]:
    """Find a pending dream without trusting the current working directory."""

    root = (
        context if context is not None else default_context()
    ).store_root
    candidates = [root / "self" / "dreams" / "pending" / f"{dream_id}.json"]
    repos = root / "repos"
    if repos.is_dir():
        candidates.extend(
            child / "dreams" / "pending" / f"{dream_id}.json"
            for child in repos.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    return [path for path in candidates if path.is_file()]


def pending_consolidation_paths(
    request_id: str,
    *,
    staged: bool = False,
    context: StoreContext | None = None,
) -> list[Path]:
    """Find a pending consolidation request inside the selected namespace."""

    def pending_dir(store: Path) -> Path:
        base = store / "consolidations"
        return base / "migration" / "pending" if staged else base / "pending"

    root = (
        context if context is not None else default_context()
    ).store_root
    candidates = [pending_dir(root / "self") / f"{request_id}.json"]
    repos = root / "repos"
    if repos.is_dir():
        candidates.extend(
            pending_dir(child) / f"{request_id}.json"
            for child in repos.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    return [path for path in candidates if path.is_file()]
