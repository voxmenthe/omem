"""End-to-end mapping and classification for hosts beyond Codex and Claude."""

import json
import os
import time
from pathlib import Path
from typing import Any

from tests.hist.fixtures import (
    Antigravity,
    Claude,
    Codex,
    CommandCode,
    Devin,
    HistCase,
    Kimi,
    OpenCode,
    protobuf,
    write_jsonl,
)

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
        self.assertIn("stopped: opencode:ses_rewritten00c1", result.stdout)
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


class KimiTest(InstalledCase):
    def setUp(self) -> None:
        super().setUp()
        self.cwd = str(self.repo)

    def session(self, records: list[dict[str, Any]], home: Path | None = None, protocol: str = "1.5",
                native: str | None = None, **state: Any) -> str:
        native = native or Kimi.session_id()
        Kimi.write_state(home or self.kimi, self.cwd, native, **state)
        write_jsonl(Kimi.journal(home or self.kimi, self.cwd, native), [Kimi.metadata(protocol), *records])
        return native

    def classes(self) -> dict[str, str]:
        return dict(self.db().execute("SELECT native_id, class FROM sessions WHERE adapter = 'kimi'").fetchall())

    def texts(self, native: str) -> list[tuple[str, str | None, str]]:
        return self.db().execute(
            "SELECT kind, tool, text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.record >= 0 ORDER BY position",
            (native,),
        ).fetchall()

    def age(self, native: str, home: Path | None = None) -> None:
        """Make a session's journal two hours old; only `sync --all` then reads it,
        as it predates the install."""

        then = time.time() - 7200
        os.utime(Kimi.journal(home or self.kimi, self.cwd, native), (then, then))

    def test_main_sessions_are_classified_by_typed_history_then_title_then_settling(self) -> None:
        launcher_home = self.kimi_state / "proj-1a2b" / "home"
        typed = self.session([Kimi.prompt("fix the guard", 1)], home=launcher_home)
        Kimi.typed(launcher_home, self.cwd, "fix the guard")
        slash = self.session([Kimi.prompt("SKILL-LOADED", 1, origin={
            "kind": "skill_activation", "skillName": "audit", "trigger": "user-slash", "skillArgs": "the guard "})])
        Kimi.typed(self.kimi, self.cwd, "/skill:audit the guard")
        titled = self.session([Kimi.prompt("a task", 1)], title="Fix the guard")
        settled = self.session([Kimi.prompt("a task", 1), Kimi.ended(2)])
        fresh = self.session([Kimi.prompt("a task", 1)])
        # Protocol 1.4 has no end-of-turn record: only an idle hour settles it.
        legacy = self.session([Kimi.prompt("a task", 1)], protocol="1.4", legacy=True)
        self.hist("sync")
        self.assertEqual(self.classes(), {typed: "interactive", slash: "interactive", titled: "interactive",
                                          settled: "worker"})
        self.assertIn("kimi (kimi): 4 indexed", self.hist("status").stdout)
        self.assertIn("; 2 not yet classified, retried each sync", self.hist("status").stdout)
        write_jsonl(Kimi.journal(self.kimi, self.cwd, fresh), [Kimi.ended(3)])
        self.age(legacy)
        self.hist("sync", "--all")
        self.assertEqual({k: v for k, v in self.classes().items() if k in (fresh, legacy)},
                         {fresh: "worker", legacy: "worker"})
        self.assertNotIn("not yet classified", self.hist("status").stdout)
        started = dict(self.db().execute("SELECT native_id, started FROM sessions").fetchall())
        self.assertEqual(started[legacy], "2026-10-01T10:00:00.000Z")
        self.assertEqual(self.texts(slash), [("user", None, "/skill:audit the guard")])

    def test_a_slash_skill_matches_its_typed_line_however_it_was_spelled(self) -> None:
        # Kimi takes `/audit` for `/skill:audit`, and the title comes only after the first turn.
        sessions = []
        for n, line in enumerate(("/audit the guard", "/skill:audit  the guard", "/Audit the guard",
                                  "/SKILL:audit the guard")):
            home = self.kimi_state / f"proj-{n}" / "home"
            sessions.append(self.session([Kimi.prompt("SKILL-LOADED", 1, origin={
                "kind": "skill_activation", "skillName": "audit", "trigger": "user-slash",
                "skillArgs": "the guard"})], home=home))
            Kimi.typed(home, self.cwd, line)
        # Spacing inside what was typed still counts.
        spaced = self.session([Kimi.prompt('Preserve "a b" exactly', 1)])
        Kimi.typed(self.kimi, self.cwd, 'Preserve "a  b" exactly')
        self.hist("sync")
        self.assertEqual(self.classes(), dict.fromkeys(sessions, "interactive"))
        self.assertNotIn(spaced, self.classes())
        self.assertEqual([self.texts(native) for native in sessions],
                         [[("user", None, "/skill:audit the guard")]] * 4)

    def test_a_first_turn_longer_than_the_head_settles_by_the_end_kimi_recorded(self) -> None:
        records = [Kimi.prompt("a task", 1), Kimi.call("Read", {}, "tool_1", 2),
                   Kimi.result("tool_1", "x" * (1 << 20), 2), Kimi.ended(3)]
        settled = self.session(records, lastTurnReason="completed")
        self.hist("sync")
        self.assertEqual(self.classes(), {settled: "worker"})

    def test_malformed_values_stop_or_drop_one_record_and_the_rest_keep_syncing(self) -> None:
        listed = self.session([Kimi.prompt("a task", 1)], protocol=["1.5"], title="Listed")  # type: ignore[arg-type]
        odd = self.session([
            Kimi.prompt("a task", 1), {"type": ["turn.prompt"], "agentId": "main"},
            Kimi.loop({"type": {"name": "tool.call"}}, 2),
            Kimi.prompt([{"type": ["text"], "text": "x"}], 3, origin={"kind": "task"}),
            Kimi.prompt("by whom", 4, origin={"kind": ["user"]}),
        ], lastTurnReason=["completed"])
        future = self.session([Kimi.prompt("a task", 1), {**Kimi.reply("then", 2), "time": 253402300800000}],
                              title="Year 10000")
        endless = self.session([Kimi.prompt("a task", 1)], title="Endless", createdAt=float("inf"))
        good = self.session([Kimi.prompt("still indexed", 1)], title="Good")
        result = self.hist("sync")
        self.assertIn(f"stopped: kimi:{listed}, kimi:{future}", result.stdout)
        status = self.hist("status").stdout
        self.assertIn("unsupported protocol_version ['1.5']", status)
        self.assertIn("record 2 time 253402300800000 is out of range", status)
        self.assertEqual(self.texts(endless), [("user", None, "a task")])
        self.assertEqual(self.texts(good), [("user", None, "still indexed")])
        self.assertNotIn(odd, self.classes())
        self.age(odd)
        self.hist("sync", "--all")
        self.assertEqual(self.texts(odd), [("user", None, "a task")])
        self.assertIn('input_part:["text"]×1, loop:{"name": "tool.call"}×1, origin:["user"]×1, '
                      'type:["turn.prompt"]×1', self.hist("status").stdout)

    def test_a_subagent_names_its_parent_from_its_labels_or_the_legacy_field(self) -> None:
        agents = {
            "main": {"type": "main"},
            "agent-0": {"type": "sub", "labels": {"parentAgentId": "main"}},
            "agent-1": {"type": "sub", "parentAgentId": "main"},
            "agent-2": {"type": "sub", "labels": {"parentAgentId": "agent-0"}},
        }
        main = self.session([Kimi.prompt("split the audit", 1), Kimi.ended(2)], agents=agents)
        for agent in ("agent-0", "agent-1", "agent-2"):
            write_jsonl(Kimi.journal(self.kimi, self.cwd, main, agent), [
                Kimi.metadata(), Kimi.prompt(f"{agent} task", 1, origin={"kind": "system_trigger", "name": "subagent"}),
                Kimi.reply(f"{agent} done", 2)])
        self.hist("sync")
        self.assertEqual(self.classes(), {main: "worker", f"{main}/agent-0": "subagent",
                                          f"{main}/agent-1": "subagent", f"{main}/agent-2": "subagent"})
        parents = dict(self.db().execute(
            "SELECT c.native_id, p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id").fetchall())
        self.assertEqual(parents, {f"{main}/agent-0": main, f"{main}/agent-1": main,
                                   f"{main}/agent-2": f"{main}/agent-0"})
        self.assertEqual(self.texts(f"{main}/agent-2"),
                         [("work", "subagent", "agent-2 task"), ("agent", None, "agent-2 done")])

    def test_a_fork_skips_its_copied_records_and_waits_for_its_state(self) -> None:
        agents = {"main": {"type": "main"}, "agent-0": {"type": "sub", "labels": {"parentAgentId": "main"}}}
        source_main = [Kimi.prompt("plan the audit", 1), Kimi.reply("COPIED-REPLY", 2, uuid=None),
                       Kimi.ended(2, "failed", error={"message": "COPIED-FAILURE"})]
        source_sub = [Kimi.prompt("COPIED-TASK", 1, origin={"kind": "system_trigger", "name": "subagent"})]
        source = self.session(source_main, agents=agents, title="Plan the audit")
        write_jsonl(Kimi.journal(self.kimi, self.cwd, source, "agent-0"), [Kimi.metadata(), *source_sub])
        # Kimi copies the agents' journals, marks each, then writes the fork's state.json.
        fork = Kimi.session_id()
        write_jsonl(Kimi.journal(self.kimi, self.cwd, fork), [
            Kimi.metadata(), *source_main, Kimi.forked("main", 3), Kimi.prompt("now the tests", 4)])
        write_jsonl(Kimi.journal(self.kimi, self.cwd, fork, "agent-0"), [
            Kimi.metadata(), *source_sub, Kimi.forked("agent-0", 3)])
        self.hist("sync")
        self.assertEqual(self.classes(), {source: "interactive", f"{source}/agent-0": "subagent"})
        Kimi.typed(self.kimi, self.cwd, "now the tests")
        Kimi.write_state(self.kimi, self.cwd, fork, title="Fork: Plan the audit", forkedFrom=source, agents={
            **agents, "agent-1": {"type": "sub", "labels": {"parentAgentId": "main"}}})
        # A subagent started after the fork has no marker and skips nothing.
        write_jsonl(Kimi.journal(self.kimi, self.cwd, fork, "agent-1"), [
            Kimi.metadata(), Kimi.prompt("check the tests", 5, origin={"kind": "system_trigger", "name": "subagent"})])
        # A fork of the fork carries both markers; only records after the last are its own.
        nested = Kimi.session_id()
        Kimi.write_state(self.kimi, self.cwd, nested, title="Fork: Fork: Plan the audit", forkedFrom=fork)
        write_jsonl(Kimi.journal(self.kimi, self.cwd, nested), [
            Kimi.metadata(), *source_main, Kimi.forked("main", 3), Kimi.prompt("now the tests", 4),
            Kimi.forked("main", 6), Kimi.prompt("a third way", 7)])
        # A forkedFrom with no marker in the journal is not Kimi's order of writing; it waits.
        unmarked = self.session(source_main, forkedFrom=source)
        self.hist("sync")
        self.assertEqual(self.texts(fork), [("event", None, f"forked from {source}"),
                                            ("user", None, "now the tests")])
        self.assertEqual(self.texts(f"{fork}/agent-0"), [("event", None, f"forked from {source}")])
        self.assertEqual(self.texts(f"{fork}/agent-1"), [("work", "subagent", "check the tests")])
        classes = self.classes()
        # The fork's title is Kimi's, and no typed prompt follows the nested fork's
        # last marker: it waits for an idle hour, then is left unclassified.
        self.assertEqual((classes[fork], classes.get(nested), classes.get(unmarked)), ("interactive", None, None))
        self.age(nested)
        self.age(unmarked)
        self.hist("sync", "--all")
        self.assertEqual(self.classes()[nested], "unclassified")
        self.assertEqual(self.texts(nested), [("event", None, f"forked from {fork}"),
                                              ("user", None, "a third way")])
        self.assertNotIn(unmarked, self.classes())
        copied = self.db().execute(
            "SELECT s.native_id, m.text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.text LIKE '%COPIED-%' AND s.native_id NOT LIKE ?", (f"{source}%",)).fetchall()
        self.assertEqual(copied, [])

    def test_a_shell_command_and_a_plugin_command_are_kept_as_the_user_typed_them(self) -> None:
        native = self.session([
            Kimi.appended("<bash-input>\ngrep -n &quot;a &amp; b&quot; x\n</bash-input>", 1,
                          {"kind": "shell_command", "phase": "input"}),
            Kimi.appended("<bash-stdout>1: a &amp; b\n</bash-stdout><bash-stderr></bash-stderr>", 1,
                          {"kind": "shell_command", "phase": "output"}),
            Kimi.appended("MIRRORED-PROMPT", 2, {"kind": "user"}),
            Kimi.prompt("EXPANDED-PLUGIN-BODY", 3, origin={
                "kind": "plugin_command", "pluginId": "review", "commandName": "diff", "commandArgs": "HEAD~1 ",
                "trigger": "user-slash", "activationId": "a-1"}),
        ])
        Kimi.typed(self.kimi, self.cwd, "/review:diff HEAD~1")
        self.hist("sync")
        self.assertEqual(self.classes(), {native: "interactive"})
        self.assertEqual(self.texts(native), [
            ("user", None, '<bash-input>\ngrep -n "a & b" x\n</bash-input>'),
            ("tool_result", "bash", "<bash-stdout>1: a & b\n</bash-stdout><bash-stderr></bash-stderr>"),
            ("user", None, "/review:diff HEAD~1"),
        ])

    def test_prompts_keep_what_the_user_typed_and_drop_what_the_host_added(self) -> None:
        skill = {"activationId": "a1", "skillName": "audit", "skillArgs": "the guard"}
        native = self.session([
            Kimi.prompt([Kimi.text("HOOK-VIEW", source="user prompt submit hook"), Kimi.text("fix the guard"),
                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,IMAGE-DATA"}}], 1),
            # Kimi 2.6 tags a typed skill's body; older prompts left it untagged, first.
            Kimi.prompt([Kimi.text("SKILL-BODY-TAGGED", source="skill activation"), Kimi.text("/skill:audit now")],
                        2, origin={"kind": "user", "skillActivations": [skill]}),
            Kimi.prompt([Kimi.text("SKILL-BODY-UNTAGGED"), Kimi.text("/skill:audit the guard")], 3,
                        origin={"kind": "user", "skillActivations": [skill]}),
            Kimi.prompt("SKILL-LOADED", 4, origin={"kind": "skill_activation", "skillName": "arxiv",
                                                   "trigger": "user-slash", "skillArgs": "fetch papers"}),
            Kimi.prompt("actually, the tests too", 5, steer=True, origin={"kind": "user", "inTurn": True}),
            Kimi.prompt("SKILL-STEER", 5, steer=True,
                        origin={"kind": "skill_activation", "skillName": "docs", "trigger": "model-tool"}),
            Kimi.prompt("SKILL-LOADED", 5, steer=True,
                        origin={"kind": "skill_activation", "skillName": "docs", "trigger": "user-slash"}),
            Kimi.reply("THINKING", 6, kind="think"),
            Kimi.reply("on it", 6),
            Kimi.call("Bash", {"command": "ls ü"}, "tool_1", 6),
            Kimi.result("tool_1", [{"type": "text", "text": "a.py"},
                                   {"type": "image_url", "image_url": {"url": "IMAGE-DATA"}}], 7),
            Kimi.loop({"type": "step.end", "finishReason": "error"}, 7),
            Kimi.prompt("task t1 finished", 8, origin={"kind": "task"}),
            Kimi.prompt("background job done", 8, steer=True, origin={"kind": "background_task"}),
            Kimi.ended(9, "failed", error={"code": "provider.auth_error", "message": "quota reached"}),
            Kimi.ended(9, "cancelled"),
            {"type": "full_compaction.complete", "time": 1},
        ], title="Fix the guard")
        self.hist("sync")
        self.assertEqual(self.texts(native), [
            ("user", None, "fix the guard[image omitted]"),
            ("user", None, "/skill:audit now"),
            ("user", None, "/skill:audit the guard"),
            ("user", None, "/skill:arxiv fetch papers"),
            ("user", None, "actually, the tests too"),
            ("user", None, "/skill:docs"),
            ("agent", None, "on it"),
            ("tool_call", "Bash", '{"command": "ls ü"}'),
            ("tool_result", "Bash", "a.py\n[image omitted]"),
            ("event", None, "the model step failed"),
            ("event", "task", "task t1 finished"),
            ("event", "background_task", "background job done"),
            ("event", None, "turn failed: quota reached"),
            ("event", None, "turn cancelled"),
            ("event", None, "context compacted"),
        ])
        for marker in (b"HOOK-VIEW", b"IMAGE-DATA", b"SKILL-BODY", b"SKILL-LOADED", b"SKILL-STEER", b"THINKING"):
            self.assertNotStored(marker)
        self.assertNotIn("unknown record types", self.hist("status").stdout)

    def test_kimis_ids_key_messages_and_a_tool_calls_id_keys_nothing(self) -> None:
        agents = {"main": {"type": "main"}, "agent-0": {"type": "sub", "labels": {"parentAgentId": "main"}}}
        calls = [Kimi.call("Read", {"path": "a"}, "tool_9", 4), Kimi.result("tool_9", "contents", 4)]
        native = self.session([
            Kimi.prompt("again", 1, prompt_id=None), Kimi.prompt("again", 2, prompt_id=None),
            Kimi.prompt("keyed", 3, prompt_id="msg_1"),
            Kimi.reply("same", 3, uuid=None), Kimi.reply("same", 3, uuid=None),
            *calls, Kimi.prompt("steered", 5, steer=True, prompt_id="msg_2"),
        ], title="Keys", agents=agents)
        # The provider's call ids repeat in another session and in a sibling agent.
        write_jsonl(Kimi.journal(self.kimi, self.cwd, native, "agent-0"), [Kimi.metadata(), *calls])
        other = self.session(calls, title="Other")
        self.hist("sync")
        rows = self.db().execute(
            "SELECT text, key FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.record >= 0 ORDER BY position", (native,)).fetchall()
        self.assertEqual(rows, [
            ("again", None), ("again", None), ("keyed", "kimi:prompt:msg_1#0"), ("same", None), ("same", None),
            ('{"path": "a"}', None), ("contents", None), ("steered", "kimi:steer:msg_2#0"),
        ])
        for session in (f"{native}/agent-0", other):
            self.assertEqual(self.texts(session), [("tool_call", "Read", '{"path": "a"}'),
                                                   ("tool_result", "Read", "contents")])

    def test_unknown_types_are_counted(self) -> None:
        self.session([
            Kimi.prompt("typed", 1), {"type": "future.record", "time": 1},
            Kimi.loop({"type": "future.event"}, 1), Kimi.reply("x", 1, kind="future_part"),
            Kimi.prompt("UNKNOWN-ORIGIN", 1, origin={"kind": "future_origin"}),
        ], title="Unknown")
        self.hist("sync")
        listed = self.hist("status").stdout
        for kind in ("type:future.record", "loop:future.event", "content_part:future_part", "origin:future_origin"):
            self.assertIn(f"{kind}×1", listed)
        self.assertNotStored(b"UNKNOWN-ORIGIN")

    def test_a_saved_output_is_read_only_from_the_agents_own_directories(self) -> None:
        native = Kimi.session_id()
        agent = Kimi.journal(self.kimi, self.cwd, native).parent
        saved = {"tool-results/Read-tool_1.txt": "WHOLE-RESULT", "tasks/t1/output.log": "WHOLE-TASK-LOG",
                 "elsewhere/out.txt": "OUTSIDE-FILE", "tool-results/Grep-tool_9.txt": "WHOLE-GREP"}
        for relative, text in saved.items():
            (agent / relative).parent.mkdir(parents=True, exist_ok=True)
            (agent / relative).write_text(text)
        (agent / "tool-results" / "link.txt").symlink_to(agent / "elsewhere" / "out.txt")
        notices = [Kimi.saved_output(agent / relative, "preview") for relative in list(saved)[:3]]
        notices.append(Kimi.saved_output(agent / "tool-results" / "link.txt", "preview"))
        notices.append(Kimi.saved_output(agent / "tool-results" / ".." / "elsewhere" / "out.txt", "preview"))
        grep = f"output_path: {agent / 'tool-results' / 'Read-tool_1.txt'}"
        spills = [Kimi.spilled("a long line, cut", agent / "tool-results" / "Grep-tool_9.txt"),
                  Kimi.spilled("a long line, cut", agent / "elsewhere" / "out.txt"),
                  # A quoted pointer is not the last line-start one; a quoted header is not Kimi's.
                  Kimi.spilled("cut", agent / "tool-results" / "Grep-tool_9.txt") + "\nand more output",
                  Kimi.spilled(Kimi.saved_output(agent / "tool-results" / "Read-tool_1.txt", "quoted"),
                               agent / "tool-results" / "Grep-tool_9.txt"),
                  # A tool's own lines after the pointer, as Bash adds its task's.
                  Kimi.spilled("cut", agent / "tasks" / "t1" / "output.log") + "\n\ntask_id: t1\nnext_step: Use TaskOutput"]
        records = [Kimi.prompt("read it", 1)]
        for n, text in enumerate([*notices, grep, *spills]):
            records += [Kimi.call("Read", {}, f"tool_{n}", 2), Kimi.result(f"tool_{n}", text, 2)]
        records.append(Kimi.reply(notices[0], 3))
        records += [Kimi.call("Read", {}, "tool_img", 3), Kimi.result("tool_img", [
            {"type": "text", "text": notices[0]}, {"type": "image_url", "image_url": {"url": "data:"}}], 3)]
        self.session(records, native=native, title="Saved")
        self.hist("sync")
        results = [text for kind, _, text in self.texts(native) if kind == "tool_result"]
        self.assertEqual(results[:2], ["WHOLE-RESULT", "WHOLE-TASK-LOG"])
        self.assertEqual(results[2:-1], [*notices[2:], grep, "WHOLE-GREP", spills[1], spills[2], "WHOLE-GREP",
                                       "WHOLE-TASK-LOG"])
        self.assertEqual(self.texts(native)[-3], ("agent", None, notices[0]))
        self.assertEqual(results[-1], "WHOLE-RESULT\n[image omitted]")
        # A refresh completes only tool results too.
        before = self.texts(native)
        self.hist("refresh")
        self.assertEqual(self.texts(native), before)

    def test_an_unsupported_protocol_stops_its_source_and_an_empty_journal_waits(self) -> None:
        future = self.session([Kimi.prompt("a task", 1)], protocol="2.0", title="Future")
        empty = Kimi.session_id()
        Kimi.write_state(self.kimi, self.cwd, empty, title="Empty")
        Kimi.journal(self.kimi, self.cwd, empty).parent.mkdir(parents=True)
        Kimi.journal(self.kimi, self.cwd, empty).touch()
        result = self.hist("sync")
        self.assertIn(f"stopped: kimi:{future}", result.stdout)
        status = self.hist("status").stdout
        self.assertIn("unsupported protocol_version '2.0'", status)
        self.assertIn("kimi (kimi): 0 indexed", status)
        self.assertIn("1 not yet classified", status)
        write_jsonl(Kimi.journal(self.kimi, self.cwd, empty), [Kimi.metadata(), Kimi.prompt("now", 1)])
        self.hist("sync")
        self.assertEqual(self.texts(empty), [("user", None, "now")])

    def test_a_journal_migrated_to_a_newer_protocol_stops_and_the_rest_keep_syncing(self) -> None:
        old = self.session([Kimi.prompt("first", 1), Kimi.reply("answer", 2)], protocol="1.4", title="Old")
        other = self.session([Kimi.prompt("other", 1)], title="Other")
        self.hist("sync")
        # Kimi 2.6 rewrites an older journal as 1.5 when it resumes it, then appends.
        path = Kimi.journal(self.kimi, self.cwd, old)
        path.write_text(path.read_text().replace('"protocol_version": "1.4"', '"protocol_version": "1.5"'))
        write_jsonl(path, [Kimi.prompt("after resume", 3)])
        write_jsonl(Kimi.journal(self.kimi, self.cwd, other), [Kimi.prompt("still synced", 2)])
        result = self.hist("sync")
        self.assertIn(f"stopped: kimi:{old}", result.stdout)
        status = self.hist("status").stdout
        self.assertIn("1 stopped", status)
        self.assertIn(f"kimi:{old}: transcript changed under its indexed prefix", status)
        self.assertEqual([text for _, _, text in self.texts(old)], ["first", "answer"])
        self.assertEqual([text for _, _, text in self.texts(other)], ["other", "still synced"])


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
        # The marker straddles the 30,000th character, where earlier versions split a long task into parts.
        long_task = "audit the guard " + "x" * (30_000 - 26) + " " + marker + "\n\nDelegate task id: a1"
        self.assertLess(long_task.index(marker), 30_000)
        self.assertGreater(long_task.index(marker) + len(marker), 30_000)
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
        self.command_code(unknown, f"audit the guard [hist-parent grok:{CLAUDE_PARENT}]")
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
