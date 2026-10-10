"""`hist summarize` against fake `devin` and `opencode` executables on PATH."""

import json
import os
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

from hist.summarize import MARGIN_SECONDS as MARGIN
from tests.hist.fixtures import HistCase

# Both fakes log what they were given; FAKE_MODE selects how the run goes.
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
        for name, body in (("devin", FAKE_DEVIN), ("opencode", FAKE_OPENCODE)):
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

    def summarize(self, backend: str, mode: str = "", payload: dict | None = None):
        return self.hist("summarize", backend, stdin=json.dumps(payload or request()), check=False, env={
            "PATH": self.path, "HOME": str(self.user_home), "FAKE_LOG": str(self.log), "FAKE_MODE": mode,
        })

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
