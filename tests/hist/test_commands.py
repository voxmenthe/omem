"""Every paged command stays within its limit and continues exactly (invariant 5)."""

import json
import re
import shlex
import sqlite3

from hist import commands
from hist.commands import LIST_BYTES, PAGE_BYTES, READ_BYTES
from tests.hist.fixtures import Claude, Codex, HistCase, git_repo, write_jsonl

SESSIONS = 55
LONG = 300
ROUNDS = 20
CONTINUATION = re.compile(r"(?:more|next): hist (.+)$")


def thread(number: int) -> str:
    return f"019a0000-0000-7000-8000-0000000{number:05x}"


class PagingTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        for n in range(SESSIONS):
            write_jsonl(Codex.path(self.codex, thread(n)), [
                Codex.meta(thread(n), cwd, "cli", self.origin, minute=n % 60),
                Codex.user(f"session {n} asks about the needle in module {n}", n % 60, 1),
                Codex.assistant(f"session {n} reply", n % 60, 2),
            ])
        records = [Codex.meta(thread(999), cwd, "cli", self.origin, minute=59)]
        records += [Codex.user(f"long session line {k} " + "z" * 60, 59, k % 60) for k in range(LONG)]
        # Multi-byte characters straddle every page boundary.
        self.big = "".join(f"{k:05d}é€😀 " for k in range(3000))
        records.append(Codex.assistant(self.big, 59, 59))
        write_jsonl(Codex.path(self.codex, thread(999)), records)
        for n in range(45):
            directory = git_repo(self.base / "other" / f"repo{n}", None)
            native = thread(2000 + n)
            write_jsonl(Codex.path(self.codex, native), [
                Codex.meta(native, str(directory), "cli", minute=n), Codex.user(f"repo {n}", n)])
        self.hist("sync")

    def walk(self, *args: str, limit: int) -> list[list[str]]:
        """Every page's item lines, following continuations to the end."""

        pages = []
        while True:
            out = self.hist(*args).stdout
            lines = out.splitlines()
            more = CONTINUATION.search(lines[-1]) if lines else None
            body = lines[:-1] if more else lines
            self.assertLessEqual(len("\n".join(body).encode()), limit, args)
            pages.append(body)
            if more is None:
                return pages
            args = tuple(shlex.split(more.group(1)))

    def test_find_pages_cover_every_hit_once_newest_first(self) -> None:
        pages = self.walk("find", "needle", limit=LIST_BYTES)
        self.assertGreater(len(pages), 1)
        for n, page in enumerate(pages):
            # The first line counts the page's hits and, but on the last page,
            # repeats the continuation, so `| head` cannot cut it off.
            header = re.fullmatch(r"(\d+) hits?, newest first(; more: hist .+)?", page[0])
            self.assertIsNotNone(header, page[0])
            entries = [line for line in page[1:] if not line.startswith("  in ")]
            self.assertEqual(int(header.group(1)), len(entries))
            self.assertEqual(header.group(2) is not None, n < len(pages) - 1)
        hits = [line for page in pages for line in page[1:] if not line.startswith("  in ")]
        sessions = [int(re.search(r"session (\d+) asks", line).group(1)) for line in hits]
        self.assertEqual(sorted(sessions), list(range(SESSIONS)))
        positions = [int(line.split("|")[0].split(":")[1].split("+")[0]) for line in hits]
        self.assertEqual(positions, sorted(positions, reverse=True))

    def test_sessions_pages_cover_every_session_once(self) -> None:
        pages = self.walk("sessions", limit=LIST_BYTES)
        aliases = [line.split()[0] for page in pages for line in page]
        expected = {thread(n)[-8:] for n in [*range(SESSIONS), 999]}
        self.assertEqual(len(aliases), len(expected))
        self.assertEqual(set(aliases), expected)

    def test_read_pages_replay_the_session_in_order(self) -> None:
        pages = self.walk("read", thread(999)[-8:], limit=READ_BYTES)
        self.assertGreater(len(pages), 2)
        lines = [line for page in pages for line in page]
        users = [int(m.group(1)) for line in lines if (m := re.search(r"long session line (\d+) ", line))]
        self.assertEqual(users, list(range(LONG)))
        self.assertEqual(len(lines), LONG + 2)

    def test_read_kind_pages_keep_the_filter_without_gap_or_repeat(self) -> None:
        pages = self.walk("read", thread(999)[-8:], "--kind", "user", limit=READ_BYTES)
        self.assertGreater(len(pages), 2)
        lines = [line for page in pages for line in page]
        users = [int(m.group(1)) for line in lines if (m := re.search(r"\] user: long session line (\d+) ", line))]
        self.assertEqual(users, list(range(LONG)))
        self.assertEqual(len(lines), LONG)

    def test_read_tail_ends_with_the_newest_and_says_how_to_read_the_rest(self) -> None:
        alias = thread(999)[-8:]
        last = self.hist("read", alias, "--tail", "3").stdout.splitlines()
        self.assertEqual(len(last), 3)
        self.assertIn("] user: long session line 298 ", last[0])
        self.assertIn("] user: long session line 299 ", last[1])
        self.assertIn("] agent: 00000é€😀", last[2])
        lines = self.hist("read", alias, "--tail", "200", "--kind", "user").stdout.splitlines()
        earlier = re.fullmatch(r"earlier: (\d+) of the last 200 did not fit; hist (read .+ --kind user)", lines[0])
        self.assertIsNotNone(earlier, lines[0])
        self.assertLessEqual(len("\n".join(lines[1:]).encode()), READ_BYTES)
        left = int(earlier.group(1))
        users = [int(re.search(r"\] user: long session line (\d+) ", line).group(1)) for line in lines[1:]]
        self.assertEqual(users, list(range(LONG - 200 + left, LONG)))
        pages = self.walk(*shlex.split(earlier.group(2)), limit=READ_BYTES)
        replayed = [int(re.search(r"\] user: long session line (\d+) ", line).group(1))
                    for page in pages for line in page]
        self.assertEqual(replayed, list(range(LONG - 200, LONG)))

    def test_timelines_pages_list_each_timeline_once(self) -> None:
        pages = self.walk("timelines", limit=LIST_BYTES * 2)
        names = [line.split()[0] for page in pages for line in page]
        self.assertEqual(len(names), 46)
        self.assertEqual(len(set(names)), 46)

    def test_zoom_pages_reassemble_the_whole_message(self) -> None:
        position = self.db().execute(
            "SELECT position FROM messages WHERE kind = 'agent' AND text LIKE '00000%'").fetchone()[0]
        pages = self.walk("zoom", f"proj:{position}", limit=PAGE_BYTES + 200)
        self.assertGreater(len(pages), 2)
        text = "".join("\n".join(page[1:]) for page in pages)
        self.assertEqual(text, self.big)


