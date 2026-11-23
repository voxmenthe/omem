"""`find`: escaped output, snippets, filters, the first line, and leaving out the caller."""

import json
import os
import re
import shlex
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from hist import commands
from hist.caller import Caller, bounded, detect, direct_parent
from hist.commands import FIND_HITS, FIND_SCAN, LIST_BYTES, Filters
from hist.config import Source
from hist.store import index_message, search_text
from tests.hist.fixtures import Claude, Codex, HistCase, git_repo, write_jsonl

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


def _insert_messages(
    db: sqlite3.Connection,
    timeline_id: int,
    session_id: int,
    source_id: int,
    texts: list[str],
    *,
    start_position: int = 0,
    start_record: int = 1,
) -> list[int]:
    """Insert synthetic messages + FTS5 rows directly for large boundary tests."""

    ids: list[int] = []
    with db:
        for offset, text in enumerate(texts):
            pos = start_position + offset
            cur = db.execute(
                "INSERT INTO messages (timeline_id, position, source_id, record, part, "
                "session_id, kind, tool, time, text) VALUES (?, ?, ?, ?, 0, ?, 'user', NULL, ?, ?)",
                (timeline_id, pos, source_id, start_record + offset, session_id,
                 "2026-10-01T10:00:00.000Z", text),
            )
            msg_id = int(cur.lastrowid)
            index_message(db, msg_id, text)
            ids.append(msg_id)
        db.execute(
            "UPDATE timelines SET length = ?, last_time = '2026-10-01T10:00:00.000Z' WHERE id = ?",
            (start_position + len(texts), timeline_id),
        )
        db.execute(
            "UPDATE sessions SET first_position = coalesce(first_position, ?), "
            "last_position = ?, message_count = message_count + ? WHERE id = ?",
            (start_position, start_position + len(texts) - 1, len(texts), session_id),
        )
    return ids


