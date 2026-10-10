"""`hist summarize` against fake agent CLIs on PATH and a fake API server."""

import json
import os
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hist.summarize import CODEX_OFF
from hist.summarize import MARGIN_SECONDS as MARGIN
from tests.hist.fixtures import HistCase

# The fakes log what they were given; FAKE_MODE selects how the run goes.
FAKE_DEVIN = textwrap.dedent('''
    import json, os, sys, time
    args = sys.argv[1:]
    path = os.environ["FAKE_LOG"]
    calls = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
    log = open(path, "a")
    if args[0] == "list":
        print(json.dumps([{"id": c["created"]} for c in calls if c.get("created") and c["cwd"] == os.getcwd()]))
        sys.exit(0)
    if args[0] == "rm":
        log.write(json.dumps({"rm": args[1:], "cwd": os.getcwd()}) + "\\n")
        sys.exit(0)
    flag = lambda name: args[args.index(name) + 1]
    log.write(json.dumps({
        "args": args, "cwd": os.getcwd(), "entries": os.listdir("."),
        "config": json.load(open(flag("--config"))), "prompt": open(flag("--prompt-file")).read(),
    }) + "\\n")
    mode = os.environ.get("FAKE_MODE", "")
    if mode == "fail":
        print("weekly quota exhausted", file=sys.stderr)
        sys.exit(2)
    log.write(json.dumps({"created": "fake-session", "cwd": os.getcwd()}) + "\\n")
    log.flush()
    if mode == "slow":
        time.sleep(60)
    steps = [{"source": "user", "message": "..."},
             {"source": "agent", "message": "user: asked for X", "tool_calls": []}]
    if mode == "tools":
        steps[-1]["tool_calls"] = [{"function_name": "skill"}]
        steps.append({"source": "agent", "message": "again", "tool_calls": []})
    json.dump({"session_id": "fake-session", "steps": steps, "final_metrics": {
        "total_prompt_tokens": 7000, "total_completion_tokens": 40, "total_cached_tokens": 0,
    }}, open(flag("--export"), "w"))
''')
FAKE_OPENCODE = textwrap.dedent('''
    import json, os, sys, time
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({
        "args": sys.argv[1:], "cwd": os.getcwd(),
        "config": json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]),
        "xdg": [os.environ[f"XDG_{kind}_HOME"] for kind in ("CONFIG", "DATA", "STATE")],
    }) + "\\n")
    log.close()
    mode = os.environ.get("FAKE_MODE", "")
    emit = lambda kind, **fields: print(json.dumps({"type": kind, **fields}), flush=True)
    if mode == "crash":
        print("Failed to start server. Is port 49374 in use?", file=sys.stderr)
        sys.exit(1)
    if mode == "limited":
        emit("error", error={"message": "Rate limit exceeded. Please try again later."})
        sys.exit(0)
    emit("step_start", part={"type": "step-start"})
    if mode == "tool":
        emit("tool_use", part={"state": {"status": "error"}})
    emit("text", part={"type": "text", "text": "user: asked for X", "time": {"start": 1, "end": 2}})
    if mode == "unreported":
        sys.exit(0)
    emit("step_finish", part={"reason": "stop", "tokens": {
        "input": 900, "output": 30, "reasoning": 10, "cache": {"read": 100, "write": 0},
    }})
    time.sleep(60)
''')
FAKE_CLAUDE = textwrap.dedent('''
    import json, os, sys
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({
        "args": sys.argv[1:], "cwd": os.getcwd(), "entries": os.listdir("."), "stdin": sys.stdin.read(),
    }) + "\\n")
    log.close()
    mode = os.environ.get("FAKE_MODE", "")
    emit = lambda kind, **fields: print(json.dumps({"type": kind, **fields}), flush=True)
    if mode == "crash":
        print("Not logged in. Please run /login", file=sys.stderr)
        sys.exit(1)
    emit("system", subtype="init", tools=[])
    content = [{"type": "text", "text": "user: asked for X"}]
    if mode == "tools":
        content.insert(0, {"type": "tool_use", "name": "Read", "input": {}})
    emit("assistant", message={"content": content})
    if mode == "fail":
        emit("result", subtype="success", is_error=True, num_turns=1, result="There's an issue with the selected model")
        sys.exit(1)
    emit("result", subtype="success", is_error=False, num_turns=2 if mode == "tools" else 1, result="user: asked for X",
         usage={"input_tokens": 2, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 100, "output_tokens": 30})
''')
FAKE_CODEX = textwrap.dedent('''
    import json, os, sys
    args = sys.argv[1:]
    settings = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
    instructions = next(value for value in settings if value.startswith("model_instructions_file="))
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({
        "args": args, "cwd": os.getcwd(), "entries": os.listdir("."), "stdin": sys.stdin.read(),
        "system": open(json.loads(instructions.split("=", 1)[1])).read(),
    }) + "\\n")
    log.close()
    mode = os.environ.get("FAKE_MODE", "")
    emit = lambda kind, **fields: print(json.dumps({"type": kind, **fields}), flush=True)
    if mode == "crash":
        print("Error: Unknown feature flag: goals", file=sys.stderr)
        sys.exit(1)
    emit("thread.started", thread_id="t-1")
    emit("turn.started")
    if mode == "failed":
        emit("error", message="The model is not supported")
        emit("turn.failed", error={"message": "The model is not supported"})
        sys.exit(1)
    if mode == "ignored":
        emit("item.completed", item={"id": "item_0", "type": "error", "message": "`goals` is ignored."})
    if mode == "tool":
        emit("item.completed", item={"id": "item_1", "type": "command_execution", "command": "ls"})
    emit("item.completed", item={"id": "item_2", "type": "agent_message", "text": "user: asked for X"})
    emit("turn.completed", usage={
        "input_tokens": 6200, "cached_input_tokens": 1800, "output_tokens": 40, "reasoning_output_tokens": 20,
    })
''')
EXPECTED_MESSAGES = [
    {"role": "user", "content": "<chat>\nproj:0|earlier line\n</chat>\n\nCompaction: compress message 1"},
    {"role": "assistant", "content": "a line that was too long"},
    {"role": "user", "content": "Too long: write it again"},
]


