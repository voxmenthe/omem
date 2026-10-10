"""`hist summarize BACKEND`: a `hist-summarize/1` summarizer over a free agent CLI.

`hist build` runs this as its `[summaries] command`: one request on stdin,
one `{"text": ..., "usage": {...}}` on stdout, failures on stderr with a
nonzero exit. The CLI runs headless in an empty scratch directory with none of
the user's rules, hooks or plugins, and with no tool it can run unasked:
Devin's are off where it allows and denied otherwise; OpenCode's must stay
listed for Zen's free tier, but need an approval a headless run cannot give.
So text in old transcripts has nothing to act through. A run must
make exactly one model request and call no tool; anything else is an error,
which stops the build. The agent's own system prompt still rides along:
Devin's adds about 6,500 tokens to every call.

Children stay in this process group, so the builder's group kill reaches any
straggler; this process enforces the request deadline itself, a little early,
to clean up first.
"""

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "hist-summarize/1"
MARGIN_SECONDS = 10
# Devin: off where the CLI allows; `skill` and the MCP tools stay listed, so
# MCP is denied and any tool call fails the run.
DEVIN_IMPORTS = ("agents_standard", "claude", "copilot", "cursor", "opencode", "windsurf", "zed")
DEVIN_TOOLS = (
    "ask_user_question", "browser_preview", "close_browser_preview", "edit", "exec", "fetch",
    "find_file_by_name", "get_output", "glob", "grep", "kill_shell", "notebook_edit",
    "notebook_read", "read", "request_scope", "task", "todo_write", "web_search", "webfetch",
    "write", "write_to_process",
)
DEVIN_DENY = ("read", "edit", "grep", "glob", "exec", "mcp__*", "Fetch(domain:*)")
OPENCODE_ENV = ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
                "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS")


class SummarizeError(RuntimeError):
    """The request or the agent run broke the contract."""


def prompt(request: dict[str, Any], with_system: bool) -> str:
    """The request as one user message, for CLIs that take no conversation."""

    parts = [request["system"]] if with_system else []
    if request["context"]:
        parts.append(request["context"])
    for turn in request["turns"]:
        if turn["role"] == "assistant":
            parts.append(f"<your-earlier-line>\n{turn['text']}\n</your-earlier-line>")
        else:
            parts.append(turn["text"])
    return "\n\n".join(parts)


