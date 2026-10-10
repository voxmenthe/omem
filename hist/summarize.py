"""`hist summarize BACKEND`: a `hist-summarize/1` summarizer over an agent CLI or a model API.

`hist build` runs this as its `[summaries] command`: one request on stdin,
one `{"text": ..., "usage": {...}}` on stdout, failures on stderr with a
nonzero exit. A CLI runs headless in an empty scratch directory with none of
the user's settings, hooks, plugins or MCP servers, and with no tool it can run
unasked: Claude Code's and Codex's are off; Devin's are off where it allows and
denied otherwise; OpenCode's must stay listed for Zen's free tier, but need an
approval a headless run cannot give. So text in old transcripts has nothing to
act through. A run must make exactly one model request and call no tool;
anything else is an error, which stops the build. What the CLI always sends
still rides along: Devin's system prompt adds about 6,500 tokens to every call,
and Codex's 3,000 to 5,000, by model, plus the user's whole global AGENTS.md,
which no setting leaves out. An API backend sends one request that offers no tools.

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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "hist-summarize/1"
MARGIN_SECONDS = 10
API_MAX_TOKENS = 4096  # a line is at most 512 bytes; Anthropic requires a cap
# Codex: every default-on feature that offers a tool or touches the user's Codex
# state. With these off and web search disabled, the model lists no tools. An
# unknown name stops Codex with an error rather than being skipped.
CODEX_OFF = (
    "apps", "browser_use", "browser_use_external", "collaboration_modes", "computer_use", "goals",
    "hooks", "image_generation", "in_app_browser", "memories", "multi_agent", "multi_agent_v2",
    "plugins", "request_permissions_tool", "shell_tool", "skill_search", "sleep_tool", "tool_search",
    "tool_suggest", "unified_exec", "view_image",
)
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


def messages(request: dict[str, Any]) -> list[dict[str, str]]:
    """The request's turns as API messages, the context leading the first."""

    turns = [{"role": turn["role"], "content": turn["text"]} for turn in request["turns"]]
    if request["context"]:
        turns[0]["content"] = f"{request['context']}\n\n{turns[0]['content']}"
    return turns


