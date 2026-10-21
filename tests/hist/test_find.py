"""`find`: escaped output and snippets."""

import json
import re

from hist.store import search_text
from tests.hist.fixtures import Claude, Codex, HistCase, write_jsonl

THREAD = "019a0000-0000-7000-8000-0000000003a1"
OTHER = "019a0000-0000-7000-8000-0000000003a2"
SESSION = "5c1f0000-0000-4000-8000-0000000000a1"
AGENT = "a00000000000000f1"


def hits(out: str) -> list[str]:
    """The hit lines of one page, without the header, note or ancestor lines."""

    return [line for line in out.splitlines() if re.match(r"[\w.-]+:\d+\+1\|", line)]


class FindTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        escaped = (
            'Script completed\nWall time 0.1 seconds\nOutput:\n\n'
            '{"exit_code":1,"output":"step one\\nTraceback (most recent call last):\\n'
            '  File \\"run.py\\", line 3\\nZeroDivisionError: \\u00e9chec"}'
        )
        write_jsonl(Codex.path(self.codex, THREAD), [
            Codex.meta(THREAD, cwd, "cli", self.origin),
            Codex.user("run the script", 1),
            Codex.call("exec", json.dumps({"cmd": "python run.py"}), "c1", 2),
            Codex.output("c1", escaped, 3),
            Codex.assistant("the gradient is \\nabla f, so the zebra stays", 4),
            Codex.user("the error.path.guard failed", 5),
        ])
        write_jsonl(Codex.path(self.codex, OTHER), [
            Codex.meta(OTHER, cwd, "cli", self.origin, minute=20),
            Codex.user("zebra in another session", 20),
            Codex.assistant("zebra reply", 21),
        ])
        project = Claude.project(self.claude, cwd)
        write_jsonl(project / f"{SESSION}.jsonl", [
            Claude.user("u1", SESSION, cwd, "zebra from the calling session", 30),
        ])
        write_jsonl(project / SESSION / "subagents" / f"agent-{AGENT}.jsonl", [
            Claude.user("u2", SESSION, cwd, "zebra from its subagent", 31, isSidechain=True, agentId=AGENT),
        ])
        self.hist("sync")

    def test_escaped_json_output_is_found_by_its_words(self) -> None:
        out = self.hist("find", "Traceback").stdout
        self.assertEqual(len(hits(out)), 1, out)
        self.assertIn("step one Traceback (most recent call last):", out)
        self.assertEqual(len(hits(self.hist("find", "échec").stdout)), 1)
        self.assertEqual(len(hits(self.hist("find", "run.py").stdout)), 2)

    def test_backslashes_outside_json_stay_literal(self) -> None:
        self.assertEqual(len(hits(self.hist("find", "nabla").stdout)), 1)
        text = "Saw \\frac{a}{b} and C:\\new\\table"
        self.assertEqual(search_text(text), text)
        self.assertEqual(search_text('{"a": "x\\ty\\\\z\\ud800"}'), '{"a": "x\ty\\z "}')

    def test_snippet_prints_the_text_unchanged(self) -> None:
        (line,) = hits(self.hist("find", "path.guard").stdout)
        self.assertIn("…the error.path.guard failed…", line)
