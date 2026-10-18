"""End-to-end mapping and classification for hosts beyond Codex and Claude."""

import json

from tests.hist.fixtures import CommandCode, HistCase, OpenCode, write_jsonl

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


class OpenCodeTest(InstalledCase):
    def setUp(self) -> None:
        super().setUp()
        self.host = OpenCode(self.opencode)
        self.addCleanup(self.host.close)

    def rows_of(self, native: str) -> list[tuple[str, str | None, str]]:
        return self.db().execute(
            "SELECT kind, tool, text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.record >= 0 ORDER BY position",
            (native,),
        ).fetchall()

    def test_rows_map_once_their_turn_ends(self) -> None:
        cwd = str(self.repo)
        self.host.session("ses_typed000000a1", cwd)
        self.host.add("ses_typed000000a1", [
            OpenCode.user("fix the guard", 1),
            OpenCode.row("synthetic", 1, text="AGENTS-BODY"),
            OpenCode.assistant([
                {"type": "reasoning", "text": "REASONING-TEXT"},
                {"type": "tool", "id": "t1", "name": "read", "state": {
                    "status": "completed", "input": {"path": "guard.py"},
                    "content": [{"type": "text", "text": "def guard(): ..."},
                                {"type": "file", "mime": "image/png", "uri": "data:image/png;base64,QUJD"}]}},
                {"type": "tool", "id": "t2", "name": "webfetch", "state": {
                    "status": "error", "input": {"url": "u"},
                    "error": {"type": "tool.execution", "message": "403"}}},
                {"type": "text", "text": "guard fixed"},
            ], 2),
            OpenCode.row("compaction", 3, status="completed", reason="manual", summary="SUMMARY-TEXT"),
            OpenCode.assistant([], 4, error={"type": "aborted", "message": "Aborted"}),
            OpenCode.idle(4),
            OpenCode.user("one more thing", 5),
        ])
        self.hist("sync")
        self.assertEqual(self.rows_of("ses_typed000000a1"), [
            ("user", None, "fix the guard"),
            ("tool_call", "read", json.dumps({"path": "guard.py"})),
            ("tool_result", "read", "def guard(): ...\n[image omitted]"),
            ("tool_call", "webfetch", json.dumps({"url": "u"})),
            ("tool_result", "webfetch", "403"),
            ("agent", None, "guard fixed"),
            ("event", None, "context compacted"),
            ("event", None, "Aborted"),
        ])
        for marker in (b"AGENTS-BODY", b"REASONING-TEXT", b"SUMMARY-TEXT", b"one more thing"):
            self.assertNotStored(marker)
        # The next turn's prompt waits for that turn to end.
        self.host.add("ses_typed000000a1", [OpenCode.idle(6)])
        self.hist("sync")
        self.assertEqual(self.rows_of("ses_typed000000a1")[-1], ("user", None, "one more thing"))

    def test_sessions_are_classified_by_view_parent_and_version(self) -> None:
        cwd = str(self.repo)
        for native, kwargs, prompts in (
            ("ses_viewed00000b1", {}, 1),
            ("ses_headless000b2", {"viewed": False}, 1),
            ("ses_child00000b3", {"viewed": False, "parent": "ses_viewed00000b1"}, 1),
            ("ses_migrated000b4", {"viewed": False, "version": "1.18.31"}, 2),
            ("ses_migrated000b5", {"viewed": False, "version": "1.18.31"}, 1),
        ):
            self.host.session(native, cwd, **kwargs)
            turns = [OpenCode.user(f"{native} prompt {n}", n + 1) for n in range(prompts)]
            # Sessions migrated from OpenCode 1.x have no `idle` rows.
            self.host.add(native, turns if "version" in kwargs else turns + [OpenCode.idle(9)])
        self.hist("sync")
        classes = dict(self.db().execute("SELECT native_id, class FROM sessions").fetchall())
        self.assertEqual(classes, {
            "ses_viewed00000b1": "interactive", "ses_headless000b2": "worker",
            "ses_child00000b3": "subagent", "ses_migrated000b4": "interactive",
            "ses_migrated000b5": "worker",
        })
        self.assertEqual(self.rows_of("ses_migrated000b4"), [
            ("user", None, "ses_migrated000b4 prompt 0"), ("user", None, "ses_migrated000b4 prompt 1")])

    def test_a_rewritten_row_stops_its_session_and_a_deleted_session_is_missing(self) -> None:
        for native in ("ses_rewritten00c1", "ses_deleted0000c2"):
            self.host.session(native, str(self.repo))
            self.host.add(native, [OpenCode.user(f"{native} {word}", n + 1)
                                   for n, word in enumerate(("first", "middle", "last"))]
                          + [OpenCode.idle(4)])
        self.hist("sync")
        self.assertEqual(self.hist("verify").stdout.split(" in ")[0], "verified 2 sources")
        self.host.db.execute(
            "UPDATE session_message SET data = replace(data, 'middle', 'MIDDLE') WHERE session_id = ?",
            ("ses_rewritten00c1",))
        for table, column in (("session_message", "session_id"), ("session_v2", "id")):
            self.host.db.execute(f"DELETE FROM {table} WHERE {column} = ?", ("ses_deleted0000c2",))
        self.host.db.commit()
        result = self.hist("verify", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"stopped: opencode:{self.host.source('ses_rewritten00c1').name}", result.stdout)
        self.assertIn("1 unreadable or moved", result.stdout)
        self.assertIn("consumed rows changed", self.hist("status").stdout)
