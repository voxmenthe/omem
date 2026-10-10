# /// script
# requires-python = ">=3.12"
# ///
"""Merge OMem's and hist's hook definitions into Claude Code and Codex settings.

    uv run scripts/install_hooks.py [memory] [hist] [--apply]

The definitions come from this checkout's integration files. Claude Code's
settings live in $CLAUDE_CONFIG_DIR (default ~/.claude) and Codex's in
$CODEX_HOME (default ~/.codex); run once per Codex home. Without --apply this
only reports. Handlers already present, matched by command, are left alone, so
reruns are safe. A file that cannot be merged exactly is left untouched.
"""

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent
FRAGMENTS = {
    "memory": {"claude": "integrations/claude/settings.json", "codex": "integrations/codex/hooks.toml"},
    "hist": {"claude": "hist/integrations/claude.json", "codex": "hist/integrations/codex.toml"},
}
_HEADER = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(#.*)?$")
_ENV_TABLE = re.compile(r"^\[shell_environment_policy\.set\][ \t]*(#.*)?$", re.MULTILINE)


class MergeError(Exception):
    """The settings file cannot take the definitions without a manual merge."""


def settings_path(host: str) -> Path:
    if host == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "settings.json"
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"


def _commands(groups: object, where: str) -> set[str]:
    if not isinstance(groups, list) or not all(
        isinstance(group, dict) and isinstance(group.get("hooks"), list) for group in groups
    ):
        raise MergeError(f"{where} is not a list of hook groups")
    return {hook.get("command") for group in groups for hook in group["hooks"]}


def _describe(event: str, group: dict) -> str:
    matcher = f" ({group['matcher']})" if "matcher" in group else ""
    return f"{event}{matcher}: " + ", ".join(hook["command"] for hook in group["hooks"])


def merge_claude(text: str, fragment: dict) -> tuple[str, list[str]]:
    settings = json.loads(text) if text.strip() else {}
    if not isinstance(settings, dict):
        raise MergeError("settings are not a JSON object")
    changes = []
    for key, value in fragment.get("env", {}).items():
        current = settings.get("env", {}).get(key)
        if current not in (None, value):
            raise MergeError(f"env {key} is {current!r}; the hooks need {value!r}")
        if current is None:
            settings.setdefault("env", {})[key] = value
            changes.append(f"env {key}={value}")
    for event, groups in fragment["hooks"].items():
        present = _commands(settings.get("hooks", {}).get(event, []), f"hooks.{event}")
        for group in groups:
            if present.isdisjoint(hook["command"] for hook in group["hooks"]):
                settings.setdefault("hooks", {}).setdefault(event, []).append(group)
                changes.append(_describe(event, group))
    return json.dumps(settings, indent=2) + "\n", changes


def _hook_groups(fragment_text: str) -> list[tuple[str, str]]:
    """(event, TOML text) for each [[hooks.<event>]] group of a fragment, in order."""
    groups: list[tuple[str, list[str]]] = []
    inside = False
    for line in fragment_text.splitlines():
        match = _HEADER.match(line)
        if match:
            name = match.group(1).strip().split(".")
            inside = name[0] == "hooks"
            if inside and len(name) == 2:
                groups.append((name[1], []))
        if inside:
            groups[-1][1].append(line)
    return [(event, "\n".join(lines).strip() + "\n") for event, lines in groups]


def merge_codex(text: str, fragment_text: str) -> tuple[str, list[str]]:
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MergeError(f"config.toml does not parse: {error}") from error
    expected = copy.deepcopy(current)
    changes, env_lines, appended = [], [], []
    for key, value in tomllib.loads(fragment_text).get("shell_environment_policy", {}).get("set", {}).items():
        existing = current.get("shell_environment_policy", {}).get("set", {}).get(key)
        if existing not in (None, value):
            raise MergeError(f"shell_environment_policy.set.{key} is {existing!r}; the hooks need {value!r}")
        if existing is None:
            expected.setdefault("shell_environment_policy", {}).setdefault("set", {})[key] = value
            env_lines.append(f"{key} = {json.dumps(value)}\n")
            changes.append(f"env {key}={value}")
    for event, group_text in _hook_groups(fragment_text):
        group = tomllib.loads(group_text)["hooks"][event][0]
        present = _commands(current.get("hooks", {}).get(event, []), f"hooks.{event}")
        if present.isdisjoint(hook["command"] for hook in group["hooks"]):
            expected.setdefault("hooks", {}).setdefault(event, []).append(group)
            appended.append(group_text)
            changes.append(_describe(event, group))
    if not changes:
        return text, []
    merged = text
    if env_lines:
        table = _ENV_TABLE.search(merged)
        if table:
            merged = merged[: table.end()] + "\n" + "".join(env_lines).rstrip("\n") + merged[table.end() :]
        else:
            appended.insert(0, "[shell_environment_policy.set]\n" + "".join(env_lines))
    if appended:
        merged = merged.rstrip("\n") + ("\n\n" if merged.strip() else "") + "\n".join(appended)
    try:
        result = tomllib.loads(merged)
    except tomllib.TOMLDecodeError as error:
        raise MergeError(f"appending would not parse ({error})") from error
    if result != expected:
        raise MergeError("appending would change existing settings")
    return merged, changes


def _write(path: Path, text: str) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.before-hooks-{time.strftime('%Y%m%dT%H%M%S')}")
        shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(text)
    if backup:
        shutil.copymode(backup, handle.name)
    os.replace(handle.name, path)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tools", nargs="*", metavar="memory|hist", help="default: both")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    args = parser.parse_args(argv)
    unknown = set(args.tools) - FRAGMENTS.keys()
    if unknown:
        parser.error(f"unknown tool: {', '.join(sorted(unknown))}")
    tools = args.tools or list(FRAGMENTS)
    found, failed, unwritten = 0, 0, False
    for host, merge in (("claude", merge_claude), ("codex", merge_codex)):
        path = settings_path(host)
        if not path.parent.is_dir():
            print(f"{host}: no {path.parent}; skipped")
            continue
        found += 1
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        changes = []
        try:
            for tool in tools:
                fragment_text = (CHECKOUT / FRAGMENTS[tool][host]).read_text(encoding="utf-8")
                text, added = merge(text, json.loads(fragment_text) if host == "claude" else fragment_text)
                changes += added
        except (MergeError, json.JSONDecodeError) as error:
            failed += 1
            print(f"{host}: {path}: {error}; nothing written. Merge by hand from:")
            print("".join(f"  {CHECKOUT / FRAGMENTS[tool][host]}\n" for tool in tools), end="")
            continue
        print(f"{host}: {path}")
        print("".join(f"  + {change}\n" for change in changes) or "  already installed\n", end="")
        if not changes:
            continue
        if not args.apply:
            unwritten = True
            continue
        backup = _write(path, text)
        print("  written" + (f"; previous copy: {backup}" if backup else ""))
        if host == "codex":
            print('  Codex runs new hooks only once trusted: at "Hooks need review" trust them, or use /hooks.')
    if not found:
        print("No Claude Code or Codex settings directory found.", file=sys.stderr)
        return 1
    if unwritten:
        print("Nothing written yet; rerun with --apply.")
    if "hist" in tools:
        config = Path(os.environ.get("HIST_HOME") or "~/.hist").expanduser() / "config.toml"
        if not config.exists():
            print(f"hist has no configuration at {config} yet; the README Quickstart creates it.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
