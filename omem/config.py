"""Strict root configuration boundary: storage routing and consolidation policy.

This module owns the optional root `config.toml` ($MEMORY_V0_DIR/config.toml
or ~/.memory-v0/config.toml), the memory root resolution it lives under, the
host identity that labels a session and selects a storage namespace, and the
hosts trusted to consolidate. Storage routing, consolidation policy, and host
trust are validated independently: a semantic `[consolidation]` or `[hosts]`
error never blocks storage routing, and no storage error is silently
downgraded to the pooled default.

Nothing here creates a root, writes a file, or moves data.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from .hosts import HOST_SLUG
from .models import (
    ConsolidationError,
    ConsolidationPolicy,
    MemoryError,
    PRUNING_PROFILES,
)

ROOT_ENV = "MEMORY_V0_DIR"
HOST_ENV = "OMEM_HOST"
CONFIG_NAME = "config.toml"

StorageMode = Literal["pooled", "per-host"]
Host = str

STORAGE_MODES: Final = ("pooled", "per-host")
TRUST_KEY: Final = "consolidation_trusted"

ITEM_BUDGET: Final = {"self": 128, "repo": 128}
WAKE_ITEM_BUDGET: Final = {"self": 16, "repo": 24}
DEFAULT_POLICY: Final = ConsolidationPolicy(
    catalog_pruning="off", self_catalog_target=16, repo_catalog_target=24
)


class ConfigError(MemoryError):
    """The root configuration is unusable for storage routing."""


def memory_root(environment: Mapping[str, str] | None = None) -> Path:
    """Return the configured root without creating it."""

    env = os.environ if environment is None else environment
    configured = env.get(ROOT_ENV)
    return (
        Path(os.path.abspath(Path(configured).expanduser()))
        if configured
        else Path.home() / ".memory-v0"
    )


def config_path(environment: Mapping[str, str] | None = None) -> Path:
    return memory_root(environment) / CONFIG_NAME


def parse_host(value: object) -> Host:
    """Return the validated host slug or reject the value before any path use."""

    if isinstance(value, str) and HOST_SLUG.fullmatch(value):
        return value
    raise ConfigError(
        f"invalid OMem host {value!r}; expected a lowercase slug such as "
        "codex, claude, devin, or kimi"
    )


def _read_toml(
    environment: Mapping[str, str] | None,
) -> tuple[Path, dict[str, Any] | None]:
    """Return (path, data); data is None when the file is absent."""

    path = config_path(environment)
    if not path.exists():
        return path, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot parse OMem config {path}: {error}") from error
    return path, data


def _reject_unknown_sections(path: Path, data: dict[str, Any]) -> None:
    unknown = set(data) - {"storage", "consolidation", "hosts"}
    if unknown:
        raise ConfigError(
            f"{path} has unknown sections: {', '.join(sorted(unknown))}; "
            "expected [storage], [consolidation], and/or [hosts]"
        )


def load_storage_mode(
    environment: Mapping[str, str] | None = None,
) -> StorageMode:
    """Resolve the storage mode; an absent file or section means pooled.

    A malformed file, an unknown section, or an invalid [storage] section
    raises ConfigError instead of guessing pooled: this value chooses which
    namespace of memories a command may read or write.
    """

    path, data = _read_toml(environment)
    if data is None:
        return "pooled"
    _reject_unknown_sections(path, data)
    section = data.get("storage")
    if section is None:
        return "pooled"
    if not isinstance(section, dict):
        raise ConfigError(f"{path} [storage] must be a table")
    unknown = set(section) - {"mode"}
    if unknown:
        raise ConfigError(
            f"{path} has unknown storage keys: {', '.join(sorted(unknown))}"
        )
    if "mode" not in section:
        return "pooled"
    mode = section["mode"]
    if mode not in STORAGE_MODES:
        allowed = ", ".join(STORAGE_MODES)
        raise ConfigError(f"{path} storage.mode must be one of: {allowed}")
    return mode  # type: ignore[return-value]


def fetch_policy(
    environment: Mapping[str, str] | None = None,
) -> ConsolidationPolicy:
    """Load the optional consolidation policy with its documented defaults.

    Storage routing problems surface here too (as ConsolidationError) because
    a request/apply must not proceed against a config it cannot fully read,
    but a semantic [consolidation] error is the only one callers must treat
    as consolidation-specific.
    """

    try:
        path, data = _read_toml(environment)
        if data is None:
            return DEFAULT_POLICY
        _reject_unknown_sections(path, data)
    except ConfigError as error:
        raise ConsolidationError(str(error)) from error
    section = data.get("consolidation")
    if section is None:
        return DEFAULT_POLICY
    if not isinstance(section, dict):
        raise ConsolidationError(f"{path} [consolidation] must be a table")
    unknown = set(section) - {
        "catalog_pruning",
        "self_catalog_target",
        "repo_catalog_target",
    }
    if unknown:
        raise ConsolidationError(
            f"{path} has unknown consolidation keys: {', '.join(sorted(unknown))}"
        )
    profile = section.get("catalog_pruning", "off")
    if not isinstance(profile, str) or profile not in PRUNING_PROFILES:
        allowed = ", ".join(PRUNING_PROFILES)
        raise ConsolidationError(
            f"{path} catalog_pruning must be one of: {allowed}"
        )
    targets: dict[str, int] = {}
    for key, scope, default in (
        ("self_catalog_target", "self", DEFAULT_POLICY.self_catalog_target),
        ("repo_catalog_target", "repo", DEFAULT_POLICY.repo_catalog_target),
    ):
        value = section.get(key, default)
        if type(value) is not int or not 1 <= value <= ITEM_BUDGET[scope]:
            raise ConsolidationError(
                f"{path} {key} must be an integer between 1 and "
                f"{ITEM_BUDGET[scope]}"
            )
        targets[key] = value
    return ConsolidationPolicy(
        catalog_pruning=profile,  # type: ignore[arg-type]
        self_catalog_target=targets["self_catalog_target"],
        repo_catalog_target=targets["repo_catalog_target"],
    )


def load_consolidation_trust(
    environment: Mapping[str, str] | None = None,
) -> frozenset[str] | None:
    """Return the hosts allowed to consolidate, or None when unrestricted.

    An absent file, section, or key leaves every host (and an unbound host)
    free to consolidate. A present list is exact: an empty list lets nobody
    consolidate until the file changes.
    """

    path, data = _read_toml(environment)
    if data is None:
        return None
    _reject_unknown_sections(path, data)
    section = data.get("hosts")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"{path} [hosts] must be a table")
    unknown = set(section) - {TRUST_KEY}
    if unknown:
        raise ConfigError(
            f"{path} has unknown hosts keys: {', '.join(sorted(unknown))}"
        )
    if TRUST_KEY not in section:
        return None
    raw = section[TRUST_KEY]
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and HOST_SLUG.fullmatch(item) for item in raw
    ):
        raise ConfigError(
            f"{path} hosts.{TRUST_KEY} must be an array of lowercase host slugs"
        )
    return frozenset(raw)


def consolidation_refusal(
    host: Host | None, trusted: frozenset[str] | None
) -> str | None:
    """Explain why this host may not consolidate, or None when it may."""

    if trusted is None or (host is not None and host in trusted):
        return None
    allowed = ", ".join(sorted(trusted)) or "none"
    if host is None:
        return (
            f"consolidation requires a trusted host (trusted: {allowed}); "
            f"set {HOST_ENV} in the host's own settings or pass --host <host>"
        )
    return (
        f"host {host} is not trusted to consolidate (trusted: {allowed}); "
        "leave due maintenance for a trusted host"
    )