class ReadTest(HistCase):
    """`read --tail` and `--kind` over a worker whose answers sit between tool rows."""

    walk = PagingTest.walk

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        native = thread(3000)
        records = [Codex.meta(native, str(self.repo), "exec", self.origin), Codex.user("audit the guard", 1)]
        for n in range(ROUNDS):
            records += [
                Codex.call("exec_command", json.dumps({"cmd": f"check {n}"}), f"c{n}", 2),
                Codex.output(f"c{n}", f"output {n}", 2),
                Codex.assistant(f"answer {n} " + "y" * 600, 3),
            ]
        write_jsonl(Codex.path(self.codex, native), records)
        self.hist("sync")
        self.alias = native[-8:]

    def answers(self, lines: list[str]) -> list[int]:
        return [int(re.search(r"\] agent: answer (\d+) ", line).group(1)) for line in lines]

    def test_tail_and_kind_select_the_last_answers_skipping_tool_rows(self) -> None:
        lines = self.hist("read", self.alias, "--tail", "3", "--kind", "agent").stdout.splitlines()
        self.assertEqual(self.answers(lines), [ROUNDS - 3, ROUNDS - 2, ROUNDS - 1])
        pages = self.walk("read", self.alias, "--kind", "user,agent", limit=READ_BYTES)
        self.assertGreater(len(pages), 1)
        lines = [line for page in pages for line in page]
        self.assertIn("] user: audit the guard", lines[0])
        self.assertEqual(self.answers(lines[1:]), list(range(ROUNDS)))

    def test_a_tail_from_the_timeline_start_reads_the_rest_from_the_start(self) -> None:
        lines = self.hist("read", self.alias, "--tail", "1000").stdout.splitlines()
        total = 2 + 3 * ROUNDS
        earlier = re.fullmatch(rf"earlier: (\d+) of the last {total} did not fit; hist (read {self.alias})",
                               lines[0])
        self.assertIsNotNone(earlier, lines[0])
        self.assertIn(f"] agent: answer {ROUNDS - 1} ", lines[-1])
        pages = self.walk(*shlex.split(earlier.group(2)), limit=READ_BYTES)
        replayed = [line for page in pages for line in page]
        self.assertEqual(len(replayed), total)
        self.assertEqual(replayed[-(len(lines) - 1):], lines[1:])
        self.assertIn("event: session", replayed[0])

    def test_empty_selections_and_flag_misuse(self) -> None:
        for args in (("--tail", "3", "--kind", "work"), ("--kind", "work")):
            with self.subTest(args=args):
                self.assertEqual(self.hist("read", self.alias, *args).stdout, "no messages\n")
        for args, error in (
            (("--tail", "3", "--after", f"w-{self.alias}:1"), "give --after or --tail, not both"),
            (("--tail", "0"), "--tail takes a positive number"),
            (("--tail", "-2"), "--tail takes a positive number"),
            (("--tail", "two"), "invalid int value"),
            (("--kind", "agent,thought"), "--kind 'thought' is not one of"),
        ):
            with self.subTest(args=args):
                result = self.hist("read", self.alias, *args, check=False)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertIn(error, result.stderr)


class DigestTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        write_jsonl(Codex.path(self.codex, thread(1)), [
            Codex.meta(thread(1), cwd, "cli", self.origin, minute=1),
            Codex.user("continue", 1, 1),
            Codex.user("rename the parser module and update its callers", 1, 2),
            Codex.assistant("first reply", 1, 3),
            Codex.assistant("renamed; callers updated", 1, 4),
        ])
        session = "7c000000-0000-4000-8000-0000000000a1"
        markup = ("<command-message>review</command-message>\n<command-name>/review</command-name>\n"
                  "<command-args>the retry logic in sync.py</command-args>")
        write_jsonl(Claude.project(self.claude, cwd) / f"{session}.jsonl", [
            Claude.user("u1", session, cwd, markup, 2)])
        self.hist("sync")

    def test_digests_decode_commands_skip_weak_prompts_and_end_with_the_last_reply(self) -> None:
        out = self.hist("sessions").stdout
        self.assertIn('"rename the parser module and update its callers" → "renamed; callers updated"', out)
        self.assertIn('"/review the retry logic in sync.py"', out)

    def test_a_page_costs_a_fixed_number_of_statements_per_session(self) -> None:
        connection = sqlite3.connect(self.home / "hist.db")
        self.addCleanup(connection.close)
        timeline = commands.resolve_timeline(connection, "proj", self.repo)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        out = commands.sessions(connection, timeline, False, None)
        self.assertEqual(len(out.splitlines()), 2)
        # The page query, then the first prompts and last reply of each session.
        self.assertEqual(len(statements), 1 + 2 * 2, statements)
