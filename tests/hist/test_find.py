"""`find`: escaped output, snippets, filters, the first line, and leaving out the caller."""

import json
import os
import re
import shlex
import unittest
from pathlib import Path

from hist.caller import Caller, bounded, detect, direct_parent
from hist.config import Source
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

    def caller_is(self, session: str) -> None:
        """Make this test process, the parent of every `hist` it runs, a Claude
        Code process running `session`."""

        sessions = self.base / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": session}))

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

    def test_filters_narrow_inside_the_search(self) -> None:
        everything = hits(self.hist("find", "zebra", "--all", "--include-self").stdout)
        # Five messages, and the event that the subagent started in its parent's timeline.
        self.assertEqual(len(everything), 6)
        other = hits(self.hist("find", "zebra", "--session", OTHER[-8:]).stdout)
        self.assertEqual(len(other), 2)
        self.assertTrue(all(f"s:{OTHER[-8:]}" in line for line in other))
        replies = hits(self.hist("find", "zebra", "--all", "--kind", "agent").stdout)
        self.assertEqual(len(replies), 2)
        window = self.hist(
            "find", "zebra", "--all", "--since", "2026-10-01T10:20Z", "--until", "2026-10-01T12:21+02:00"
        ).stdout
        (line,) = hits(window)
        self.assertIn("zebra in another session", line)
        bad = self.hist("find", "zebra", "--kind", "reply", check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--kind 'reply' is not one of", bad.stderr)

    def test_human_leaves_out_the_tasks_agents_wrote(self) -> None:
        users = hits(self.hist("find", "zebra", "--all", "--include-self", "--kind", "user").stdout)
        (task,) = [line for line in users if "zebra from its subagent" in line]
        humans = hits(self.hist("find", "zebra", "--all", "--include-self", "--kind", "human").stdout)
        self.assertEqual(sorted(humans), sorted(line for line in users if line != task))
        mixed = hits(self.hist("find", "zebra", "--all", "--include-self", "--kind", "human,agent").stdout)
        self.assertEqual(len(mixed), len(humans) + 2)
        subagent = re.search(r"\[s:(\w+) ", task).group(1)
        self.assertEqual(self.hist("read", subagent, "--kind", "human").stdout, "no messages\n")
        own = self.hist("read", OTHER[-8:], "--kind", "human").stdout
        self.assertIn("zebra in another session", own)
        self.assertNotIn("zebra reply", own)

    def test_human_leaves_out_messages_injected_in_the_users_turn(self) -> None:
        cwd = str(self.repo)
        teammate = (
            "Another Claude session sent a message:\n<teammate-message teammate_id=\"lane-a\" color=\"blue\">\n"
            '{"type":"idle_notification","failureReason":"zebra limit"}\n</teammate-message>\n\n'
            "This came from another Claude session — not typed by your user."
        )
        quoting = "why does every Another Claude session sent a message: zebra notice count as mine?"
        write_jsonl(Claude.project(self.claude, cwd) / f"{SESSION}.jsonl", [
            Claude.user("u3", SESSION, cwd, teammate, 32),
            Claude.user("u4", SESSION, cwd, quoting, 33),
        ])
        write_jsonl(Codex.path(self.codex, OTHER), [
            Codex.user("<codex_delegation>\n  <input>zebra coordination only</input>\n</codex_delegation>", 23),
            Codex.user("<heartbeat>\n  <instructions>continue the zebra goal</instructions>\n</heartbeat>", 24),
        ])
        self.hist("sync")
        humans = "\n".join(hits(self.hist("find", "zebra", "--all", "--include-self", "--kind", "human").stdout))
        self.assertIn("count as mine", humans)
        self.assertIn("zebra in another session", humans)
        for injected in ("teammate-message", "codex_delegation", "heartbeat"):
            self.assertNotIn(injected, humans)
        users = "\n".join(hits(self.hist("find", "zebra", "--all", "--include-self", "--kind", "user").stdout))
        self.assertIn("teammate-message", users)
        # History indexed before whole messages stored a long one in parts: a later part of the teammate's
        # record carries none of its wrapper. Make the owner's next message such a part.
        continuation = "zebra continued from the teammate's long report"
        write_jsonl(Claude.project(self.claude, cwd) / f"{SESSION}.jsonl", [
            Claude.user("u5", SESSION, cwd, continuation, 34),
        ])
        self.hist("sync")
        db = self.db()
        with db:
            ((source, record),) = db.execute(
                "SELECT source_id, record FROM messages WHERE text LIKE 'Another Claude session%'").fetchall()
            db.execute("UPDATE messages SET record = ?, part = 1 WHERE source_id = ? AND text = ?",
                       (record, source, continuation))
        self.assertNotIn(continuation, self.hist("find", "zebra", "--all", "--include-self", "--kind", "human").stdout)
        self.assertNotIn(continuation, self.hist("read", SESSION[-8:], "--kind", "human").stdout)
        for session, kept, injected in ((SESSION[-8:], "count as mine", "teammate-message"),
                                        (OTHER[-8:], "zebra in another session", "codex_delegation")):
            for flags in ((), ("--tail", "3")):
                with self.subTest(session=session, flags=flags):
                    own = self.hist("read", session, "--kind", "human", *flags).stdout
                    self.assertIn(kept, own)
                    self.assertNotIn(injected, own)
                    self.assertNotIn("heartbeat", own)

    def test_continuations_repeat_the_filters(self) -> None:
        for n in range(12):
            write_jsonl(Codex.path(self.codex, OTHER), [Codex.user(f"zebra again {n}", 22)])
        self.hist("sync")
        args = ["find", "zebra", "--session", OTHER[-8:], "--kind", "user", "--since", "2026-10-01"]
        found = []
        while True:
            lines = self.hist(*args).stdout.splitlines()
            header = re.fullmatch(r"(\d+) hits?, newest first(?:; more: (hist .+))?", lines[0])
            found += hits("\n".join(lines))
            self.assertEqual(int(header.group(1)), len(hits("\n".join(lines))))
            if header.group(2) is None:
                break
            self.assertEqual(lines[-1], f"more: {header.group(2)}")
            self.assertRegex(
                header.group(2),
                f'^hist find "zebra" --all --session {OTHER[-8:]} --kind user '
                r"--since 2026-10-01T00:00:00\.000Z --after \d+$",
            )
            args = shlex.split(header.group(2))[1:]
        self.assertEqual(len(found), 13)
        self.assertEqual(len(set(found)), 13)

    def test_the_calling_session_and_its_subagents_are_left_out(self) -> None:
        self.caller_is(SESSION)
        out = self.hist("find", "zebra", "--all").stdout
        self.assertEqual(len(hits(out)), 3, out)
        self.assertFalse([line for line in hits(out) if f"s:{SESSION[-8:]}" in line or "subagent" in line])
        self.assertIn(f"(left out this session, s:{SESSION[-8:]}, and the sessions it started;", out.splitlines()[1])
        self.assertEqual(len(hits(self.hist("find", "zebra", "--all", "--include-self").stdout)), 6)
        own = hits(self.hist("find", "zebra", "--session", "self").stdout)
        self.assertEqual(len(own), 3)
        self.assertIn("zebra from the calling session", self.hist("read", "self").stdout)
        # A person at a terminal sees everything.
        self.assertEqual(len(hits(self.hist("find", "zebra", "--all", terminal=True).stdout)), 6)

    def test_self_needs_a_known_caller(self) -> None:
        result = self.hist("read", "self", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot tell which session is calling", result.stderr)


class DetectTest(unittest.TestCase):
    """The caller rules that need a process table no test can build."""

    sources = (
        Source("codex", "codex", (Path("/h/.codex"),)),
        Source("claude", "claude", (Path("/h/.claude/projects"),)),
    )

    def detect(
        self, table: dict[int, tuple[int, str]], environ: dict[str, str], files: tuple[str, ...] = ()
    ) -> Caller | None:
        return detect(self.sources, pid=10, environ=environ, process=table.get, open_files=lambda pid: list(files))

    def test_codex_uses_its_thread_variable_or_its_one_open_rollout(self) -> None:
        table = {10: (11, "/bin/zsh"), 11: (12, "/usr/local/bin/codex"), 12: (1, "claude")}
        self.assertEqual(self.detect(table, {"CODEX_THREAD_ID": "t-1", "CLAUDE_CODE_SESSION_ID": "c"}),
                         Caller("codex", "t-1", "codex-env"))
        rollout = "/h/.codex/sessions/2026/10/01/rollout-2026-10-01T10-00-00-019a0000-0000-7000-8000-000000000001.jsonl"
        self.assertEqual(self.detect(table, {}, (rollout, "/dev/null")),
                         Caller("codex", "019a0000-0000-7000-8000-000000000001", "codex-rollout"))

    def test_codex_without_a_thread_identifies_nobody(self) -> None:
        table = {10: (11, "codex"), 11: (1, "claude")}
        two = tuple(f"/h/.codex/sessions/rollout-x-019a0000-0000-7000-8000-00000000000{n}.jsonl" for n in (1, 2))
        self.assertIsNone(self.detect(table, {"CLAUDE_CODE_SESSION_ID": "c"}, two))
        self.assertIsNone(self.detect(table, {"CLAUDE_CODE_SESSION_ID": "c"}))

    def test_without_a_host_the_environment_decides_unless_it_conflicts(self) -> None:
        table = {10: (1, "/bin/sh")}
        self.assertEqual(self.detect(table, {"CLAUDE_CODE_SESSION_ID": "c"}), Caller("claude", "c", "claude-env"))
        self.assertIsNone(self.detect(table, {"CLAUDE_CODE_SESSION_ID": "c", "CODEX_THREAD_ID": "t"}))
        self.assertIsNone(self.detect(table, {}))

    def test_kimi_identifies_nobody_and_stops_the_walk(self) -> None:
        # Kimi exports no session id, so the variables it inherits name whoever launched it.
        environ = {"CODEX_THREAD_ID": "t-1", "CLAUDE_CODE_SESSION_ID": "c"}
        for name in ("/Users/x/.kimi-code/bin/kimi", "kimi-code"):
            table = {10: (11, "/bin/sh"), 11: (12, name), 12: (1, "codex")}
            self.assertIsNone(self.detect(table, environ), name)

    def test_a_kimi_parent_is_the_next_host_up_unless_it_is_another_kimi(self) -> None:
        def parent(table: dict[int, tuple[int, str]], environ: dict[str, str]) -> Caller | None:
            return direct_parent(self.sources, "kimi", pid=10, environ=environ, process=table.get,
                                 open_files=lambda pid: [])

        under_codex = {10: (11, "/bin/sh"), 11: (12, "kimi"), 12: (13, "/bin/zsh"), 13: (1, "codex")}
        self.assertEqual(parent(under_codex, {"CODEX_THREAD_ID": "t-1"}), Caller("codex", "t-1", "codex-env"))
        self.assertIsNone(parent(under_codex, {}))
        # Codex, then Kimi A, then Kimi B: B's inherited thread names Codex, not A.
        nested = {10: (11, "kimi"), 11: (12, "kimi-code"), 12: (1, "codex")}
        self.assertIsNone(parent(nested, {"CODEX_THREAD_ID": "t-1"}))
        # Kimi alone, or no Kimi at all: the environment never decides.
        self.assertIsNone(parent({10: (11, "kimi"), 11: (1, "/bin/zsh")}, {"CLAUDE_CODE_SESSION_ID": "c"}))
        self.assertIsNone(parent({10: (11, "/bin/sh"), 11: (1, "codex")}, {"CODEX_THREAD_ID": "t-1"}))

    def test_a_bounded_walk_shares_one_deadline_and_past_it_names_nobody(self) -> None:
        table = {10: (11, "/bin/sh"), 11: (12, "kimi"), 12: (13, "/bin/zsh"), 13: (1, "codex")}
        rollout = "/h/.codex/sessions/rollout-x-019a0000-0000-7000-8000-000000000001.jsonl"

        def walk(seconds: float) -> tuple[Caller | None, list[tuple[str, int, float]]]:
            now, given = [0.0], []

            def slow(name: str, answer):  # each call takes 0.3 s of the budget
                def call(pid: int, timeout: float):
                    given.append((name, pid, round(timeout, 6)))
                    now[0] += 0.3
                    return answer(pid)
                return call

            process, open_files = bounded(seconds, process=slow("ps", table.get),  # the clock starts at 0
                                          open_files=slow("lsof", lambda pid: [rollout]), clock=lambda: now[0])
            return direct_parent(self.sources, "kimi", pid=10, environ={}, process=process,
                                 open_files=open_files), given

        found, given = walk(10)
        self.assertEqual(found, Caller("codex", "019a0000-0000-7000-8000-000000000001", "codex-rollout"))
        self.assertEqual([timeout for _, _, timeout in given], [10, 9.7, 9.4, 9.1, 8.8])
        # The fourth ps leaves 0.1 s of a 1 s budget; lsof is not run, and nobody is named.
        found, given = walk(1)
        self.assertIsNone(found)
        self.assertEqual(given, [("ps", 10, 1), ("ps", 11, 0.7), ("ps", 12, 0.4), ("ps", 13, 0.1)])
