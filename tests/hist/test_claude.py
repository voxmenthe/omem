"""Claude Code text recorded outside ordinary user and tool-result records:
prompts queued while the agent worked, and large outputs saved apart."""

import os

from tests.hist.fixtures import Claude, HistCase, write_jsonl

SESSION = "7e000000-0000-4000-8000-0000000000a1"
AGENT = "a7e00000000000001"
OTHER = "7e000000-0000-4000-8000-0000000000b2"


class ClaudeTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        self.cwd = str(self.repo)
        self.project = Claude.project(self.claude, self.cwd)

    def rows(self) -> list[tuple[str, str | None, str]]:
        return self.db().execute(
            "SELECT kind, tool, text FROM messages WHERE kind != 'event' ORDER BY timeline_id, position"
        ).fetchall()

    def test_queued_prompts_and_notifications_are_indexed(self) -> None:
        cwd = self.cwd
        write_jsonl(self.project / f"{SESSION}.jsonl", [
            Claude.user("u1", SESSION, cwd, "start the build", 1),
            Claude.queued("q1", SESSION, cwd, 2, "prompt", "also check the logs", origin="human"),
            Claude.queued("q2", SESSION, cwd, 3, "prompt", "older hosts give no origin"),
            Claude.queued("q3", SESSION, cwd, 4, "task-notification", "<task-notification>done</task-notification>"),
            Claude.queued("q4", SESSION, cwd, 5, "prompt", "a peer session asks", origin="peer"),
            Claude.queued("q5", SESSION, cwd, 6, "prompt", "<system-reminder>INJECTED</system-reminder>"),
            Claude.queued("q6", SESSION, cwd, 7, "future-mode", "UNKNOWN-TEXT"),
            Claude.hook_context("h1", SESSION, cwd, 8, "INJECTED hook"),
        ])
        self.hist("sync")
        self.assertEqual(self.rows(), [
            ("user", None, "start the build"),
            ("user", None, "also check the logs"),
            ("user", None, "older hosts give no origin"),
            ("work", "task", "<task-notification>done</task-notification>"),
            ("work", "peer", "a peer session asks"),
        ])
        self.assertNotStored(b"INJECTED")
        self.assertNotStored(b"UNKNOWN-TEXT")
        self.assertIn("queued_command:future-mode×1", self.hist("status").stdout)

    def test_status_counts_sessions_indexed_before_queued_prompts(self) -> None:
        self.assertNotIn("typed while the agent was working", self.hist("status").stdout)
        write_jsonl(self.project / f"{SESSION}.jsonl", [Claude.user("u1", SESSION, self.cwd, "hi", 1)])
        self.hist("sync")
        db = self.db()
        with db:
            db.execute("DELETE FROM meta WHERE key = 'queued_prompts_from'")
        self.hist("sync")
        self.assertIn("1 Claude Code sessions indexed before", self.hist("status").stdout)

    def test_saved_outputs_are_indexed_whole(self) -> None:
        cwd = self.cwd
        results = self.project / SESSION / "tool-results"
        results.mkdir(parents=True)
        whole = "first line\n" + "".join(f"row {n}\n" for n in range(9000)) + "TAIL-MARKER\n"
        (results / "b1.txt").write_text(whole)
        # Not this session's: another session's directory, and a symlink.
        elsewhere = self.project / OTHER / "tool-results"
        elsewhere.mkdir(parents=True)
        (elsewhere / "b2.txt").write_text("OTHER-SESSION-OUTPUT")
        os.symlink(elsewhere / "b2.txt", results / "b3.txt")
        subagent = self.project / SESSION / "subagents" / f"agent-{AGENT}.jsonl"
        (results / "b4.txt").write_text("SUBAGENT-WHOLE")
        write_jsonl(self.project / f"{SESSION}.jsonl", [
            Claude.user("u1", SESSION, cwd, "run it", 1),
            Claude.assistant("a1", SESSION, cwd, [Claude.tool_use("t1", "Bash", {"command": "x"})], 2),
            Claude.user("r1", SESSION, cwd, [Claude.tool_result("t1", Claude.saved_output(results / "b1.txt", "first line"))], 3),
            Claude.assistant("a2", SESSION, cwd, [Claude.tool_use("t2", "Bash", {"command": "y"})], 4),
            Claude.user("r2", SESSION, cwd, [Claude.tool_result("t2", Claude.saved_output(elsewhere / "b2.txt", "PREVIEW-2"))], 5),
            Claude.assistant("a3", SESSION, cwd, [Claude.tool_use("t3", "Bash", {"command": "z"})], 6),
            Claude.user("r3", SESSION, cwd, [Claude.tool_result("t3", Claude.saved_output(results / "b3.txt", "PREVIEW-3"))], 7),
        ])
        write_jsonl(subagent, [
            Claude.user("s1", SESSION, cwd, "sub task", 8, isSidechain=True, agentId=AGENT),
            Claude.assistant("s2", SESSION, cwd, [Claude.tool_use("t4", "Bash", {"command": "w"})], 9,
                             isSidechain=True, agentId=AGENT),
            Claude.user("s3", SESSION, cwd, [Claude.tool_result("t4", Claude.saved_output(results / "b4.txt", "PREVIEW-4"))], 10,
                        isSidechain=True, agentId=AGENT),
        ])
        self.hist("sync")
        outputs = [text for kind, _, text in self.rows() if kind == "tool_result"]
        self.assertEqual(len(outputs), 4)
        main, other, linked, sub = outputs
        self.assertTrue(main.startswith("first line\nrow 0\n"))
        self.assertIn("characters clipped", main)
        self.assertTrue(main.endswith("TAIL-MARKER\n"))
        self.assertTrue(other.startswith("<persisted-output>") and "PREVIEW-2" in other)
        self.assertTrue(linked.startswith("<persisted-output>") and "PREVIEW-3" in linked)
        self.assertNotStored(b"OTHER-SESSION-OUTPUT")
        self.assertEqual(sub, "SUBAGENT-WHOLE")
        self.assertIn("TAIL-MARKER", self.hist("find", "TAIL-MARKER", "--all").stdout)