def _seconds_left(request: dict[str, Any]) -> float:
    deadline = datetime.strptime(request["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    left = (deadline - datetime.now(timezone.utc)).total_seconds() - MARGIN_SECONDS
    if left <= 0:
        raise SummarizeError("the request deadline has passed")
    return left


def _tail(path: Path) -> str:
    return path.read_bytes()[-300:].decode(errors="replace").strip()


def _devin(request: dict[str, Any], model: str, root: Path) -> dict[str, Any]:
    user_path = Path.home() / ".config" / "devin" / "config.json"
    user = json.loads(user_path.read_text()) if user_path.exists() else {}
    config = {
        "version": user.get("version", 1),
        "read_config_from": {name: False for name in DEVIN_IMPORTS},
        "permissions": {"deny": list(DEVIN_DENY)},
        "disabled_tools": list(DEVIN_TOOLS),
        "subagents_enabled": False,
        "shell": {"setup_complete": True},
    }
    if "devin" in user:
        config["devin"] = user["devin"]
    (root / "config.json").write_text(json.dumps(config))
    (root / "prompt.txt").write_text(prompt(request, with_system=True))
    export, errors = root / "export.json", root / "stderr"
    command = [
        "devin", "--config", str(root / "config.json"), "--model", model,
        "--permission-mode", "auto", "--respect-workspace-trust", "false",
        "--prompt-file", str(root / "prompt.txt"), "--export", str(export), "-p",
    ]
    try:
        with errors.open("wb") as stderr:
            try:
                done = subprocess.run(
                    command, cwd=root / "work", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=stderr, timeout=_seconds_left(request), check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise SummarizeError("devin passed the request deadline") from error
        if done.returncode != 0:
            raise SummarizeError(f"devin exited {done.returncode}: {_tail(errors)}")
        session = json.loads(export.read_text())
    finally:
        _devin_forget(root / "work")
    steps = [step for step in session["steps"] if step["source"] == "agent"]
    if len(steps) != 1 or steps[0].get("tool_calls"):
        calls = sum(len(step.get("tool_calls") or []) for step in steps)
        raise SummarizeError(f"devin made {len(steps)} model requests and {calls} tool calls; want 1 and 0")
    totals = session["final_metrics"]
    return {
        "text": steps[0]["message"],
        "usage": {
            "input_tokens": totals["total_prompt_tokens"],
            "cached_input_tokens": totals["total_cached_tokens"],
            "output_tokens": totals["total_completion_tokens"],
        },
    }


def _devin_forget(work: Path) -> None:
    """Delete the run's Devin sessions, so summaries don't pile into the user's history.

    The directory is this run's alone, so every session listed there is the
    run's, including one the deadline cut off before its export was written.
    """

    quiet = {"stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 30}
    try:
        listed = subprocess.run(
            ["devin", "list", "--format", "json"], cwd=work, stdout=subprocess.PIPE, check=True, **quiet
        )
        for session in json.loads(listed.stdout):
            subprocess.run(
                ["devin", "rm", "--force", session["id"]], cwd=work, stdout=subprocess.DEVNULL, check=True, **quiet
            )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"hist summarize devin: could not delete this run's session in {work}: {error}", file=sys.stderr)


def _opencode(request: dict[str, Any], model: str, root: Path) -> dict[str, Any]:
    left = _seconds_left(request)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "plugin": [],
        "mcp": {},
        "instructions": [],
        # Zen's free tier refuses requests that offer no OpenCode tools, so the
        # tools stay listed but every call needs an approval no headless run
        # can give: OpenCode rejects it, and the run fails below.
        "permission": "ask",
        "agent": {
            "hist": {"mode": "primary", "prompt": request["system"], "permission": "ask"},
            "title": {"disable": True},
            "summary": {"disable": True},
            "compaction": {"disable": True},
        },
    }
    env = dict(os.environ, OPENCODE_CONFIG_CONTENT=json.dumps(config))
    env.update({name: "1" for name in OPENCODE_ENV})
    env.update({f"XDG_{kind}_HOME": str(root / kind.lower()) for kind in ("CONFIG", "DATA", "STATE")})
    errors = root / "stderr"
    # --standalone: a private server reading this config, never the user's shared service.
    command = [
        "opencode", "run", "--standalone", "--format", "json", "--agent", "hist", "--model", model,
        "--title", "hist", prompt(request, with_system=False),
    ]
    with errors.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=root / "work", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr
        )
        try:
            events = _opencode_events(process, time.monotonic() + left)
            exited = _exit_code(process) if not events or events[-1]["type"] == "text" else None
        finally:
            process.kill()
            process.wait()
    if not events:
        raise SummarizeError(f"opencode exited {exited} with no events: {_tail(errors)}")
    errored = [event for event in events if event["type"] == "error"]
    if errored:
        raise SummarizeError(f"opencode: {errored[0]['error'].get('message', errored[0]['error'])}")
    starts = sum(event["type"] == "step_start" for event in events)
    calls = sum(event["type"] == "tool_use" for event in events)
    finish = next((event["part"] for event in events if event["type"] == "step_finish"), None)
    if starts != 1 or calls or (finish or {}).get("reason") == "tool-calls":
        raise SummarizeError(f"opencode made {starts} model requests and {calls} tool calls; want 1 and 0")
    texts = [event["part"] for event in events if event["type"] == "text"]
    if not texts or any("end" not in part.get("time", {}) for part in texts):
        raise SummarizeError(f"opencode ended without a finished answer: {_tail(errors)}")
    # Some free models end the run before the step's finish, which carries usage.
    if finish is None and exited != 0:
        raise SummarizeError(f"opencode exited {exited} before finishing its step: {_tail(errors)}")
    tokens = finish["tokens"] if finish else None
    return {
        "text": "".join(part["text"] for part in texts),
        "usage": tokens and {
            "input_tokens": tokens["input"] + tokens["cache"]["read"],
            "cached_input_tokens": tokens["cache"]["read"],
            "output_tokens": tokens["output"] + tokens.get("reasoning", 0),
        },
    }


def _exit_code(process: subprocess.Popen[bytes]) -> int | None:
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return None


def _opencode_events(process: subprocess.Popen[bytes], end: float) -> list[dict[str, Any]]:
    """Events up to the first finished step or error, or the end of output.

    The CLI can hang after answering, so a finished step ends the read.
    """

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    events: list[dict[str, Any]] = []
    pending = b""
    while True:
        left = end - time.monotonic()
        if left <= 0:
            raise SummarizeError("opencode passed the request deadline")
        if not selector.select(left):
            continue
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            return events
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event)
            if event["type"] in ("step_finish", "error"):
                return events


BACKENDS: dict[str, tuple[Callable[[dict[str, Any], str, Path], dict[str, Any]], str]] = {
    "devin": (_devin, "swe-2-max"),
    "opencode": (_opencode, "opencode/muse-spark-1.3-contributor-free"),
}


def main(backend: str, model: str | None) -> int:
    run, default = BACKENDS[backend]
    try:
        request = json.load(sys.stdin)
        if request.get("protocol") != PROTOCOL:
            raise SummarizeError(f"expected a {PROTOCOL} request, got {request.get('protocol')!r}")
        with tempfile.TemporaryDirectory(prefix="hist-summarize-") as scratch:
            root = Path(scratch)
            (root / "work").mkdir()
            response = run(request, model or default, root)
    except (SummarizeError, OSError, ValueError, KeyError) as error:
        print(f"hist summarize {backend}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0
