"""End-to-end mapping and classification for hosts beyond Codex and Claude."""

import json

from tests.hist.fixtures import Antigravity, CommandCode, Devin, HistCase, OpenCode, protobuf, write_jsonl

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


class DevinTest(InstalledCase):
    def setUp(self) -> None:
        super().setUp()
        self.host = Devin(self.devin)
        self.addCleanup(self.host.close)

    def test_main_chains_are_indexed_once_and_others_never(self) -> None:
        host, session = self.host, "devin-session-000000d1"
        host.session(session, str(self.repo))
        prompt = host.user("fix the guard", 1, message_id="typed-1")
        host.add(session, [
            (0, None, host.prompt(0)),
            (1, 0, prompt),
            (2, 1, host.reply("", 2, calls=[{"id": "c1", "name": "read", "arguments": "{'path': 'guard.py'}"}])),
            (3, 2, host.message("tool", "def guard(): ...", 2, source="tool_result", operation="read",
                                tool_call_id="c1")),
            (4, 3, host.message("user", "continue", 3, source="user")),
            (5, 4, host.reply("guard fixed", 3)),
            # A later main chain repeats the prompt under its message id.
            (10, None, host.message("system", "<system_info>\nos: macos", 4)),
            (11, 10, prompt),
            (20, None, host.prompt(5, operation="subagent_explore")),
            (21, 20, host.user("SUBAGENT-TASK", 5)),
            (22, 21, host.reply("SUBAGENT-REPLY", 5)),
            (30, None, host.message("system", "You are a Summarizer of this conversation.", 6)),
            (31, 30, host.reply("SUMMARY-TEXT", 6)),
            # A chain whose root is stored after its first message, as when Devin rewrites one.
            (41, 40, host.user("asked under a late root", 7)),
            (40, None, host.prompt(7)),
        ])
        skipped = "devin-session-000000d2"
        host.session(skipped, str(self.base / "tmp" / "hist-summarize-x1" / "work"))
        host.add(skipped, [(0, None, host.prompt(0)), (1, 0, host.user("SUMMARIZER-PROMPT", 1))])

        self.hist("sync")
        db = self.db()
        self.assertEqual(db.execute("SELECT native_id, class FROM sessions").fetchall(), [(session, "worker")])
        self.assertEqual(
            db.execute("SELECT reason FROM sources WHERE native_id = ?", (skipped,)).fetchone(),
            ("hist summarizer run",))
        rows = db.execute(
            "SELECT kind, tool, text FROM messages WHERE record >= 0 ORDER BY position").fetchall()
        self.assertEqual(rows, [
            ("user", None, "fix the guard"),
            ("tool_call", "read", "{'path': 'guard.py'}"),
            ("tool_result", "read", "def guard(): ..."),
            ("agent", None, "guard fixed"),
            ("user", None, "asked under a late root"),
        ])
        for marker in (b"SUBAGENT-", b"SUMMARY-TEXT", b"SUMMARIZER-PROMPT", b"THINKING-TEXT"):
            self.assertNotStored(marker)
        self.assertNotIn("unknown record types", self.hist("status").stdout)


class AntigravityTest(InstalledCase):
    def setUp(self) -> None:
        super().setUp()
        self.host = Antigravity(self.antigravity)
        self.addCleanup(self.host.close)

    def texts(self, native: str) -> list[tuple[str, str | None, str]]:
        return self.db().execute(
            "SELECT kind, tool, text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.record >= 0 ORDER BY position",
            (native,),
        ).fetchall()

    def test_steps_map_and_wait_while_not_final(self) -> None:
        host, native = self.host, "a9000000-0000-4000-8000-0000000000e1"
        host.conversation(native, str(self.repo), self.origin)
        host.typed(native, "fix the guard")
        error = protobuf((3, protobuf((1, "The model produced an invalid tool call."), (2, "invalid_args"))))
        host.add(native, [
            Antigravity.user("fix the guard", 1),
            Antigravity.reply("", 2, calls=(("view_file", '{"AbsolutePath":"guard.py"}'),), thinking="THINKING"),
            Antigravity.result("view_file", "def guard(): ...", 2),
            Antigravity.step(101, 2, (114, protobuf((1, "SYSTEM-MESSAGE")))),
            Antigravity.step(23, 3, (30, protobuf((5, "SESSION-SUMMARY")))),
            Antigravity.step(17, 3, (24, error)),
            Antigravity.reply("guard fixed", 4, status=Antigravity.RUNNING),
            Antigravity.user("and the tests?", 5),
        ])
        self.hist("sync")
        expected = [
            ("user", None, "fix the guard"),
            ("tool_call", "view_file", '{"AbsolutePath":"guard.py"}'),
            ("tool_result", "view_file", "def guard(): ..."),
            ("event", None, "context compacted"),
            ("event", None, "The model produced an invalid tool call."),
        ]
        self.assertEqual(self.texts(native), expected)
        for marker in (b"THINKING", b"SYSTEM-MESSAGE", b"SESSION-SUMMARY", b"guard fixed"):
            self.assertNotStored(marker)
        host.set_status(native, 6, Antigravity.DONE)
        host.add(native, [Antigravity.reply("tests pass", 6, status=Antigravity.RUNNING)])
        self.hist("sync")
        expected += [("agent", None, "guard fixed"), ("user", None, "and the tests?")]
        self.assertEqual(self.texts(native), expected)
        # A closed conversation's steps are all final.
        host.finish(native)
        self.hist("sync")
        self.assertEqual(self.texts(native), expected + [("agent", None, "tests pass")])

    def test_conversations_are_classified_by_history_flag_parent_and_prompts(self) -> None:
        cwd = str(self.repo)
        typed, printed, child, open_, closed, chatty = (
            f"a9000000-0000-4000-8000-0000000000f{n}" for n in range(1, 7))
        for native in (typed, printed, child, open_, closed, chatty):
            self.host.conversation(native, cwd, parent=typed if native == child else None)
            prompts = [Antigravity.user(f"{native} prompt", 1, printed=native == printed)]
            if native == chatty:
                prompts.append(Antigravity.user("a second prompt", 2))
            self.host.add(native, prompts)
        self.host.typed(typed, "typed prompt")
        self.host.finish(closed)
        self.hist("sync")
        classes = dict(self.db().execute("SELECT native_id, class FROM sessions").fetchall())
        # An open conversation with neither sign waits for its prompt history.
        self.assertEqual(classes, {typed: "interactive", printed: "worker", child: "subagent",
                                   closed: "worker", chatty: "interactive"})
        self.host.typed(open_, "typed prompt")
        self.hist("sync")
        self.assertEqual(
            self.db().execute("SELECT class FROM sessions WHERE native_id = ?", (open_,)).fetchone(),
            ("interactive",))
