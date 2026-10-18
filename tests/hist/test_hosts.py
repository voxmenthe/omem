"""End-to-end mapping and classification for hosts beyond Codex and Claude."""

import json

from tests.hist.fixtures import CommandCode, HistCase, write_jsonl

TYPED = "cc000000-0000-4000-8000-0000000000a1"
PRINTED = "cc000000-0000-4000-8000-0000000000b2"


class InstalledCase(HistCase):
    """hist is installed before any session is written, so none waits for a backfill."""

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")


class CommandCodeTest(InstalledCase):
    def test_messages_map_and_print_runs_are_workers(self) -> None:
        cwd = str(self.repo)
        typed = CommandCode.path(self.commandcode, cwd, TYPED)
        CommandCode.write_meta(typed)
        write_jsonl(typed, [
            CommandCode.session(TYPED, cwd),
            {"type": "model_change", "id": "m0", "parentId": None, "timestamp": "x", "model": "m"},
            CommandCode.user("<command-message>goal</command-message>\n<command-args>fix it</command-args>", 1,
                             isAutomated=True, injected={"companions": [{"kind": "skill", "text": "SKILL-BODY"}]}),
            CommandCode.assistant([{"type": "thinking", "thinking": "THINKING-TEXT", "signature": "s"},
                                   {"type": "tool_use", "id": "t1", "name": "read_file",
                                    "input": {"path": "guard.py"}}], 2),
            CommandCode.tool_result("t1", "def guard(): ...", 2),
            CommandCode.user("btw keep it short", 3, source="steering"),
            CommandCode.assistant([], 3),
            CommandCode.user("Please continue your response.", 3, source="followup"),
            CommandCode.user("Error: 500 Upstream idle timeout exceeded", 4, source=None),
            CommandCode.assistant([{"type": "text", "text": "guard fixed"}], 5),
        ])
        printed = CommandCode.path(self.commandcode, cwd, PRINTED)
        CommandCode.write_meta(printed, entrypoint="print")
        write_jsonl(printed, [CommandCode.session(PRINTED, cwd, 6), CommandCode.user("headless task", 6)])

        self.hist("sync")
        db = self.db()
        classes = dict(db.execute("SELECT native_id, class FROM sessions").fetchall())
        self.assertEqual(classes, {TYPED: "interactive", PRINTED: "worker"})
        rows = db.execute(
            "SELECT kind, tool, text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.record >= 0 ORDER BY position",
            (TYPED,),
        ).fetchall()
        self.assertEqual(rows, [
            ("user", None, "<command-message>goal</command-message>\n<command-args>fix it</command-args>"),
            ("tool_call", "read_file", json.dumps({"path": "guard.py"})),
            ("tool_result", "read_file", "def guard(): ..."),
            ("user", None, "btw keep it short"),
            ("event", None, "Error: 500 Upstream idle timeout exceeded"),
            ("agent", None, "guard fixed"),
        ])
        for marker in (b"SKILL-BODY", b"THINKING-TEXT", b"Please continue"):
            self.assertNotStored(marker)
        self.assertIn(PRINTED[-8:], self.hist("sessions", "--workers").stdout)
