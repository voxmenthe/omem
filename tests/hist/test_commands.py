"""Every paged command stays within its limit and continues exactly (invariant 5)."""

import re
import shlex

from hist.commands import LIST_BYTES, PAGE_BYTES, READ_BYTES
from tests.hist.fixtures import Codex, HistCase, git_repo, write_jsonl

SESSIONS = 55
LONG = 300
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
        hits = [line for page in pages for line in page if not line.startswith("  in ")]
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