def request(seconds: int = 120) -> dict:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return {
        "protocol": "hist-summarize/1",
        "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "system": "SYSTEM RULES",
        "context": "<chat>\nproj:0|earlier line\n</chat>",
        "turns": [
            {"role": "user", "text": "Compaction: compress message 1"},
            {"role": "assistant", "text": "a line that was too long"},
            {"role": "user", "text": "Too long: write it again"},
        ],
    }


class SummarizeTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        fakes = (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX), ("devin", FAKE_DEVIN), ("opencode", FAKE_OPENCODE))
        for name, body in fakes:
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n{body}")
            path.chmod(0o755)
        self.user_home = self.base / "user-home"
        (self.user_home / ".config" / "devin").mkdir(parents=True)
        (self.user_home / ".config" / "devin" / "config.json").write_text(json.dumps({
            "version": 1, "devin": {"org_id": "org-1"}, "permissions": {"allow": ["Exec(ls)"]},
        }))
        self.log = self.base / "calls.jsonl"
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def summarize(self, backend: str, *options: str, mode: str = "", payload: dict | None = None,
                  env: dict[str, str] | None = None):
        return self.hist("summarize", backend, *options, stdin=json.dumps(payload or request()), check=False, env={
            "PATH": self.path, "HOME": str(self.user_home), "FAKE_LOG": str(self.log), "FAKE_MODE": mode, **(env or {}),
        })

    def serve(self, answer: dict, status: int = 200) -> tuple[str, list[dict]]:
        """A local API answering every POST with `answer`; returns its URL and what it received."""

        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                headers = {key.lower(): value for key, value in self.headers.items()}
                received.append({"path": self.path, "headers": headers, "body": body})
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", received

    def calls(self, key: str) -> list[dict]:
        return [call for call in map(json.loads, self.log.read_text().splitlines()) if key in call]

    def test_devin_answers_once_without_user_config_and_forgets_the_session(self) -> None:
        done = self.summarize("devin")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 7000, "cached_input_tokens": 0, "output_tokens": 40,
        }})
        (run,) = self.calls("args")
        self.assertIn("swe-2-max", run["args"])
        self.assertEqual(run["args"][run["args"].index("--permission-mode") + 1], "auto")
        self.assertEqual(run["config"]["devin"], {"org_id": "org-1"})
        self.assertNotIn("allow", run["config"]["permissions"])
        self.assertIn("mcp__*", run["config"]["permissions"]["deny"])
        self.assertFalse(any(run["config"]["read_config_from"].values()))
        self.assertEqual(run["entries"], [])
        # One message carries everything, the earlier attempt marked as such.
        prompt = run["prompt"]
        for part in ("SYSTEM RULES", "proj:0|earlier line", "Compaction: compress message 1",
                     "<your-earlier-line>\na line that was too long\n</your-earlier-line>", "Too long"):
            self.assertIn(part, prompt)
        self.assertLess(prompt.index("SYSTEM RULES"), prompt.index("Too long"))
        self.assertEqual(self.calls("rm"), [{"rm": ["--force", "fake-session"], "cwd": run["cwd"]}])

    def test_devin_tool_calls_and_failures_stop_loudly(self) -> None:
        extra = self.summarize("devin", mode="tools")
        self.assertEqual(extra.returncode, 1)
        self.assertIn("devin made 2 model requests and 1 tool calls; want 1 and 0", extra.stderr)
        self.assertEqual(len(self.calls("rm")), 1)
        failed = self.summarize("devin", mode="fail")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("devin exited 2: weekly quota exhausted", failed.stderr)
        self.assertEqual(len(self.calls("rm")), 1)
        stale = self.summarize("devin", payload=dict(request(), deadline="2020-01-01T00:00:00Z"))
        self.assertIn("the request deadline has passed", stale.stderr)
        wrong = self.summarize("devin", payload=dict(request(), protocol="hist-summarize/2"))
        self.assertIn("expected a hist-summarize/1 request", wrong.stderr)

    def test_devin_forgets_a_session_cut_off_by_the_deadline(self) -> None:
        started = time.monotonic()
        slow = self.summarize("devin", mode="slow", payload=request(seconds=MARGIN + 3))
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("devin passed the request deadline", slow.stderr)
        (run,) = self.calls("args")
        self.assertEqual(self.calls("rm"), [{"rm": ["--force", "fake-session"], "cwd": run["cwd"]}])

    def test_opencode_runs_isolated_and_stops_at_the_finished_step(self) -> None:
        started = time.monotonic()
        done = self.summarize("opencode")
        self.assertLess(time.monotonic() - started, 30, "waited for the hanging CLI")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 40,
        }})
        (run,) = self.calls("args")
        self.assertIn("--standalone", run["args"])
        self.assertNotIn("--auto", run["args"])
        config = run["config"]
        self.assertEqual(config["permission"], "ask")
        self.assertEqual(config["agent"]["hist"], {"mode": "primary", "prompt": "SYSTEM RULES", "permission": "ask"})
        self.assertEqual((config["plugin"], config["mcp"], config["instructions"]), ([], {}, []))
        root = os.path.dirname(run["cwd"])
        self.assertEqual({os.path.dirname(os.path.realpath(path)) for path in run["xdg"]}, {root})
        message = run["args"][-1]
        self.assertNotIn("SYSTEM RULES", message)
        self.assertIn("<your-earlier-line>", message)

    def test_opencode_accepts_an_unreported_finish_but_not_errors_or_tools(self) -> None:
        unreported = self.summarize("opencode", mode="unreported")
        self.assertEqual(unreported.returncode, 0, unreported.stderr)
        self.assertEqual(json.loads(unreported.stdout), {"text": "user: asked for X", "usage": None})
        limited = self.summarize("opencode", mode="limited")
        self.assertEqual(limited.returncode, 1)
        self.assertIn("opencode: Rate limit exceeded", limited.stderr)
        crash = self.summarize("opencode", mode="crash")
        self.assertIn("opencode exited 1 with no events: Failed to start server", crash.stderr)
        tool = self.summarize("opencode", mode="tool")
        self.assertEqual(tool.returncode, 1)
        self.assertIn("opencode made 1 model requests and 1 tool calls; want 1 and 0", tool.stderr)

    def test_claude_answers_once_without_user_settings_or_tools(self) -> None:
        done = self.summarize("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 702, "cached_input_tokens": 100, "output_tokens": 30,
        }})
        (run,) = self.calls("args")
        args = run["args"]
        follows = lambda flag: args[args.index(flag) + 1]
        self.assertEqual(follows("--model"), "claude-haiku-5-5")
        self.assertEqual((follows("--tools"), follows("--setting-sources")), ("", ""))
        self.assertEqual(follows("--system-prompt"), "SYSTEM RULES")
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, args)
        self.assertEqual(run["entries"], [])
        self.assertNotIn("SYSTEM RULES", run["stdin"])
        self.assertIn("<your-earlier-line>\na line that was too long", run["stdin"])

    def test_claude_errors_and_tool_use_stop_loudly(self) -> None:
        tools = self.summarize("claude", mode="tools")
        self.assertEqual(tools.returncode, 1)
        self.assertIn("claude made 2 model requests and 1 tool calls; want 1 and 0", tools.stderr)
        failed = self.summarize("claude", mode="fail")
        self.assertIn("claude: There's an issue with the selected model", failed.stderr)
        crash = self.summarize("claude", mode="crash")
        self.assertIn("claude exited 1 without a result: Not logged in", crash.stderr)

    def test_codex_answers_once_with_its_tools_off_and_no_session_kept(self) -> None:
        done = self.summarize("codex")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 6200, "cached_input_tokens": 1800, "output_tokens": 40,
        }})
        (run,) = self.calls("args")
        args = run["args"]
        self.assertEqual(args[:2], ["exec", "--json"])
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", 'web_search="disabled"'):
            self.assertIn(flag, args)
        disabled = {args[i + 1] for i, arg in enumerate(args) if arg == "--disable"}
        self.assertEqual(disabled, set(CODEX_OFF))
        self.assertTrue({"shell_tool", "unified_exec", "memories", "apps"} <= disabled)
        self.assertEqual(args[-1], "-")
        self.assertEqual(run["system"], "SYSTEM RULES")
        self.assertEqual(run["entries"], [])
        self.assertNotIn("SYSTEM RULES", run["stdin"])
        self.assertIn("Too long: write it again", run["stdin"])

    def test_codex_failures_tools_and_ignored_settings_stop_loudly(self) -> None:
        failed = self.summarize("codex", mode="failed")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("codex: The model is not supported", failed.stderr)
        crash = self.summarize("codex", mode="crash")
        self.assertIn("codex exited 1: Error: Unknown feature flag: goals", crash.stderr)
        tool = self.summarize("codex", mode="tool")
        self.assertIn("codex wrote 1 messages and 1 other items; want 1 and 0; command_execution", tool.stderr)
        ignored = self.summarize("codex", mode="ignored")
        self.assertIn("other items; want 1 and 0; error: `goals` is ignored.", ignored.stderr)

    def test_anthropic_sends_one_request_with_the_named_key(self) -> None:
        url, received = self.serve({
            "content": [{"type": "text", "text": "user: asked for X"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900,
                      "output_tokens": 30},
        })
        done = self.summarize("anthropic", "--base-url", url + "/", "--key-env", "TEST_KEY",
                              env={"TEST_KEY": "key-1", "ANTHROPIC_API_KEY": "not-this-one"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 950, "cached_input_tokens": 900, "output_tokens": 30,
        }})
        (call,) = received
        self.assertEqual(call["path"], "/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "key-1")
        self.assertIn("anthropic-version", call["headers"])
        self.assertEqual(call["body"], {
            "model": "claude-haiku-5-5", "max_tokens": 4096, "system": "SYSTEM RULES", "messages": EXPECTED_MESSAGES,
        })
        self.assertFalse(self.log.exists(), "an API backend ran a CLI")

    def test_openai_compatible_api_sends_only_a_named_key_and_reports_errors(self) -> None:
        url, received = self.serve({
            "choices": [{"message": {"role": "assistant", "content": "user: asked for X"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 100}},
        })
        done = self.summarize("openai", "--base-url", url, "--key-env", "ROUTER_KEY", "--model", "vendor/model-1",
                              env={"ROUTER_KEY": "key-2", "OPENAI_API_KEY": "not-this-one"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"text": "user: asked for X", "usage": {
            "input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 40,
        }})
        (call,) = received
        self.assertEqual(call["path"], "/chat/completions")
        self.assertEqual(call["headers"]["authorization"], "Bearer key-2")
        self.assertEqual(call["body"], {
            "model": "vendor/model-1",
            "messages": [{"role": "system", "content": "SYSTEM RULES"}, *EXPECTED_MESSAGES],
        })
        # The default key is OpenAI's: another base URL never receives it unnamed.
        refused, sent = self.serve({"error": {"message": "Incorrect API key provided"}}, status=401)
        denied = self.summarize("openai", "--base-url", refused, env={"OPENAI_API_KEY": "sk-openai"})
        self.assertNotIn("authorization", sent[0]["headers"])
        self.assertEqual(denied.returncode, 1)
        self.assertIn("/chat/completions answered 401: ", denied.stderr)
        self.assertIn("Incorrect API key provided", denied.stderr)
        routed, _ = self.serve({"error": {"message": "No endpoints found for vendor/model-1"}})
        unrouted = self.summarize("openai", "--base-url", routed)
        self.assertIn("answered an error: {'message': 'No endpoints found for vendor/model-1'}", unrouted.stderr)

    def test_effort_is_medium_for_the_default_model_and_settable(self) -> None:
        efforts = lambda run: [arg for arg in run["args"] if arg.startswith("model_reasoning_effort=")]
        self.assertEqual(self.summarize("codex", "--model", "gpt-6-luna-pro").returncode, 0)
        self.assertEqual(self.summarize("codex", "--model", "gpt-6-luna-pro", "--effort", "high").returncode, 0)
        own, high = self.calls("args")
        self.assertEqual((efforts(own), efforts(high)), ([], ['model_reasoning_effort="high"']))
        url, received = self.serve({"choices": [{"message": {"content": "user: asked for X"}}]})
        for options in ((), ("--effort", "low"), ("--model", "gpt-4o")):
            done = self.summarize("openai", "--base-url", url, *options)
            self.assertEqual(done.returncode, 0, done.stderr)
        chosen = [(call["body"]["model"], call["body"].get("reasoning_effort")) for call in received]
        self.assertEqual(chosen, [("gpt-6-luna", "medium"), ("gpt-6-luna", "low"), ("gpt-4o", None)])
        refused = self.summarize("claude", "--effort", "high")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--effort applies only to codex and openai", refused.stderr)

    def test_api_options_are_refused_for_a_cli(self) -> None:
        done = self.summarize("codex", "--base-url", "http://127.0.0.1:9")
        self.assertEqual(done.returncode, 1)
        self.assertIn("--base-url and --key-env apply only to anthropic and openai", done.stderr)
        self.assertFalse(self.log.exists())
