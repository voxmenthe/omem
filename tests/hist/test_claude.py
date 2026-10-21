"""Claude Code text recorded outside ordinary user and tool-result records:
prompts queued while the agent worked."""

from tests.hist.fixtures import Claude, HistCase, write_jsonl

SESSION = "7e000000-0000-4000-8000-0000000000a1"


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
