"""End-to-end mapping and classification for hosts beyond Codex and Claude."""

import json

from tests.hist.fixtures import (
    Antigravity,
    Claude,
    Codex,
    CommandCode,
    Devin,
    HistCase,
    OpenCode,
    protobuf,
    write_jsonl,
)
from hist.adapters import TEXT_PART_CHARS

TYPED = "cc000000-0000-4000-8000-0000000000a1"
PRINTED = "cc000000-0000-4000-8000-0000000000b2"
CLAUDE_PARENT = "5e000000-0000-4000-8000-0000000000c1"
CODEX_PARENT = "019a0000-0000-7000-8000-0000000000c2"


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


class MarkerLinkTest(InstalledCase):
    """A launcher's `[hist-parent <adapter>:<id>]` line links a worker on any host."""

    def setUp(self) -> None:
        super().setUp()
        self.cwd = str(self.repo)

    def claude_parent(self) -> None:
        write_jsonl(Claude.project(self.claude, self.cwd) / f"{CLAUDE_PARENT}.jsonl", [
            Claude.user("p1", CLAUDE_PARENT, self.cwd, "delegate the audit", 1, promptSource="typed")])

    def codex_parent(self) -> None:
        write_jsonl(Codex.path(self.codex, CODEX_PARENT), [
            Codex.meta(CODEX_PARENT, self.cwd, "cli", self.origin), Codex.user("delegate the review", 1)])

    def command_code(self, session: str, *tasks: str, entrypoint: str | None = "print") -> None:
        path = CommandCode.path(self.commandcode, self.cwd, session)
        CommandCode.write_meta(path, **({"entrypoint": entrypoint} if entrypoint else {}))
        write_jsonl(path, [CommandCode.session(session, self.cwd, 2)]
                    + [CommandCode.user(task, 2 + n) for n, task in enumerate(tasks)])

    def links(self) -> dict[str, tuple[str, str, int]]:
        rows = self.db().execute("SELECT child_native, parent_native, via, resolved FROM links").fetchall()
        return {child: (parent, via, resolved) for child, parent, via, resolved in rows}

    def parents(self) -> dict[str, str]:
        return dict(self.db().execute(
            "SELECT c.native_id, p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id").fetchall())

    def test_workers_on_four_hosts_link_once_their_parent_is_indexed(self) -> None:
        marker = f"[hist-parent claude:{CLAUDE_PARENT}]"
        command = "cc000000-0000-4000-8000-0000000000d1"
        self.command_code(command, f"audit the guard\n\n{marker}")
        opencode = OpenCode(self.opencode)
        self.addCleanup(opencode.close)
        # OpenCode keeps a launcher's task JSON-quoted.
        opencode.session("ses_worker00000d2", self.cwd, viewed=False)
        opencode.add("ses_worker00000d2", [OpenCode.user(json.dumps(f"audit the guard\n\n{marker}\n"), 2),
                                           OpenCode.idle(3)])
        devin = Devin(self.devin)
        self.addCleanup(devin.close)
        # The marker straddles the boundary between the task's stored parts.
        long_task = "audit the guard " + "x" * (TEXT_PART_CHARS - 26) + " " + marker + "\n\nDelegate task id: a1"
        self.assertLess(long_task.index(marker), TEXT_PART_CHARS)
        self.assertGreater(long_task.index(marker) + len(marker), TEXT_PART_CHARS)
        devin.session("devin-worker-0000d3", self.cwd)
        devin.add("devin-worker-0000d3", [(0, None, devin.prompt(0)), (1, 0, devin.user(long_task, 2))])
        antigravity = Antigravity(self.antigravity)
        self.addCleanup(antigravity.close)
        agy = "a9000000-0000-4000-8000-0000000000d4"
        antigravity.conversation(agy, self.cwd, minute=2)
        antigravity.add(agy, [Antigravity.user(f"audit the guard\n\n{marker}", 2, printed=True)])
        workers = [command, "ses_worker00000d2", "devin-worker-0000d3", agy]

        self.hist("sync")
        self.assertEqual(self.links(), {w: (CLAUDE_PARENT, "marker", 0) for w in workers})
        self.claude_parent()
        self.hist("sync")
        self.assertEqual(self.links(), {w: (CLAUDE_PARENT, "marker", 1) for w in workers})
        self.assertEqual(self.parents(), {w: CLAUDE_PARENT for w in workers})
        events = [text for (text,) in self.db().execute(
            "SELECT m.text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.kind = 'event' AND m.text LIKE 'worker %'", (CLAUDE_PARENT,))]
        # Devin's worker started now; the fixtures' older workers are attached without a note.
        devin_alias = self.db().execute(
            "SELECT alias FROM sessions WHERE native_id = 'devin-worker-0000d3'").fetchone()[0]
        self.assertIn(f"worker w-{devin_alias} started (marker): audit the guard xxx", "\n".join(events))
        self.assertTrue(all(" started (marker): " in text for text in events), events)

    def test_only_the_last_marker_of_a_workers_first_record_counts(self) -> None:
        self.claude_parent()
        self.codex_parent()
        claude, codex = f"[hist-parent claude:{CLAUDE_PARENT}]", f"[hist-parent codex:{CODEX_PARENT}]"
        last, later, own, unknown, bad_last, typed = (
            f"cc000000-0000-4000-8000-0000000000e{n}" for n in range(1, 7))
        self.command_code(last, f"forwarded packet:\n{codex}\nend of packet\n\n{claude}")
        self.command_code(later, "audit the guard", f"now {claude}")
        self.command_code(own, f"audit the guard [hist-parent commandcode:{own}]")
        self.command_code(unknown, f"audit the guard [hist-parent kimi:{CLAUDE_PARENT}]")
        self.command_code(bad_last, f"audit the guard {claude} [hist-parent commandcode:{bad_last}]")
        self.command_code(typed, f"audit the guard {claude}", entrypoint=None)
        self.hist("sync")
        self.assertEqual(self.links(), {last: (CLAUDE_PARENT, "marker", 1)})
        self.assertEqual(self.parents(), {last: CLAUDE_PARENT})

    def test_a_native_or_hook_link_stands_and_a_marker_fills_an_empty_slot(self) -> None:
        self.claude_parent()
        self.codex_parent()
        marker = f"[hist-parent codex:{CODEX_PARENT}]"
        opencode = OpenCode(self.opencode)
        self.addCleanup(opencode.close)
        opencode.session("ses_parent0000f1", self.cwd)
        opencode.session("ses_child00000f2", self.cwd, viewed=False, parent="ses_parent0000f1")
        opencode.add("ses_parent0000f1", [OpenCode.user("plan the audit", 1), OpenCode.idle(1)])
        opencode.add("ses_child00000f2", [OpenCode.user(f"audit the guard {marker}", 2), OpenCode.idle(2)])
        hooked, unhooked = "4b000000-0000-4000-8000-0000000000f3", "4b000000-0000-4000-8000-0000000000f4"
        project = Claude.project(self.claude, self.cwd)
        payload = {"session_id": hooked, "transcript_path": str(project / f"{hooked}.jsonl"), "cwd": self.cwd,
                   "hook_event_name": "SessionStart", "source": "startup"}
        self.hist("hook", "claude", "SessionStart", stdin=json.dumps(payload),
                  env={"HIST_PARENT": f"claude:{CLAUDE_PARENT}"})
        for session in (hooked, unhooked):
            write_jsonl(project / f"{session}.jsonl", [
                Claude.user(f"w{session[-1]}", session, self.cwd, f"audit the guard {marker}", 2,
                            entrypoint="sdk-cli")])
        self.hist("sync")
        self.assertEqual(self.links(), {
            "ses_child00000f2": ("ses_parent0000f1", "native", 1),
            hooked: (CLAUDE_PARENT, "HIST_PARENT", 1),
            unhooked: (CODEX_PARENT, "marker", 1),
        })
        self.assertEqual(self.parents(), {
            "ses_child00000f2": "ses_parent0000f1", hooked: CLAUDE_PARENT, unhooked: CODEX_PARENT})