def _seconds_left(request: dict[str, Any]) -> float:
    deadline = datetime.strptime(request["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    left = (deadline - datetime.now(timezone.utc)).total_seconds() - MARGIN_SECONDS
    if left <= 0:
        raise SummarizeError("the request deadline has passed")
    return left


def _tail(path: Path) -> str:
    return path.read_bytes()[-300:].decode(errors="replace").strip()


def _run(name: str, command: list[str], request: dict[str, Any], root: Path) -> tuple[int, bytes]:
    """Run `command` in the scratch directory with the prompt on stdin: exit code, stdout.

    Files, not pipes: a grandchild holding a pipe open would keep the wait
    going past the deadline.
    """

    (root / "prompt.txt").write_text(prompt(request, with_system=False))
    out = root / "stdout"
    with (root / "prompt.txt").open("rb") as given, out.open("wb") as stdout, (root / "stderr").open("wb") as stderr:
        try:
            done = subprocess.run(
                command, cwd=root / "work", stdin=given, stdout=stdout, stderr=stderr,
                timeout=_seconds_left(request), check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SummarizeError(f"{name} passed the request deadline") from error
    return done.returncode, out.read_bytes()


def _claude(request: dict[str, Any], model: str, root: Path) -> dict[str, Any]:
    # No setting source: the user's hooks, permissions and env stay out. The
    # request's system prompt replaces Claude Code's, which leaves out CLAUDE.md.
    code, stdout = _run("claude", [
        "claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--system-prompt", request["system"],
    ], request, root)
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    result = next((event for event in events if event["type"] == "result"), None)
    if result is None:
        raise SummarizeError(f"claude exited {code} without a result: {_tail(root / 'stderr')}")
    if result["is_error"]:
        raise SummarizeError(f"claude: {result.get('result') or result.get('subtype')}")
    calls = sum(
        block["type"] == "tool_use"
        for event in events if event["type"] == "assistant"
        for block in event["message"]["content"]
    )
    if result["num_turns"] != 1 or calls:
        raise SummarizeError(f"claude made {result['num_turns']} model requests and {calls} tool calls; want 1 and 0")
    usage = result["usage"]
    cached = usage.get("cache_read_input_tokens", 0)
    return {
        "text": result["result"],
        "usage": {
            "input_tokens": usage["input_tokens"] + usage.get("cache_creation_input_tokens", 0) + cached,
            "cached_input_tokens": cached,
            "output_tokens": usage["output_tokens"],
        },
    }


def _codex(request: dict[str, Any], model: str, root: Path, effort: str | None = None) -> dict[str, Any]:
    # Codex reports no request count: one message and no other item stand in for
    # it. It reports ignored settings as items too, so a renamed one fails here.
    (root / "system.md").write_text(request["system"])
    code, stdout = _run("codex", [
        "codex", "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "--skip-git-repo-check", "--sandbox", "read-only", "--model", model,
        *(["-c", f"model_reasoning_effort={json.dumps(effort)}"] if effort else []),
        "-c", f"model_instructions_file={json.dumps(str(root / 'system.md'))}", "-c", 'web_search="disabled"',
        *(flag for name in CODEX_OFF for flag in ("--disable", name)), "-",
    ], request, root)
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    failed = [event for event in events if event["type"] in ("error", "turn.failed")]
    if failed:
        raise SummarizeError(f"codex: {failed[-1].get('message') or failed[-1]['error']['message']}")
    if code != 0:
        raise SummarizeError(f"codex exited {code}: {_tail(root / 'stderr')}")
    items = [event["item"] for event in events if event["type"] == "item.completed"]
    said = [item["text"] for item in items if item["type"] == "agent_message"]
    other = [item for item in items if item["type"] not in ("agent_message", "reasoning")]
    if len(said) != 1 or other:
        detail = "".join(f"; {item['type']}: {item.get('message', '')}" for item in other)
        raise SummarizeError(f"codex wrote {len(said)} messages and {len(other)} other items; want 1 and 0{detail}")
    finished = [event["usage"] for event in events if event["type"] == "turn.completed"]
    if len(finished) != 1:
        raise SummarizeError(f"codex finished {len(finished)} turns; want 1")
    usage = finished[0]
    return {
        "text": said[0],
        "usage": {
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }


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


def _post(url: str, headers: dict[str, str], body: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    call = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(call, timeout=_seconds_left(request)) as answer:
            reply = json.load(answer)
    except urllib.error.HTTPError as error:
        raise SummarizeError(f"{url} answered {error.code}: {error.read()[:300].decode(errors='replace')}") from error
    except TimeoutError as error:
        raise SummarizeError(f"{url} passed the request deadline") from error
    # Some OpenAI-compatible services report errors in a 200 answer.
    if reply.get("error"):
        raise SummarizeError(f"{url} answered an error: {reply['error']}")
    return reply


def _anthropic(request: dict[str, Any], model: str, base_url: str, key: str | None) -> dict[str, Any]:
    reply = _post(
        f"{base_url}/v1/messages",
        {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})},
        {"model": model, "max_tokens": API_MAX_TOKENS, "system": request["system"], "messages": messages(request)},
        request,
    )
    text = "".join(block["text"] for block in reply["content"] if block["type"] == "text")
    if not text:
        raise SummarizeError(f"{model} answered no text (stop reason {reply.get('stop_reason')})")
    usage = reply.get("usage")
    cached = (usage or {}).get("cache_read_input_tokens") or 0
    return {
        "text": text,
        "usage": usage and {
            "input_tokens": usage["input_tokens"] + (usage.get("cache_creation_input_tokens") or 0) + cached,
            "cached_input_tokens": cached,
            "output_tokens": usage["output_tokens"],
        },
    }


def _openai(
    request: dict[str, Any], model: str, base_url: str, key: str | None, effort: str | None = None
) -> dict[str, Any]:
    reply = _post(
        f"{base_url}/chat/completions",
        {"authorization": f"Bearer {key}"} if key else {},
        {
            "model": model,
            "messages": [{"role": "system", "content": request["system"]}, *messages(request)],
            **({"reasoning_effort": effort} if effort else {}),
        },
        request,
    )
    choice = reply["choices"][0]
    text = choice["message"].get("content")
    if not text or choice["message"].get("tool_calls"):
        raise SummarizeError(f"{model} answered no text (finish reason {choice.get('finish_reason')})")
    usage = reply.get("usage")
    return {
        "text": text,
        "usage": usage and {
            "input_tokens": usage["prompt_tokens"],
            "cached_input_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
            "output_tokens": usage["completion_tokens"],
        },
    }


# Each CLI runs in its own scratch directory and is given a model.
CLIS: dict[str, tuple[Callable[[dict[str, Any], str, Path], dict[str, Any]], str]] = {
    "claude": (_claude, "claude-haiku-5-5"),
    "codex": (_codex, "gpt-6-luna"),
    "devin": (_devin, "swe-2-max"),
    "opencode": (_opencode, "opencode/muse-spark-1.3-contributor-free"),
}
# Each API takes a model, a base URL, and the environment variable holding its key.
APIS: dict[str, tuple[Callable[[dict[str, Any], str, str, str | None], dict[str, Any]], str, str, str]] = {
    "anthropic": (_anthropic, "claude-haiku-5-5", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "openai": (_openai, "gpt-6-luna", "https://api.openai.com/v1", "OPENAI_API_KEY"),
}
# The backends that take --effort, with their default model's effort. A --model
# without --effort runs at that model's own default, which suits any model.
EFFORTS = {"codex": "medium", "openai": "medium"}
BACKENDS = sorted([*CLIS, *APIS])


def main(
    backend: str, model: str | None, base_url: str | None = None, key_env: str | None = None,
    effort: str | None = None,
) -> int:
    try:
        if backend in CLIS and (base_url or key_env):
            raise SummarizeError(f"--base-url and --key-env apply only to {' and '.join(APIS)}")
        if effort and backend not in EFFORTS:
            raise SummarizeError(f"--effort applies only to {' and '.join(EFFORTS)}")
        effort = effort or (EFFORTS.get(backend) if model is None else None)
        options = {"effort": effort} if effort else {}
        request = json.load(sys.stdin)
        if request.get("protocol") != PROTOCOL:
            raise SummarizeError(f"expected a {PROTOCOL} request, got {request.get('protocol')!r}")
        if backend in APIS:
            call, default, url, env = APIS[backend]
            # A default key goes only to its own service; elsewhere, only a key named for it.
            name = key_env or (env if base_url is None else None)
            key = os.environ.get(name) if name else None
            response = call(request, model or default, (base_url or url).rstrip("/"), key, **options)
        else:
            run, default = CLIS[backend]
            with tempfile.TemporaryDirectory(prefix="hist-summarize-") as scratch:
                root = Path(scratch)
                (root / "work").mkdir()
                response = run(request, model or default, root, **options)
    except (SummarizeError, OSError, ValueError, KeyError) as error:
        print(f"hist summarize {backend}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0
