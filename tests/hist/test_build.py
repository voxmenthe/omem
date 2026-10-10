"""`hist build` through the real summarizer process contract, with a fake summarizer."""

import json
import os
import sys
import textwrap
import time

from tests.hist.fixtures import Codex, HistCase, write_jsonl

THREAD = "019a0000-0000-7000-8000-0000000004a1"

# Records each request; answers deterministically. FAKE_MODE selects misbehavior.
FAKE = textwrap.dedent('''
    import json, os, subprocess, sys
    request = json.load(sys.stdin)
    log = os.environ["FAKE_LOG"]
    with open(log, "a") as handle:
        handle.write(json.dumps(request) + "\\n")
    mode = os.environ.get("FAKE_MODE", "")
    task = request["turns"][0]["text"]
    if mode == "straggler":
        child = subprocess.Popen(["sleep", "60"])
        with open(log + ".pid", "w") as handle:
            handle.write(str(child.pid))
    if mode == "fail":
        print("model unavailable", file=sys.stderr)
        sys.exit(3)
    if mode == "long" and len(request["turns"]) == 1:
        print(json.dumps({"text": "x" * 900, "usage": {"input": 1}}))
        sys.exit(0)
    kind = "leaf" if task.startswith("Compaction: compress") else "merge"
    print(json.dumps({"text": f"SUMMARY {kind} {len(request['turns'])}", "usage": {"input": 10, "output": 3}}))
''')


class BuildTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        fake = self.base / "fake_summarizer.py"
        fake.write_text(FAKE)
        self.log = self.base / "requests.jsonl"
        self.write_config(extra=(
            f'[summaries]\ncommand = ["{sys.executable}", "{fake}"]\n'
            'timelines = ["proj"]\nmax_attempts_per_day = 4\n'
        ))
        self.hist("status")
        write_jsonl(Codex.path(self.codex, THREAD), [
            Codex.meta(THREAD, str(self.repo), "cli", self.origin),
            Codex.user("first long request " + "a" * 700, 1),
            Codex.assistant("second long reply " + "b" * 700, 2),
        ])
        self.hist("sync")

    def build(self, *args: str, mode: str = "", check: bool = True):
        return self.hist("build", *args, check=check,
                         env={"FAKE_LOG": str(self.log), "FAKE_MODE": mode})

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_one_leaf_and_one_merge_through_the_process_contract(self) -> None:
        # Position 0, the session-start event, fits as an exact line; 1 and 2 need leaves.
        self.assertEqual(self.build().stdout, "built 3 summaries in 3 attempts\n")
        requests = self.requests()
        self.assertEqual([r["protocol"] for r in requests], ["hist-summarize/1"] * 3)
        tasks = [r["turns"][0]["text"] for r in requests]
        self.assertTrue(tasks[0].startswith("Compaction: compress message 1 "), tasks[0])
        self.assertIn("user: first long request", tasks[0])
        self.assertIn("proj:1+1|SUMMARY leaf 1\n", requests[1]["context"])
        self.assertTrue(tasks[2].startswith("Compaction: merge lines proj:0+1 and proj:1+1"), tasks[2])
        self.assertIn("\nproj:1+1|SUMMARY leaf 1\n", tasks[2])
        nodes = self.db().execute("SELECT level, idx, text FROM nodes ORDER BY level, idx").fetchall()
        self.assertEqual(nodes, [(0, 1, "SUMMARY leaf 1"), (0, 2, "SUMMARY leaf 1"), (1, 0, "SUMMARY merge 1")])
        self.assertEqual(self.build().stdout, "built 0 summaries in 0 attempts\n")

    def test_a_too_long_line_is_retried_with_the_cut_shown(self) -> None:
        self.build("--limit", "1", mode="long")
        requests = self.requests()
        self.assertEqual(len(requests), 2)
        retry = requests[1]["turns"]
        self.assertEqual([turn["role"] for turn in retry], ["user", "assistant", "user"])
        self.assertTrue(retry[2]["text"].startswith("Too long: your line is 900 bytes"))
        self.assertIn("proj:1+1|SUMMARY leaf 3", self.hist("view").stdout)

    def test_the_daily_cap_and_summarizer_failures_stop_loudly(self) -> None:
        failed = self.build(mode="fail", check=False)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("summarizer exited 3: model unavailable", failed.stderr)
        self.build("--limit", "1", mode="long")
        capped = self.build(check=False)
        self.assertEqual(capped.returncode, 1)
        self.assertIn("daily cap of 4 summarizer attempts reached", capped.stderr)
        self.assertEqual(len(self.requests()), 4)

    def test_no_summarizer_process_outlives_its_call(self) -> None:
        started = time.monotonic()
        self.build("--limit", "1", mode="straggler")
        self.assertLess(time.monotonic() - started, 10, "the build waited on the straggler")
        pid = int((self.base / "requests.jsonl.pid").read_text())
        # The killed child is reaped by init asynchronously; allow it a moment.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"summarizer child {pid} outlived its call")