class FindPatternTest(unittest.TestCase):
    """Lossless FTS5 query construction for separators, underscores, Unicode, and short tails."""

    def test_unicode61_splitting_closed_ascii_and_short_tail_modes(self) -> None:
        # Closed ASCII separators omit trailing `*`.
        for query_text, expected in (
            ("error:", '"error"'),
            ("my_var_", '"my var"'),
            ("foo/bar/", '"foo bar"'),
            ("call(arg)", '"call arg"'),
            ("cam -p ", '"cam p"'),
            ("cafe\u0301:", '"cafe"'),
        ):
            with self.subTest(closed=query_text):
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=1)[0], expected)
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=2)[0], expected)

        # Decomposed Unicode combining mark is NOT treated as an ASCII separator.
        self.assertEqual(commands._find_pattern("cafe\u0301", short_tail_max=2)[0], '"cafe"*')
        # Single >=3-char token keeps prefix `*`.
        self.assertEqual(commands._find_pattern("sub", short_tail_max=2)[0], '"sub"*')
        # Combining mark between letters is NOT an ASCII separator, so tail is not dropped.
        self.assertEqual(commands._find_pattern("foo\u0301a", short_tail_max=2)[0], '"foo a"*')

        # 1-char open tails drop the trailing token in both modes (including `_` and curly apostrophe).
        for query_text, expected in (
            ("cam -p", '"cam"'),
            ("my_var_a", '"my var"'),
            ("screenshot\u2019s", '"screenshot"'),
        ):
            with self.subTest(tail1=query_text):
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=1)[0], expected)
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=2)[0], expected)

        # 2-char open tails depend on short_tail_max (1 vs 2).
        for query_text, expected_1, expected_2 in (
            ("git co", '"git co"*', '"git"'),
            ("2026-11-20", '"2026 11 20"*', '"2026 11"'),
            ("to the co", '"to the co"*', '"to the"'),
            ("objects.py", '"objects py"*', '"objects"'),
        ):
            with self.subTest(tail2=query_text):
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=1)[0], expected_1)
                self.assertEqual(commands._find_pattern(query_text, short_tail_max=2)[0], expected_2)

        # Underscore-joined short identifiers like `_ts_ic` stay accepted without dropping tail
        # (no preceding >=3-char token); underscores alone or only short words are rejected.
        self.assertEqual(commands._find_pattern("_ts_ic", short_tail_max=2)[0], '"ts ic"*')
        for bad in ("___", "ab", "a-b", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(commands.UsageError):
                    commands._find_pattern(bad)


class FindSemanticsAndBoundsTest(HistCase):
    """End-to-end lossless short-tail/separator search and FTS5 [lo, hi] rowid bounding."""

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        write_jsonl(Codex.path(self.codex, THREAD), [
            Codex.meta(THREAD, cwd, "cli", self.origin, minute=1),
            Codex.user("run cam -p on the branch", 1, 1),
            Codex.user("try git commit after git checkout", 1, 2),
            Codex.user("timestamp 2026-11-20T10:00:00Z logged", 1, 3),
            Codex.user("reading my_var_alpha and my_var_a values", 1, 4),
            Codex.user("error: bad state vs errors ignored", 1, 5),
            Codex.user("walk to the corner store", 1, 6),
            Codex.user("visit the cafe\u0301teria today", 1, 7),
            Codex.user("launch a subagent worker", 1, 8),
            Codex.user("import objects.py helper", 1, 9),
            Codex.user("check _ts_ic and screenshot\u2019s dimensions", 1, 10),
        ])
        other_repo = git_repo(self.base / "work" / "otherproj", "git@github.com:me/otherproj.git")
        write_jsonl(Codex.path(self.codex, OTHER), [
            Codex.meta(OTHER, str(other_repo), "cli", "https://github.com/me/otherproj.git", minute=20),
            Codex.user("error: other timeline hit", 20, 1),
            Codex.user("subagent in otherproj", 20, 2),
        ])
        project = Claude.project(self.claude, cwd)
        write_jsonl(project / f"{SESSION}.jsonl", [
            Claude.user("u1", SESSION, cwd, "treehit from parent session", 30),
        ])
        write_jsonl(project / SESSION / "subagents" / f"agent-{AGENT}.jsonl", [
            Claude.user("u2", SESSION, cwd, "treehit from child sidechain", 31, isSidechain=True, agentId=AGENT),
        ])
        self.hist("sync")

    def test_separator_and_short_tail_queries_are_lossless_in_both_modes(self) -> None:
        db = self.db()
        tl = commands.resolve_timeline(db, "proj", self.repo)
        cases = [
            ("cam -p", "run cam -p on the branch", 1),
            ("git co", "try git commit after git checkout", 1),
            ("2026-11-20", "2026-11-20T10:00:00Z", 1),
            ("my_var_a", "my_var_alpha", 1),
            ("error:", "error: bad state", 1),
            ("to the co", "to the corner", 1),
            ("cafe\u0301", "cafe\u0301teria", 1),
            ("sub", "subagent worker", 1),
            ("objects.py", "objects.py helper", 1),
            ("_ts_ic", "check _ts_ic", 1),
            ("screenshot\u2019s", "screenshot\u2019s dimensions", 1),
        ]
        for tail_max in (1, 2):
            with mock.patch.object(commands, "_SHORT_TAIL_MAX", tail_max):
                for needle, expected_fragment, count in cases:
                    with self.subTest(short_tail_max=tail_max, needle=needle):
                        out = commands.find(db, needle, tl, None)
                        found = hits(out)
                        self.assertEqual(len(found), count, out)
                        self.assertIn(expected_fragment, found[0])
                # Closed separator `"error:"` does NOT match `"errors"`.
                self.assertEqual(commands.find(db, "errors:", tl, None), "no matches\n")

    def test_rowid_bounds_use_covering_index_and_short_circuit_empty_scopes(self) -> None:
        db = self.db()
        tl = commands.resolve_timeline(db, "proj", self.repo)
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        out = commands.find(db, "error:", tl, None)
        db.set_trace_callback(None)
        self.assertEqual(len(hits(out)), 1, out)
        # Verify covering-index ORDER BY position queries (never min(id)/max(id)) and bounded FTS5.
        bound_sql = [s for s in statements if "SELECT id FROM messages" in s]
        self.assertEqual(len(bound_sql), 2, bound_sql)
        self.assertTrue(all("ORDER BY position" in s for s in bound_sql), bound_sql)
        self.assertFalse(any("min(id)" in s.lower() or "max(id)" in s.lower() for s in statements), statements)
        for sql in bound_sql:
            plan = " ".join(str(r) for r in db.execute(f"EXPLAIN QUERY PLAN {sql}"))
            self.assertIn("COVERING INDEX", plan)
        fts_sql = [s for s in statements if "FROM search" in s]
        self.assertEqual(len(fts_sql), 1, fts_sql)
        self.assertIn("search.rowid >= ", fts_sql[0])
        self.assertIn("search.rowid < ", fts_sql[0])

        # Empty timeline short-circuits without touching FTS5 and preserves caller note.
        with db:
            cur = db.execute(
                "INSERT INTO timelines (name, kind, identity, display, length) "
                "VALUES ('empty', 'repository', 'repo:empty', 'empty', 0)"
            )
            empty_id = int(cur.lastrowid)
        empty_tl = commands.timeline_named(db, "empty")
        self.assertEqual(empty_tl.id, empty_id)
        statements.clear()
        db.set_trace_callback(statements.append)
        empty_out = commands.find(db, "error:", empty_tl, None, Filters(note="(caller note)"))
        db.set_trace_callback(None)
        self.assertEqual(empty_out, "no matches\n(caller note)\n")
        self.assertFalse(any("FROM search" in s for s in statements), statements)

        # `--after` at or below timeline lo also short-circuits without touching FTS5.
        lo, _ = commands._timeline_bounds(db, tl.id)  # type: ignore[misc]
        statements.clear()
        db.set_trace_callback(statements.append)
        below_out = commands.find(db, "error:", tl, lo)
        db.set_trace_callback(None)
        self.assertEqual(below_out, "no matches\n")
        self.assertFalse(any("FROM search" in s for s in statements), statements)

    def test_session_tree_bounding_combined_scope_and_over_64_sessions(self) -> None:
        db = self.db()
        # Session tree includes parent + subagent (whose messages live on a separate worker timeline).
        filters = commands.find_filters(db, session=SESSION[-8:])
        self.assertEqual(len(filters.sessions or ()), 2)
        out = commands.find(db, "treehit", None, None, filters)
        found = hits(out)
        # Parent message, subagent message, and worker-started event in parent's timeline.
        self.assertEqual(len(found), 3, out)

        # Combined timeline + session scope intersects bounds cleanly.
        proj = commands.resolve_timeline(db, "proj", self.repo)
        other_tl = commands.resolve_timeline(db, "otherproj", self.repo)
        proj_only = commands.find(db, "treehit", proj, None, filters)
        self.assertEqual(len(hits(proj_only)), 2, proj_only)
        # Disjoint timeline + session returns no matches cleanly.
        self.assertEqual(commands.find(db, "treehit", other_tl, None, filters), "no matches\n")

        # Empty session tree short-circuits without touching FTS5.
        with db:
            cur = db.execute(
                "INSERT INTO sessions (adapter, native_id, label, class, alias, timeline_id, started) "
                "VALUES ('codex', 'empty-sess', 'codex', 'interactive', 'emptys01', ?, '2026-10-01T10:00:00Z')",
                (proj.id,),
            )
            empty_sess = int(cur.lastrowid)
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        self.assertEqual(commands.find(db, "treehit", None, None, Filters(sessions=(empty_sess,))), "no matches\n")
        db.set_trace_callback(None)
        self.assertFalse(any("FROM search" in s for s in statements), statements)

        # > 64 sessions skips session bounding (no search.rowid >= ?) and still returns hits.
        session_ids = tuple(filters.sessions or ()) + tuple(range(10_000, 10_065))
        self.assertGreater(len(session_ids), commands._BOUND_SESSIONS_MAX)
        statements.clear()
        db.set_trace_callback(statements.append)
        wide_out = commands.find(db, "treehit", None, None, Filters(sessions=session_ids))
        db.set_trace_callback(None)
        self.assertEqual(len(hits(wide_out)), 3, wide_out)
        fts_sql = [s for s in statements if "FROM search" in s]
        self.assertEqual(len(fts_sql), 1)
        self.assertNotIn("search.rowid >=", fts_sql[0])

        # `--kind human` + caller exclusion still work under timeline bounding.
        human_filters = commands.find_filters(db, kinds="human", include_self=True)
        human_out = commands.find(db, "treehit", proj, None, human_filters)
        self.assertEqual(len(hits(human_out)), 1, human_out)
        self.assertIn("parent session", human_out)


class FindStreamingAndBudgetTest(HistCase):
    """Streaming cursor early exit/cleanup, FIND_SCAN boundaries, and LIST_BYTES enforcement."""

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        write_jsonl(Codex.path(self.codex, THREAD), [
            Codex.meta(THREAD, str(self.repo), "cli", self.origin, minute=1),
            Codex.user("seed message", 1, 1),
        ])
        self.hist("sync")
        db = self.db()
        self.tl_id, self.sess_id, self.src_id = db.execute(
            "SELECT timeline_id, session_id, source_id FROM messages LIMIT 1"
        ).fetchone()
        self.next_pos = db.execute(
            "SELECT length FROM timelines WHERE id = ?", (self.tl_id,)
        ).fetchone()[0]

    def test_streaming_fetches_only_needed_rows_and_closes_cursor_in_finally(self) -> None:
        db = self.db()
        _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            [f"streamneedle item {i}" for i in range(60)],
            start_position=self.next_pos, start_record=100,
        )

        # Instrument cursor iteration via a thin proxy around the real sqlite3 connection.
        stats = {"fts_yielded": 0, "fts_closed": 0, "fts_fetchall": 0}

        class TrackingCursor:
            def __init__(self, inner: sqlite3.Cursor, is_fts: bool) -> None:
                self._inner = inner
                self._is_fts = is_fts

            def __iter__(self):
                return self

            def __next__(self):
                row = next(self._inner)
                if self._is_fts:
                    stats["fts_yielded"] += 1
                return row

            def fetchone(self):
                return self._inner.fetchone()

            def fetchall(self):
                if self._is_fts:
                    stats["fts_fetchall"] += 1
                return self._inner.fetchall()

            def close(self) -> None:
                if self._is_fts:
                    stats["fts_closed"] += 1
                self._inner.close()

        class TrackingConnection:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            def execute(self, sql: str, parameters=()):
                return TrackingCursor(self._inner.execute(sql, parameters), "FROM search" in sql)

        raw = sqlite3.connect(self.home / "hist.db")
        self.addCleanup(raw.close)
        conn = TrackingConnection(raw)
        tl = commands.resolve_timeline(raw, "proj", self.repo)
        out = commands.find(conn, "streamneedle", tl, None)  # type: ignore[arg-type]
        self.assertEqual(len(hits(out)), FIND_HITS, out)
        # 10-hit stop emits exact 10th hit message_id (not +1) and stops streaming immediately.
        tenth_id = raw.execute(
            "SELECT id FROM messages WHERE text LIKE 'streamneedle item %' ORDER BY id DESC LIMIT 1 OFFSET 9"
        ).fetchone()[0]
        self.assertTrue(out.splitlines()[-1].endswith(f"--after {tenth_id}"), out)
        self.assertEqual(stats["fts_fetchall"], 0)
        self.assertEqual(stats["fts_yielded"], FIND_HITS)
        self.assertEqual(stats["fts_closed"], 1)

        # Even when an exception occurs inside the loop, finally: still closes the cursor.
        stats["fts_yielded"] = 0
        stats["fts_closed"] = 0
        with mock.patch.object(commands, "search_text", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                commands.find(conn, "streamneedle", tl, None)  # type: ignore[arg-type]
        self.assertEqual(stats["fts_yielded"], 1)
        self.assertEqual(stats["fts_closed"], 1)

    def test_scan_limit_boundaries_1999_2000_2001_and_exact_cursors(self) -> None:
        # FTS5 phrase `"alpha beta"*` matches `"alpha beta"`, while literal regex `"alpha-beta"` rejects it.
        db = self.db()
        tl = commands.resolve_timeline(db, "proj", self.repo)

        # 1) 1,999 non-matching candidates: below FIND_SCAN -> no continuation.
        ids = _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            ["alpha beta decoy"] * (FIND_SCAN - 1),
            start_position=self.next_pos, start_record=1000,
        )
        tl = commands.resolve_timeline(db, "proj", self.repo)
        self.assertEqual(commands.find(db, "alpha-beta", tl, None), "no matches\n")

        # 2) Add 1 more decoy (total 2,000 candidates): hits FIND_SCAN with 0 hits and cursor at oldest id.
        ids += _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            ["alpha beta decoy"],
            start_position=self.next_pos + len(ids), start_record=1000 + len(ids),
        )
        tl = commands.resolve_timeline(db, "proj", self.repo)
        out_2000 = commands.find(db, "alpha-beta", tl, None)
        lines = out_2000.splitlines()
        self.assertEqual(
            lines[0],
            f'0 hits, newest first, in the newest {FIND_SCAN} candidates; more: hist find "alpha-beta" proj --after {ids[0]}',
        )
        self.assertEqual(lines[-1], f'more: hist find "alpha-beta" proj --after {ids[0]}')
        self.assertLessEqual(len(out_2000.encode()), LIST_BYTES)

        # 3) 2,001 candidates where the oldest (lowest id) row IS a real match:
        #    Rewrite ids[0] to match `"alpha-beta"` and add 1 more decoy at the top so 2,000 decoys sit above it.
        with db:
            db.execute("UPDATE messages SET text = ? WHERE id = ?", ("real alpha-beta match", ids[0]))
            db.execute("DELETE FROM search WHERE rowid = ?", (ids[0],))
            index_message(db, ids[0], "real alpha-beta match")
        ids += _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            ["alpha beta decoy"],
            start_position=self.next_pos + len(ids), start_record=1000 + len(ids),
        )
        tl = commands.resolve_timeline(db, "proj", self.repo)
        page1 = commands.find(db, "alpha-beta", tl, None)
        self.assertIn(f"0 hits, newest first, in the newest {FIND_SCAN} candidates; more: ", page1.splitlines()[0])
        self.assertIn(f"--after {ids[1]}", page1.splitlines()[0])
        page2 = commands.find(db, "alpha-beta", tl, ids[1])
        self.assertEqual(page2.splitlines()[0], "1 hit, newest first")
        self.assertIn("real alpha-beta match", page2)

        # 4) Mixed hits + decoys hitting FIND_SCAN (5 hits inside 2,000 candidates): cursor is last_scanned_id.
        with db:
            for hit_id in ids[-5:]:
                db.execute("UPDATE messages SET text = ? WHERE id = ?", ("real alpha-beta match", hit_id))
                db.execute("DELETE FROM search WHERE rowid = ?", (hit_id,))
                index_message(db, hit_id, "real alpha-beta match")
        mixed = commands.find(db, "alpha-beta", tl, None)
        self.assertEqual(len(hits(mixed)), 5, mixed)
        self.assertIn(f"5 hits, newest first, in the newest {FIND_SCAN} candidates; more: ", mixed.splitlines()[0])
        self.assertTrue(mixed.splitlines()[-1].endswith(f"--after {ids[1]}"), mixed)

    def test_500_byte_literal_never_exceeds_list_bytes_and_oversized_continuation_makes_progress(self) -> None:
        db = self.db()
        # 500-byte literal with multi-byte UTF-8 and caller-exclusion note.
        literal_500 = "longmarker " + "é" * 244 + "z"
        self.assertEqual(len(literal_500.encode()), 500)
        msg_ids = _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            [f"prefix {i:02d} {literal_500} trailing context " + ("w" * 120) for i in range(15)],
            start_position=self.next_pos, start_record=5000,
        )
        tl = commands.resolve_timeline(db, "proj", self.repo)
        filters = Filters(note="(left out this session, s:deadbeef, and the sessions it started; --include-self searches them too)")
        seen_indices: list[int] = []
        after: int | None = None
        pages = 0
        while True:
            out = commands.find(db, literal_500, tl, after, filters)
            pages += 1
            self.assertLessEqual(len(out.encode("utf-8")), LIST_BYTES, f"page {pages} overflowed: {len(out.encode())}")
            page_hits = hits(out)
            self.assertGreater(len(page_hits), 0, out)
            for line in page_hits:
                seen_indices.append(int(re.search(r"prefix (\d+) ", line).group(1)))
            m = re.search(r"--after (\d+)$", out.splitlines()[-1])
            if m is None:
                break
            after = int(m.group(1))
        self.assertGreater(pages, 1)
        self.assertEqual(seen_indices, list(range(14, -1, -1)))

        # Verify byte-budget cursor is exact `message_id + 1` (so overflowed hit starts next page).
        first_page = commands.find(db, literal_500, tl, None, filters)
        first_count = len(hits(first_page))
        self.assertLess(first_count, FIND_HITS)
        cursor_id = int(re.search(r"--after (\d+)$", first_page.splitlines()[-1]).group(1))
        overflow_hit_id = msg_ids[len(msg_ids) - 1 - first_count]
        self.assertEqual(cursor_id, overflow_hit_id + 1)

        # Extreme edge case: continuation string ALONE exceeds LIST_BYTES (2,500-byte literal).
        huge = "hugemarker " + ("q" * 2489)
        self.assertGreater(len(huge.encode()) * 2, LIST_BYTES)
        huge_ids = _insert_messages(
            db, self.tl_id, self.sess_id, self.src_id,
            [f"huge {i} {huge}" for i in range(3)],
            start_position=self.next_pos + len(msg_ids), start_record=6000,
        )
        tl = commands.resolve_timeline(db, "proj", self.repo)
        collected: list[int] = []
        after = None
        for expected_step in range(3):
            out = commands.find(db, huge, tl, after)
            page_hits = hits(out)
            # Preserves single-hit forward progress (`and hits`) without crashing or looping.
            self.assertEqual(len(page_hits), 1, out)
            collected.append(int(re.search(r"huge (\d+) ", page_hits[0]).group(1)))
            m = re.search(r"--after (\d+)$", out.splitlines()[-1])
            if expected_step < 2:
                self.assertIsNotNone(m, out)
                after = int(m.group(1))
                self.assertEqual(after, huge_ids[1 - expected_step] + 1)
            else:
                self.assertIsNone(m, out)
        self.assertEqual(collected, [2, 1, 0])
