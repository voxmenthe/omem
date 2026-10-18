"""Strict `hist` configuration: explicit sources, discovery, view, summaries.

Transcript locations come only from this file, never from the environment,
so coverage never depends on which launcher started the caller. `HIST_HOME`
relocates the whole store and its configuration, for isolation and tests.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

HOME_ENV = "HIST_HOME"
CONFIG_NAME = "config.toml"
ADAPTERS = frozenset({"antigravity", "codex", "claude", "commandcode", "devin", "opencode"})
LABEL = re.compile(r"[a-z][a-z0-9-]{0,31}")
WorkerView = Literal["header", "view", "none"]
# Header plus 32 all-reference roots: the irreducible view of any timeline.
MIN_VIEW_BYTES = 4096


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


@dataclass(frozen=True)
class Source:
    adapter: str
    label: str
    roots: tuple[Path, ...]


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...]
    watch: tuple[str, ...]
    exclude: tuple[str, ...]
    view_bytes: int
    worker_view: WorkerView
    inject: tuple[str, ...] | Literal["all"]
    summary_command: tuple[str, ...] | None
    summary_timelines: tuple[str, ...]
    max_concurrency: int
    max_attempts_per_day: int


def hist_home(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    raw = env.get(HOME_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".hist"


def _table(raw: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where} must be a list of non-empty strings")
    return tuple(value)


def _integer(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer of at least {minimum}")
    return value


def _sources(raw: Any) -> tuple[Source, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("at least one [[sources]] entry is required")
    sources: list[Source] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("each [[sources]] entry must be a table")
        unknown = set(entry) - {"adapter", "label", "roots"}
        missing = {"adapter", "label", "roots"} - set(entry)
        if unknown or missing:
            raise ConfigError(
                f"[[sources]] needs exactly adapter, label, roots; "
                f"unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        adapter, label = entry["adapter"], entry["label"]
        if adapter not in ADAPTERS:
            raise ConfigError(f"unknown adapter {adapter!r}; known: {sorted(ADAPTERS)}")
        if not isinstance(label, str) or not LABEL.fullmatch(label):
            raise ConfigError(f"source label {label!r} must be a lowercase slug")
        roots = tuple(
            Path(root).expanduser() for root in _strings(entry["roots"], "roots")
        )
        for root in roots:
            if not root.is_absolute():
                raise ConfigError(f"source root {root} must be absolute")
        sources.append(Source(adapter, label, roots))
    labels = [source.label for source in sources]
    if len(set(labels)) != len(labels):
        raise ConfigError("source labels must be unique")
    claimed = [(root, source.label) for source in sources for root in source.roots]
    for k, (root, label) in enumerate(claimed):
        for other, other_label in claimed[k + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise ConfigError(
                    f"overlapping roots: {root} ({label}) and {other} ({other_label})"
                )
    return tuple(sources)


def parse_config(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML: {error}") from error
    unknown = set(raw) - {"sources", "discovery", "view", "summaries"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    discovery = _table(raw, "discovery", {"watch", "exclude"})
    view = _table(raw, "view", {"bytes", "worker", "inject"})
    summaries = _table(
        raw,
        "summaries",
        {"command", "timelines", "max_concurrency", "max_attempts_per_day"},
    )
    worker = view.get("worker", "header")
    if worker not in ("header", "view", "none"):
        raise ConfigError("view.worker must be header, view, or none")
    inject_raw = view.get("inject", [])
    inject: tuple[str, ...] | Literal["all"] = (
        "all" if inject_raw == "all" else _strings(inject_raw, "view.inject")
    )
    command = summaries.get("command")
    return Config(
        sources=_sources(raw.get("sources")),
        watch=_strings(discovery.get("watch", []), "discovery.watch"),
        exclude=_strings(discovery.get("exclude", []), "discovery.exclude"),
        view_bytes=_integer(view.get("bytes", 8000), "view.bytes", MIN_VIEW_BYTES),
        worker_view=worker,
        inject=inject,
        summary_command=None if command is None else _strings(command, "summaries.command"),
        summary_timelines=_strings(summaries.get("timelines", []), "summaries.timelines"),
        max_concurrency=_integer(summaries.get("max_concurrency", 4), "summaries.max_concurrency", 1),
        max_attempts_per_day=_integer(
            summaries.get("max_attempts_per_day", 2000), "summaries.max_attempts_per_day", 0
        ),
    )


def load_config(home: Path) -> Config:
    path = home / CONFIG_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(
            f"no configuration at {path}; copy hist/config.example.toml there"
        ) from error
    return parse_config(text)
