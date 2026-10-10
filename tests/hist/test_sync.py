"""End-to-end: fixture transcript trees in, `hist` commands out."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from tests.hist.fixtures import REPO_ROOT, Claude, Codex, HistCase, stamp, write_jsonl

T1 = "019a0000-0000-7000-8000-00000000c0d1"
T2 = "019a0000-0000-7000-8000-00000000c0d2"
S3 = "3f2a0000-0000-4000-8000-0000000c1a03"
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class ThreeHostsTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        write_jsonl(Codex.path(self.codex, T1), [
            Codex.meta(T1, cwd, "cli", self.origin, minute=0),
            Codex.injected_user(0),
            Codex.developer("INJECTED-HOOK-CODEX", 0),
            Codex.user("fix the guard for memories_v2 paths", 1),
            Codex.reasoning(1),
            Codex.call("exec_command", json.dumps({"cmd": "pytest -q"}), "c1", 2),
            Codex.output("c1", "14 passed", 2),
            Codex.assistant(f"guard landed; token was {SECRET}", 3),
        ])
        write_jsonl(Codex.path(self.codex2, T2), [
            Codex.meta(T2, cwd, "vscode", "git@github.com:me/proj.git", minute=2),
            Codex.legacy_injected_user(2),
            Codex.user("what did codex find about the guard?", 2, 30),
            Codex.assistant("codex2 reply: the guard uses a table", 4),
        ])
        project = Claude.project(self.claude, cwd)
        write_jsonl(project / f"{S3}.jsonl", [
            Claude.metadata("mode", S3, mode="normal"),
            Claude.user("u1", S3, cwd, "claude: review the guard", 3, promptSource="typed"),
            Claude.hook_context("h1", S3, cwd, 3, "INJECTED-HOOK-CLAUDE"),
            Claude.user("m1", S3, cwd, "<system-reminder>INJECTED-REMINDER</system-reminder>", 3),
            Claude.user("m2", S3, cwd, "Base directory for this skill: /x\n\nINJECTED-SKILL", 3, isMeta=True),
            Claude.assistant("a1", S3, cwd, [Claude.tool_use("t1", "Read", {"file_path": "guard.py"})], 3),
            Claude.user("r1", S3, cwd, [Claude.tool_result("t1", "def guard(): ...")], 3),
            Claude.assistant("f1", S3, cwd, [{"type": "fallback", "from": {"model": "claude-opus-5-5"},
                                              "to": {"model": "claude-sonnet-5-5"}}], 4),
            Claude.assistant("a2", S3, cwd, [Claude.text("claude reply: looks right")], 5),
            Claude.metadata("last-prompt", S3, leafUuid="a2"),
        ])

    def test_one_timeline_across_hosts_without_hooks(self) -> None:
        self.hist("sync")
        listed = self.hist("timelines").stdout
        self.assertIn("proj repository", listed)
        view = self.hist("view").stdout
        self.assertTrue(view.startswith("<hist>\nHistory of this repository: every interactive "
                                        "session here (claude, codex, codex2)"), view)
        self.assertTrue(view.endswith("</hist>\n"))
        self.assertLessEqual(len(view.encode()), 8000)
        read = self.hist("read", T1[-8:]).stdout
        self.assertIn("[s:0000c0d1 codex] user: fix the guard for memories_v2 paths", read)
        self.assertIn("tool exec_command:", read)
        self.assertIn("result exec_command: 14 passed", read)
        self.assertIn("[s:0000c0d2 codex2] user: what did codex find", self.hist("read", T2[-8:]).stdout)
        claude = self.hist("read", S3[-8:]).stdout
        self.assertIn(f"[s:{S3[-8:]} claude]", claude)
        self.assertIn("user: claude: review the guard", claude)
        self.assertIn("tool Read:", claude)
        self.assertIn("result Read: def guard(): ...", claude)

    def test_turns_interleave_across_sessions(self) -> None:
        self.hist("sync")
        db = self.db()
        users = [text for (text,) in db.execute(
            "SELECT text FROM messages WHERE kind = 'user' ORDER BY position")]
        self.assertEqual(users, [
            "fix the guard for memories_v2 paths",
            "what did codex find about the guard?",
            "claude: review the guard",
        ])

    def test_injected_context_reasoning_and_secrets_are_never_stored(self) -> None:
        self.hist("sync")
        for marker in (b"INJECTED-AGENTS", b"INJECTED-ENV", b"INJECTED-OLD", b"INJECTED-HOOK-CODEX",
                       b"INJECTED-HOOK-CLAUDE", b"INJECTED-REMINDER", b"INJECTED-SKILL", b"REASONING-TEXT",
                       SECRET.encode()):
            self.assertNotStored(marker)
        self.assertIn("[redacted:github-token]", self.hist("read", T1[-8:]).stdout)
        self.assertNotIn("unknown record types", self.hist("status").stdout)

    def test_zoom_reaches_a_user_message_verbatim(self) -> None:
        self.hist("sync")
        db = self.db()
        position, = db.execute(
            "SELECT position FROM messages WHERE text LIKE 'what did codex%'").fetchone()
        whole = self.hist("zoom", f"proj:{position}").stdout
        self.assertIn(f"proj:{position} | codex2 s:0000c0d2 (interactive)", whole)
        self.assertIn("\nwhat did codex find about the guard?\n", whole)
        top = self.hist("zoom", "proj:0+8", "--depth", "3").stdout.splitlines()
        self.assertEqual([line.split("|")[0] for line in top], [f"proj:{k}+1" for k in range(8)])

    def test_resync_is_idempotent_and_appends_continue(self) -> None:
        self.hist("sync")
        db = self.db()
        before = db.execute("SELECT position, text FROM messages ORDER BY position").fetchall()
        self.hist("sync")
        self.assertEqual(db.execute("SELECT position, text FROM messages ORDER BY position").fetchall(), before)
        write_jsonl(Codex.path(self.codex, T1), [Codex.user("one more thing", 9)])
        self.hist("sync")
        after = db.execute("SELECT position, text FROM messages ORDER BY position").fetchall()
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(after[len(before):], [(len(before), "one more thing")])

    def test_sessions_lists_newest_first_with_first_reference(self) -> None:
        self.hist("sync")
        rows = self.hist("sessions").stdout.splitlines()
        self.assertEqual([row.split()[0] for row in rows], [S3[-8:], T2[-8:], T1[-8:]])
        self.assertIn("from proj:", rows[0])

    def test_reads_proceed_when_another_process_is_syncing(self) -> None:
        import fcntl

        self.hist("sync")
        with open(self.home / "sync.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            out = self.hist("view").stdout
        self.assertTrue(out.startswith("(index may be behind: another process is syncing"), out)
        self.assertIn("<hist>", out)


class ClassificationTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")

    def test_workers_subagents_and_maintenance_stay_out_of_the_repository(self) -> None:
        cwd = str(self.repo)
        parent = "019a0000-0000-7000-8000-0000000000aa"
        child = "019a0000-0000-7000-8000-0000000000bb"
        worker = "019a0000-0000-7000-8000-0000000000cc"
        consolidation = "019a0000-0000-7000-8000-0000000000dd"
        home = self.codex_state / "repos" / "abc"
        write_jsonl(Codex.path(home, parent), [
            Codex.meta(parent, cwd, "cli", self.origin), Codex.user("delegate the audit", 1)])
        write_jsonl(Codex.path(home, child), [
            Codex.meta(child, cwd, {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
                       self.origin, minute=2),
            Codex.user("audit the guard", 2), Codex.assistant("audit done", 3)])
        write_jsonl(Codex.path(self.codex, worker), [
            Codex.meta(worker, cwd, "exec", self.origin, minute=4), Codex.user("headless task", 4)])
        write_jsonl(Codex.path(self.codex, consolidation), [
            Codex.meta(consolidation, str(self.codex / "memories"), {"subagent": "memory_consolidation"}),
            Codex.user("CONSOLIDATION-TEXT", 5)])
        native_dir = str(self.codex_state / "exec" / "x1" / "memories_v2")
        other = "019a0000-0000-7000-8000-0000000000ee"
        write_jsonl(Codex.path(self.codex, other), [
            Codex.meta(other, native_dir, "exec"), Codex.user("NATIVE-DIR-TEXT", 5)])
        sdk = "4b5c0000-0000-4000-8000-00000000a5d1"
        project = Claude.project(self.claude, cwd)
        write_jsonl(project / f"{sdk}.jsonl", [
            Claude.user("s1", sdk, cwd, "sdk prompt", 6, entrypoint="sdk-cli")])
        write_jsonl(project / sdk / "subagents" / "agent-a1234567890abcdef.jsonl", [
            Claude.user("s2", sdk, cwd, "subagent task", 7, entrypoint="sdk-cli", isSidechain=True,
                        agentId="a1234567890abcdef")])
        self.hist("sync")
        db = self.db()
        classes = dict(db.execute("SELECT native_id, class FROM sessions").fetchall())
        self.assertEqual(classes, {parent: "interactive", child: "subagent", worker: "worker",
                                   sdk: "worker", "a1234567890abcdef": "subagent"})
        skipped = db.execute("SELECT native_id, reason FROM sources WHERE state = 'skipped'").fetchall()
        self.assertEqual(sorted(skipped), [(consolidation, "native maintenance session"),
                                           (other, "native maintenance session")])
        self.assertNotStored(b"CONSOLIDATION-TEXT")
        self.assertNotStored(b"NATIVE-DIR-TEXT")
        repo_users = [t for (t,) in db.execute(
            "SELECT m.text FROM messages m JOIN timelines t ON t.id = m.timeline_id "
            "WHERE t.name = 'proj' AND m.kind = 'user'")]
        self.assertEqual(repo_users, ["delegate the audit"])
        events = [t for (t,) in db.execute(
            "SELECT m.text FROM messages m JOIN timelines t ON t.id = m.timeline_id "
            "WHERE t.name = 'proj' AND m.kind = 'event'")]
        self.assertIn("worker w-000000bb started: audit the guard", events)
        self.assertIn("audit done", self.hist("view", "w-000000bb").stdout)
        workers = self.hist("sessions", "--workers").stdout
        for alias in ("000000bb", "000000cc", "00a5d1"[-6:]):
            self.assertIn(alias, workers)
        sub = db.execute(
            "SELECT p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id "
            "WHERE c.native_id = 'a1234567890abcdef'").fetchone()
        self.assertEqual(sub, (sdk,))

    def test_unclassified_sessions_are_stored_apart_and_listed(self) -> None:
        legacy = "019a0000-0000-7000-8000-0000000000f1"
        path = Codex.path(self.codex, legacy)
        write_jsonl(path, [
            {"id": legacy, "timestamp": stamp(0), "instructions": "x", "git": None},
            {"record_type": "state"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "legacy hello"}]},
        ])
        self.hist("sync")
        status = self.hist("status").stdout
        self.assertIn("unclassified sessions (1; open with `hist read <alias>`)", status)
        self.assertIn("legacy hello", self.hist("read", "000000f1").stdout)
        self.assertNotIn("proj ", self.hist("timelines").stdout)

    def test_excluded_paths_are_never_stored(self) -> None:
        self.write_config(extra=f'[discovery]\nexclude = ["{self.repo}"]\n')
        thread = "019a0000-0000-7000-8000-0000000000e1"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli"), Codex.user("EXCLUDED-TEXT", 1)])
        self.hist("sync")
        self.assertNotStored(b"EXCLUDED-TEXT")

    def test_older_sources_wait_for_backfill(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000e2"
        path = write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("old work", 1)])
        os.utime(path, (time.time() - 86400 * 30, time.time() - 86400 * 30))
        out = self.hist("sync").stdout
        self.assertIn("1 older sessions await `hist sync --all`", out)
        self.assertIn("1 older sessions await backfill", self.hist("status").stdout)
        self.hist("sync", "--all")
        self.assertIn("old work", self.hist("read", "000000e2").stdout)

    def test_a_finished_backfill_leaves_nothing_pending(self) -> None:
        def old(path: Path) -> None:
            os.utime(path, (time.time() - 86400 * 30, time.time() - 86400 * 30))

        # A session quit before its first message holds no message record.
        empty = "3d0ac479-d239-42a1-bb17-9da221b8dae1"
        old(write_jsonl(Claude.project(self.claude, str(self.repo)) / f"{empty}.jsonl",
                        [Claude.metadata("summary", empty, summary="Exited")]))
        self.hist("sync", "--all")
        self.assertNotIn("await", self.hist("sync").stdout)
        status = self.hist("status").stdout
        self.assertNotIn("await backfill", status)
        self.assertIn(", backfilled ", status)
        # An older transcript found after the backfill is indexed without another one.
        thread = "019a0000-0000-7000-8000-0000000000e3"
        old(write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("found later", 1)]))
        self.hist("sync")
        self.assertIn("found later", self.hist("read", "000000e3").stdout)


class ForksAndRewritesTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")

    def test_legacy_codex_fork_skips_its_copied_prefix(self) -> None:
        cwd = str(self.repo)
        parent = "019a0000-0000-7000-8000-0000000000a1"
        fork = "019a0000-0000-7000-8000-0000000000a2"
        shared = [Codex.user("parent question", 1), Codex.assistant("parent answer", 2)]
        write_jsonl(Codex.path(self.codex, parent), [Codex.meta(parent, cwd, "cli", self.origin), *shared])
        copied = [{**record, "timestamp": stamp(5)} for record in shared]
        write_jsonl(Codex.path(self.codex, fork), [
            Codex.meta(fork, cwd, {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
                       self.origin, minute=5, forked_from_id=parent, history_mode="legacy"),
            {**Codex.meta(parent, cwd, "cli", self.origin), "timestamp": stamp(5)},
            *copied,
            Codex.event("thread_settings_applied", 5),
            Codex.user("fork's own task", 6),
        ])
        self.hist("sync")
        db = self.db()
        fork_users = [t for (t,) in db.execute(
            "SELECT m.text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.native_id = ? AND m.kind = 'user'", (fork,))]
        self.assertEqual(fork_users, ["fork's own task"])

    def test_paginated_subagent_skips_inherited_records(self) -> None:
        cwd = str(self.repo)
        child = "019a0000-0000-7000-8000-0000000000a3"
        records = [Codex.meta(child, cwd, {"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}},
                              subagent_history_start_ordinal=2)]
        for ordinal, record in enumerate([Codex.user("INHERITED", 1), Codex.assistant("INHERITED2", 1),
                                          Codex.user("own task", 2)]):
            records.append({**record, "ordinal": ordinal})
        write_jsonl(Codex.path(self.codex, child), records)
        self.hist("sync")
        self.assertNotStored(b"INHERITED")
        self.assertIn("own task", self.hist("read", "000000a3").stdout)

    def test_claude_fork_copies_are_stored_once(self) -> None:
        cwd = str(self.repo)
        first, second = "5a000000-0000-4000-8000-0000000000f1", "5a000000-0000-4000-8000-0000000000f2"
        project = Claude.project(self.claude, cwd)
        original = [Claude.user("x1", first, cwd, "original prompt", 1),
                    Claude.assistant("x2", first, cwd, [Claude.text("original reply")], 2)]
        write_jsonl(project / f"{first}.jsonl", original)
        self.hist("sync")
        copies = [{**record, "sessionId": second} for record in original]
        write_jsonl(project / f"{second}.jsonl", [*copies, Claude.user("x3", second, cwd, "forked follow-up", 3)])
        write_jsonl(project / f"{first}.jsonl", original)
        self.hist("sync")
        db = self.db()
        texts = [t for (t,) in db.execute("SELECT text FROM messages WHERE kind != 'event' ORDER BY position")]
        self.assertEqual(texts, ["original prompt", "original reply", "forked follow-up"])

    def test_a_truncated_transcript_stops_its_source_loudly(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000a4"
        path = write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("first", 1),
            Codex.assistant("second", 2)])
        self.hist("sync")
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(lines[:2]))
        write_jsonl(path, [Codex.user("rewritten", 3)])
        self.hist("sync")
        status = self.hist("status").stdout
        self.assertIn("1 stopped", status)
        self.assertIn("transcript changed under its indexed prefix", status)
        self.assertNotStored(b"rewritten")

    def test_an_archive_move_keeps_the_logical_source(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000a5"
        path = write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("before archive", 1)])
        self.hist("sync")
        archived = Codex.path(self.codex, thread, archived=True)
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        write_jsonl(archived, [Codex.user("after archive", 2)])
        self.hist("sync")
        db = self.db()
        texts = [t for (t,) in db.execute("SELECT text FROM messages WHERE kind = 'user' ORDER BY position")]
        self.assertEqual(texts, ["before archive", "after archive"])
        self.assertEqual(db.execute("SELECT path FROM sources").fetchone(), (str(archived),))

    def test_a_move_without_new_records_is_verified_once(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000a6"
        path = write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("archived as is", 1)])
        self.hist("sync")
        archived = Codex.path(self.codex, thread, archived=True)
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        self.assertIn("from 1 changed sources", self.hist("sync").stdout)
        self.assertIn("from 0 changed sources", self.hist("sync").stdout)
        self.assertEqual(self.db().execute("SELECT path FROM sources").fetchone(), (str(archived),))

    def test_verify_catches_a_same_length_rewrite_that_sync_cannot(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000a7"
        path = write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("keep this", 1),
            Codex.user("middle record", 2), Codex.assistant("last record", 3)])
        self.hist("sync")
        self.assertIn("0 of 1 indexed sources fully re-hashed (oldest never)", self.hist("status").stdout)
        self.assertEqual(self.hist("verify").stdout.split(" in ")[0], "verified 1 sources")
        self.assertIn("1 of 1 indexed sources fully re-hashed (oldest 20", self.hist("status").stdout)
        path.write_bytes(path.read_bytes().replace(b"middle record", b"MIDDLE record"))
        self.hist("sync")
        self.assertNotIn("stopped", self.hist("status").stdout)
        result = self.hist("verify", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"stopped: codex:{path.name}", result.stdout)
        self.assertIn("consumed prefix changed", self.hist("status").stdout)

    def test_a_symlinked_transcript_is_reported_not_followed(self) -> None:
        thread = "019a0000-0000-7000-8000-0000000000a8"
        outside = write_jsonl(self.base / "elsewhere" / "real.jsonl", [
            Codex.meta(thread, str(self.repo), "cli", self.origin), Codex.user("LINKED-TEXT", 1)])
        link = Codex.path(self.codex, thread)
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        self.hist("sync")
        self.assertNotStored(b"LINKED-TEXT")
        self.assertIn(f"symlink {link}: ", self.hist("status").stdout)


class ConcurrencyTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        for k in range(6):
            thread = f"019a0000-0000-7000-8000-0000000001{k:02d}"
            write_jsonl(Codex.path(self.codex, thread), [
                Codex.meta(thread, str(self.base / f"dir{k}"), "cli"),
                *[Codex.user(f"message {k}-{n} " + "x" * 400, 1 + n % 50) for n in range(300)],
            ])

    def _count(self) -> int:
        return self.db().execute("SELECT count(*) FROM messages").fetchone()[0]

    def test_an_interrupted_sync_resumes_without_duplicates(self) -> None:
        crashed = self.hist("sync", env={"HIST_FAULT": "commit:2"}, check=False)
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        partial = self._count()
        self.assertGreater(partial, 0)
        self.hist("sync")
        self.assertEqual(self._count(), 6 * 301)
        db = self.db()
        self.assertEqual(db.execute(
            "SELECT count(*) FROM (SELECT source_id, record, part FROM messages "
            "GROUP BY 1, 2, 3 HAVING count(*) > 1)").fetchone(), (0,))

    def test_overlapping_syncs_add_nothing_twice(self) -> None:
        barrier = threading.Barrier(3)
        results: list[subprocess.CompletedProcess[str]] = []

        def run() -> None:
            barrier.wait()
            results.append(self.hist("sync", check=False))

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r.returncode for r in results], [0, 0, 0], [r.stderr for r in results])
        self.assertEqual(self._count(), 6 * 301)
        db = self.db()
        self.assertEqual(db.execute(
            "SELECT count(*) FROM (SELECT timeline_id FROM messages GROUP BY timeline_id "
            "HAVING min(position) != 0 OR max(position) != count(*) - 1)").fetchone(), (0,))


class FindTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        thread = "019a0000-0000-7000-8000-0000000002a1"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin),
            Codex.user("the error.path.guard failed", 1),
            Codex.user("several guards were added", 2),
            Codex.user("foobarbaz is one word", 3),
            Codex.user("Memories_V2 paths", 4),
        ])
        self.hist("sync")

    def test_find_is_literal_case_insensitive_and_word_anchored(self) -> None:
        self.assertIn("error.path.guard failed", self.hist("find", "path.guard").stdout)
        self.assertIn("guards were added", self.hist("find", "guard").stdout)
        self.assertEqual(self.hist("find", "barbaz").stdout, "no matches\n")
        self.assertIn("Memories_V2 paths", self.hist("find", "memories_v2").stdout)
        short = self.hist("find", "ab", check=False)
        self.assertEqual(short.returncode, 2)
        self.assertIn("at least three characters", short.stderr)


class OmemUnaffectedTest(HistCase):
    def test_hist_does_not_import_into_omem(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import omem.cli, sys; print('hist' in sys.modules)"],
            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")
