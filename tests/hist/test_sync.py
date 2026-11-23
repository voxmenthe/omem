"""End-to-end: fixture transcript trees in, `hist` commands out."""

import contextlib
import fcntl
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hist import cli as hist_cli
from hist import sync as hist_sync
from hist.config import load_config
from hist.store import DATABASE_NAME, Store, StoreError, StoreMissing
from tests.hist.fixtures import REPO_ROOT, Claude, Codex, HistCase, git_repo, stamp, write_jsonl

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
        self.hist("sync")
        with open(self.home / "sync.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            out = self.hist("view").stdout
            started = time.monotonic()
            caught = self.hist("sync", "--catch-up", check=False)
            # A catch-up tries the lock once instead of waiting its turn.
            self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(out.startswith("(index may be behind: another process is syncing"), out)
        self.assertIn("<hist>", out)
        self.assertEqual(caught.returncode, 0)
        self.assertEqual((caught.stdout, caught.stderr), ("", ""))
        self.assertFalse((self.home / "catch-up.log").exists())

    def test_a_read_waits_out_a_brief_background_sync_without_busy_banner(self) -> None:
        self.hist("sync")
        locked = threading.Event()

        def hold_lock() -> None:
            with open(self.home / "sync.lock", "w") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                locked.set()
                time.sleep(0.25)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        locked.wait()
        try:
            out = self.hist("view").stdout
        finally:
            holder.join()
        self.assertTrue(out.startswith("<hist>\n"), out)
        self.assertNotIn("another process is syncing", out)

    def test_a_read_out_of_time_leaves_the_rest_to_a_background_sync(self) -> None:
        out = self.hist("status", env={"HIST_SYNC_SECONDS": "0"}).stdout
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("(index may be behind: the sync time budget ran out"), first)
        self.assertTrue(first.endswith("; a background `hist sync` is catching up)"), first)
        log = self.home / "catch-up.log"
        deadline = time.monotonic() + 60
        while "complete:" not in (log.read_text() if log.exists() else ""):
            self.assertLess(time.monotonic(), deadline, "the catch-up never finished")
            time.sleep(0.05)
        text = log.read_text()
        self.assertIn("complete: indexed", text)
        self.assertIn("from 3 changed sources", text)
        err_log = self.home / "catch-up.err"
        self.assertEqual(err_log.read_text() if err_log.exists() else "", "")
        self.assertTrue(self.hist("sync").stdout.startswith("indexed 0 messages"))

    def test_a_catch_up_never_recreates_a_removed_store(self) -> None:
        gone = self.base / "removed-home"
        caught = self.hist("sync", "--catch-up", env={"HIST_HOME": str(gone)})
        self.assertEqual((caught.stdout, caught.stderr), ("", ""))
        self.assertFalse(gone.exists())

        # Existing home with config.toml, but hist.db removed right before catch-up.
        no_db = self.base / "no-db-home"
        no_db.mkdir()
        (no_db / "config.toml").write_text((self.home / "config.toml").read_text())
        caught_no_db = self.hist("sync", "--catch-up", env={"HIST_HOME": str(no_db)})
        self.assertEqual((caught_no_db.stdout, caught_no_db.stderr), ("", ""))
        self.assertFalse((no_db / DATABASE_NAME).exists())
        self.assertFalse((no_db / "catch-up.log").exists())


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

    def test_a_subagent_tasked_by_its_parent_agent_is_linked(self) -> None:
        cwd = str(self.repo)
        parent = "019a0000-0000-7000-8000-0000000001aa"
        child = "019a0000-0000-7000-8000-0000000001bb"
        spawn = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
        write_jsonl(Codex.path(self.codex, parent), [
            Codex.meta(parent, cwd, "cli", self.origin), Codex.user("delegate the audit", 1)])
        write_jsonl(Codex.path(self.codex, child), [
            Codex.meta(child, cwd, spawn, self.origin, minute=2),
            Codex.agent_message("/root", "audit the guard", 2), Codex.assistant("audit done", 3)])
        self.hist("sync")
        events = [t for (t,) in self.db().execute("SELECT text FROM messages WHERE kind = 'event'")]
        self.assertIn("worker w-000001bb started: audit the guard", events)

    def test_a_child_linked_long_after_it_started_gets_no_start_note(self) -> None:
        cwd = str(self.repo)
        parent = "019a0000-0000-7000-8000-0000000002aa"
        child = "019a0000-0000-7000-8000-0000000002bb"
        spawn = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
        later = Codex.user("two days later", 5)
        later["timestamp"] = "2026-10-03T10:05:00.000Z"
        write_jsonl(Codex.path(self.codex, parent), [
            Codex.meta(parent, cwd, "cli", self.origin), Codex.user("delegate the audit", 1), later])
        write_jsonl(Codex.path(self.codex, child), [
            Codex.meta(child, cwd, spawn, self.origin, minute=2), Codex.user("audit the guard", 2)])
        self.hist("sync")
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE kind = 'event' AND text LIKE 'worker%'")
                         .fetchone(), (0,))
        self.assertEqual(db.execute(
            "SELECT p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id WHERE c.native_id = ?",
            (child,)).fetchone(), (parent,))
        self.assertEqual(db.execute("SELECT count(*) FROM links WHERE resolved = 0").fetchone(), (0,))

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
        self.assertIn(f"stopped: codex:{thread}", result.stdout)
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


class CatchUpReliabilityTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")

    def test_store_open_no_create_fails_cleanly_and_handles_special_uri_chars(self) -> None:
        missing_dir = self.base / "missing-store"
        with self.assertRaises(StoreMissing):
            Store.open(missing_dir, create=False)
        self.assertFalse(missing_dir.exists())

        weird_home = self.base / "a?b#c%d%20e f"
        weird_home.mkdir()
        with self.assertRaises(StoreMissing) as err:
            Store.open(weird_home, create=False)
        self.assertIsInstance(err.exception, StoreError)
        self.assertFalse((weird_home / DATABASE_NAME).exists())

        created = Store.open(weird_home, create=True)
        created.close()
        reopened = Store.open(weird_home, create=False)
        try:
            self.assertEqual(reopened.meta("schema_version"), "1")
        finally:
            reopened.close()

        # An empty 0-byte database file is rejected instead of creating schema v1.
        empty_home = self.base / "empty-db"
        empty_home.mkdir()
        (empty_home / DATABASE_NAME).write_bytes(b"")
        with self.assertRaises(StoreError) as empty_err:
            Store.open(empty_home, create=False)
        self.assertNotIsInstance(empty_err.exception, StoreMissing)
        conn = sqlite3.connect(empty_home / DATABASE_NAME)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master").fetchone(), (0,))
        finally:
            conn.close()

        # A corrupt (non-missing) database fails `--catch-up` visibly on stderr with exit 2.
        corrupt_home = self.base / "corrupt-db"
        corrupt_home.mkdir()
        (corrupt_home / "config.toml").write_text((self.home / "config.toml").read_text())
        (corrupt_home / DATABASE_NAME).write_bytes(b"not a sqlite database")
        corrupt = self.hist("sync", "--catch-up", env={"HIST_HOME": str(corrupt_home)}, check=False)
        self.assertEqual(corrupt.returncode, 2)
        self.assertIn("hist: cannot open", corrupt.stderr)

    def test_catch_up_log_published_atomically_under_lock_and_losers_exit_zero(self) -> None:
        log = self.home / "catch-up.log"
        sentinel = "2026-10-01T00:00:00Z complete: indexed 9 messages from 1 changed sources in 0.1 s\n"
        log.write_text(sentinel, encoding="utf-8")

        # Parent spawn must not truncate catch-up.log and a lock loser must exit 0 silently.
        with open(self.home / "sync.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            loser = self.hist("sync", "--catch-up", check=False)
            self.assertEqual(loser.returncode, 0)
            self.assertEqual((loser.stdout, loser.stderr), ("", ""))
            self.assertEqual(log.read_text(encoding="utf-8"), sentinel)

            # Spawning via _catch_up while lock is held also leaves sentinel untouched.
            spawned: list[subprocess.Popen[bytes]] = []
            real_popen = subprocess.Popen

            def track_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
                spawned.append(proc)
                return proc

            with (
                mock.patch.dict(os.environ, self.environment(), clear=True),
                mock.patch.object(hist_cli.subprocess, "Popen", side_effect=track_popen),
            ):
                self.assertTrue(hist_cli._catch_up(self.home))
            self.assertEqual(len(spawned), 1)
            self.assertEqual(spawned[0].wait(timeout=30), 0)
            self.assertEqual(log.read_text(encoding="utf-8"), sentinel)

        # Verify the `after` hook executes while `sync.lock` is still held exclusively.
        lock_held_in_callback = False
        store = Store.open(self.home, create=False)
        try:
            config = load_config(self.home)

            def check_lock(_report: hist_sync.Report, _error: Exception | None) -> None:
                nonlocal lock_held_in_callback
                fd = os.open(self.home / "sync.lock", os.O_RDWR)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock_held_in_callback = True
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

            hist_sync.sync(store, config, deadline=time.monotonic() + 10.0, wait=0.0, after=check_lock)
        finally:
            store.close()
        self.assertTrue(lock_held_in_callback)

        # Concurrent catch-up burst: every process exits 0 and catch-up.log holds the newest completion.
        thread = "019a0000-0000-7000-8000-00000000c901"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin),
            Codex.user("concurrent catch-up payload", 1),
        ])
        barrier = threading.Barrier(5)
        results: list[subprocess.CompletedProcess[str]] = []

        def run_one() -> None:
            barrier.wait()
            results.append(self.hist("sync", "--catch-up", check=False))

        threads = [threading.Thread(target=run_one) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r.returncode for r in results], [0] * 5, [r.stderr for r in results])
        final_log = log.read_text(encoding="utf-8")
        self.assertIn("complete: indexed", final_log)
        self.assertEqual(len(final_log.splitlines()), 1)
        self.assertEqual(list(self.home.glob("catch-up.log.tmp.*")), [])
        self.assertIn("concurrent catch-up payload", self.hist("read", thread[-8:]).stdout)

    def test_catch_up_log_distinguishes_partial_and_error_outcomes(self) -> None:
        thread = "019a0000-0000-7000-8000-00000000c902"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin),
            Codex.user("partial budget test", 1),
        ])
        log = self.home / "catch-up.log"

        with (
            mock.patch.dict(os.environ, self.environment(), clear=True),
            mock.patch.object(hist_cli, "CATCH_UP_SECONDS", 0.0),
        ):
            rc = hist_cli.main(["sync", "--catch-up"])
        self.assertEqual(rc, 0)
        self.assertIn("partial (time budget): indexed", log.read_text(encoding="utf-8"))

        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment(), clear=True),
            mock.patch.object(
                hist_sync, "_sync_locked", side_effect=sqlite3.DatabaseError("simulated disk error")
            ),
            contextlib.redirect_stderr(stderr),
        ):
            rc = hist_cli.main(["sync", "--catch-up"])
        self.assertEqual(rc, 1)
        self.assertIn("simulated disk error", stderr.getvalue())
        text = log.read_text(encoding="utf-8")
        self.assertIn("error: DatabaseError: simulated disk error", text)

    def test_pre_lock_config_error_writes_to_catch_up_err_and_trims_when_oversized(self) -> None:
        log = self.home / "catch-up.log"
        sentinel = "2026-10-01T00:00:00Z complete: indexed 0 messages from 0 changed sources in 0.0 s\n"
        log.write_text(sentinel, encoding="utf-8")
        (self.home / "config.toml").write_text("not = [valid toml\n", encoding="utf-8")

        spawned: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def track_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(proc)
            return proc

        with (
            mock.patch.dict(os.environ, self.environment(), clear=True),
            mock.patch.object(hist_cli.subprocess, "Popen", side_effect=track_popen),
        ):
            self.assertTrue(hist_cli._catch_up(self.home))

        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0].wait(timeout=30), 2)
        err_log = self.home / "catch-up.err"
        self.assertIn("invalid TOML", err_log.read_text(encoding="utf-8"))
        self.assertEqual(log.read_text(encoding="utf-8"), sentinel)

        # Verify bounded error-log trimming keeps the tail and stays under the cap.
        line = "hist: old error line\n"
        tail_marker = "hist: newest error line\n"
        oversized = (line * ((hist_cli.CATCH_UP_ERR_MAX_BYTES // len(line)) + 200)) + tail_marker
        err_log.write_text(oversized, encoding="utf-8")
        hist_cli._trim_error_log(err_log)
        trimmed = err_log.read_text(encoding="utf-8")
        self.assertLessEqual(len(trimmed.encode("utf-8")), hist_cli.CATCH_UP_ERR_MAX_BYTES)
        self.assertTrue(trimmed.endswith(tail_marker))

    @unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
    def test_missing_zstd_records_transient_alarm_without_stopping_source(self) -> None:
        # Build a clean PATH containing git (and standard bins) without zstd.
        no_zstd_bin = self.base / "no-zstd-bin"
        no_zstd_bin.mkdir()
        git_bin = shutil.which("git")
        self.assertIsNotNone(git_bin)
        (no_zstd_bin / "git").symlink_to(git_bin)
        no_zstd_env = {"PATH": str(no_zstd_bin)}
        self.assertIsNone(shutil.which("zstd", path=no_zstd_env["PATH"]))

        # Case 1: brand-new .jsonl.zst during _register.
        t_new = "019a0000-0000-7000-8000-00000000c910"
        plain_new = write_jsonl(Codex.path(self.codex, t_new, archived=True), [
            Codex.meta(t_new, str(self.repo), "cli", self.origin),
            Codex.user("compressed before first sync", 1),
        ])
        subprocess.run(["zstd", "-q", "--rm", str(plain_new)], check=True)
        self.assertTrue(plain_new.with_name(plain_new.name + ".zst").exists())

        # Case 2: already-indexed plain source compressed to .jsonl.zst before next sync (_ingest).
        t_existing = "019a0000-0000-7000-8000-00000000c911"
        plain_existing = write_jsonl(Codex.path(self.codex, t_existing), [
            Codex.meta(t_existing, str(self.repo), "cli", self.origin),
            Codex.user("before compression", 2),
        ])
        self.hist("sync", env=no_zstd_env)
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM sources WHERE state = 'stopped'").fetchone(), (0,))
        self.assertEqual(
            db.execute("SELECT kind, subject FROM alarms WHERE kind = 'dependency'").fetchall(),
            [("dependency", "zstd")],
        )
        self.assertIn("before compression", self.hist("read", t_existing[-8:], env=no_zstd_env).stdout)

        # Compress existing source with an appended message, then sync again without zstd.
        archived = Codex.path(self.codex, t_existing, archived=True)
        archived.parent.mkdir(parents=True, exist_ok=True)
        plain_existing.rename(archived)
        write_jsonl(archived, [Codex.user("after compression", 3)])
        subprocess.run(["zstd", "-q", "--rm", str(archived)], check=True)

        self.hist("sync", env=no_zstd_env)
        self.assertEqual(db.execute("SELECT count(*) FROM sources WHERE state = 'stopped'").fetchone(), (0,))
        self.assertEqual(
            db.execute("SELECT state FROM sources WHERE native_id = ?", (t_existing,)).fetchone(),
            ("active",),
        )
        # `hist verify` without zstd treats it as missing, never stops it.
        verified = self.hist("verify", env=no_zstd_env)
        self.assertIn("1 unreadable or moved", verified.stdout)
        self.assertEqual(db.execute("SELECT count(*) FROM sources WHERE state = 'stopped'").fetchone(), (0,))

        # Once zstd returns to PATH, both compressed sources ingest cleanly.
        self.hist("sync")
        self.assertEqual(db.execute("SELECT count(*) FROM sources WHERE state = 'stopped'").fetchone(), (0,))
        self.assertIn("compressed before first sync", self.hist("read", t_new[-8:]).stdout)
        existing_read = self.hist("read", t_existing[-8:]).stdout
        self.assertIn("before compression", existing_read)
        self.assertIn("after compression", existing_read)


class SchedulingAndTelemetryTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo_old = git_repo(self.base / "work" / "repo-old", "https://github.com/me/repo-old.git")
        self.repo_new = git_repo(self.base / "work" / "repo-new", "https://github.com/me/repo-new.git")
        # Mark the store backfilled so synthetic past mtimes are indexed instead of deferred.
        self.hist("sync", "--all")

    def test_bounded_read_sorts_changed_timelines_newest_first_while_full_and_catch_up_stay_oldest_first(
        self,
    ) -> None:
        t_old = "019a0000-0000-7000-8000-00000000d001"
        t_new = "019a0000-0000-7000-8000-00000000d002"
        p_old = write_jsonl(Codex.path(self.codex, t_old), [
            Codex.meta(t_old, str(self.repo_old), "cli", "https://github.com/me/repo-old.git", minute=1),
            Codex.user("old initial", 1),
        ])
        p_new = write_jsonl(Codex.path(self.codex, t_new), [
            Codex.meta(t_new, str(self.repo_new), "cli", "https://github.com/me/repo-new.git", minute=5),
            Codex.user("new initial", 5),
        ])
        self.hist("sync")

        # Append to both already-registered timelines; give repo-new a newer mtime.
        now = time.time()
        write_jsonl(p_old, [Codex.user("old bounded turn", 10)])
        os.utime(p_old, (now - 600, now - 600))
        write_jsonl(p_new, [Codex.user("new bounded turn", 20)])
        os.utime(p_new, (now, now))

        crashed = self.hist(
            "status", env={"HIST_FAULT": "commit:1", "HIST_SYNC_SECONDS": "10"}, check=False
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        texts = {t for (t,) in self.db().execute("SELECT text FROM messages")}
        self.assertIn("new bounded turn", texts)
        self.assertNotIn("old bounded turn", texts)

        # Finish indexing both, then verify full `hist sync` still goes oldest-frontier first.
        self.hist("sync")
        write_jsonl(p_old, [Codex.user("old full turn", 30)])
        os.utime(p_old, (now - 600, now - 600))
        write_jsonl(p_new, [Codex.user("new full turn", 40)])
        os.utime(p_new, (now, now))

        crashed_full = self.hist("sync", env={"HIST_FAULT": "commit:1"}, check=False)
        self.assertEqual(crashed_full.returncode, 91, crashed_full.stderr)
        texts = {t for (t,) in self.db().execute("SELECT text FROM messages")}
        self.assertIn("old full turn", texts)
        self.assertNotIn("new full turn", texts)

        # Finish, then verify `hist sync --catch-up` also stays oldest-frontier first.
        self.hist("sync")
        write_jsonl(p_old, [Codex.user("old catchup turn", 45)])
        os.utime(p_old, (now - 600, now - 600))
        write_jsonl(p_new, [Codex.user("new catchup turn", 50)])
        os.utime(p_new, (now, now))

        crashed_cu = self.hist("sync", "--catch-up", env={"HIST_FAULT": "commit:1"}, check=False)
        self.assertEqual(crashed_cu.returncode, 91, crashed_cu.stderr)
        texts = {t for (t,) in self.db().execute("SELECT text FROM messages")}
        self.assertIn("old catchup turn", texts)
        self.assertNotIn("new catchup turn", texts)

    def test_intra_timeline_merge_keeps_frontier_order_on_bounded_reads(self) -> None:
        t_early = "019a0000-0000-7000-8000-00000000d011"
        t_late = "019a0000-0000-7000-8000-00000000d012"
        p_early = write_jsonl(Codex.path(self.codex, t_early), [
            Codex.meta(t_early, str(self.repo), "cli", self.origin, minute=1),
            Codex.user("earlier turn", 1),
        ])
        p_late = write_jsonl(Codex.path(self.codex, t_late), [
            Codex.meta(t_late, str(self.repo), "cli", self.origin, minute=5),
            Codex.user("later turn", 5),
        ])
        now = time.time()
        os.utime(p_early, (now - 600, now - 600))
        os.utime(p_late, (now, now))
        self.hist("view", env={"HIST_SYNC_SECONDS": "10"})
        users = [
            text
            for (text,) in self.db().execute(
                "SELECT text FROM messages WHERE kind = 'user' ORDER BY position"
            )
        ]
        self.assertEqual(users, ["earlier turn", "later turn"])

    def test_simultaneous_unseen_claude_parent_and_fork_preserve_parent_ownership(self) -> None:
        cwd = str(self.repo)
        parent_id = "5a000000-0000-4000-8000-00000000a001"
        fork_id = "5a000000-0000-4000-8000-00000000a002"
        project = Claude.project(self.claude, cwd)
        shared = [
            Claude.user("u1", parent_id, cwd, "parent original prompt", 1),
            Claude.assistant("a1", parent_id, cwd, [Claude.text("parent original reply")], 2),
        ]
        parent_path = write_jsonl(project / f"{parent_id}.jsonl", shared)
        fork_records = [
            *[{**record, "sessionId": fork_id} for record in shared],
            Claude.user("u2", fork_id, cwd, "fork follow-up", 3),
        ]
        fork_path = write_jsonl(project / f"{fork_id}.jsonl", fork_records)
        now = time.time()
        os.utime(parent_path, (now - 600, now - 600))
        os.utime(fork_path, (now, now))

        # Bounded read discovers BOTH parent and fork simultaneously.
        self.hist("view", env={"HIST_SYNC_SECONDS": "10"})
        owners = self.db().execute(
            "SELECT m.text, s.native_id FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.kind != 'event' ORDER BY m.position"
        ).fetchall()
        self.assertEqual(
            owners,
            [
                ("parent original prompt", parent_id),
                ("parent original reply", parent_id),
                ("fork follow-up", fork_id),
            ],
        )

    def test_resolve_links_survives_partial_ingestion_under_newest_first_order(self) -> None:
        cwd = str(self.repo)
        parent = "019a0000-0000-7000-8000-00000000d021"
        child = "019a0000-0000-7000-8000-00000000d022"
        spawn = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
        p_parent = write_jsonl(Codex.path(self.codex, parent), [
            Codex.meta(parent, cwd, "cli", self.origin, minute=1),
            Codex.user("parent delegates", 1),
            Codex.assistant("parent reply", 2),
        ])
        p_child = write_jsonl(Codex.path(self.codex, child), [
            Codex.meta(child, cwd, spawn, self.origin, minute=2),
            Codex.user("child audit task", 2),
            Codex.assistant("child audit done", 3),
        ])
        now = time.time()
        os.utime(p_parent, (now - 600, now - 600))
        os.utime(p_child, (now, now))

        # Let registration and ONLY the newest timeline (child) ingest before deadline expires,
        # so `_resolve_links` runs while parent has 0 ingested messages.
        store = Store.open(self.home, create=False)
        try:
            config = load_config(self.home)
            ingested = 0
            real_ingest = hist_sync._ingest
            real_monotonic = time.monotonic
            cutoff = real_monotonic() + 10.0

            def fake_monotonic() -> float:
                return cutoff + 1.0 if ingested >= 1 else real_monotonic()

            def counting_ingest(*args: object, **kwargs: object) -> None:
                nonlocal ingested
                real_ingest(*args, **kwargs)  # type: ignore[arg-type]
                ingested += 1

            with (
                mock.patch.object(hist_sync, "_ingest", side_effect=counting_ingest),
                mock.patch.object(hist_sync.time, "monotonic", side_effect=fake_monotonic),
            ):
                partial = hist_sync.sync(store, config, deadline=cutoff, wait=0.0)
            self.assertFalse(partial.complete)
            self.assertEqual(ingested, 1)
            # Child was ingested first and resolved its parent link before parent's messages arrived.
            db = self.db()
            self.assertEqual(
                db.execute(
                    "SELECT p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id "
                    "WHERE c.native_id = ?",
                    (child,),
                ).fetchone(),
                (parent,),
            )
            self.assertEqual(db.execute("SELECT count(*) FROM links WHERE resolved = 0").fetchone(), (0,))

            # Full sync finishes parent cleanly with contiguous positions and no duplicate start note.
            full = hist_sync.sync(store, config, deadline=float("inf"), wait=0.0)
            self.assertTrue(full.complete)
        finally:
            store.close()

        db = self.db()
        self.assertEqual(
            db.execute(
                "SELECT count(*) FROM (SELECT timeline_id FROM messages GROUP BY timeline_id "
                "HAVING min(position) != 0 OR max(position) != count(*) - 1)"
            ).fetchone(),
            (0,),
        )
        worker_events = [
            t
            for (t,) in db.execute(
                "SELECT text FROM messages WHERE kind = 'event' AND text LIKE 'worker w-%'"
            )
        ]
        self.assertEqual(worker_events, ["worker w-0000d022 started: child audit task"])
        self.assertIn("parent delegates", self.hist("read", parent[-8:]).stdout)

        # Reverse order on a fresh parent/child pair: parent (newer mtime) ingests before child's
        # task arrives; the unresolved link stays pending cleanly until the next sync ingests child.
        parent2 = "019a0000-0000-7000-8000-00000000d023"
        child2 = "019a0000-0000-7000-8000-00000000d024"
        spawn2 = {"subagent": {"thread_spawn": {"parent_thread_id": parent2, "depth": 1}}}
        p_parent2 = write_jsonl(Codex.path(self.codex, parent2), [
            Codex.meta(parent2, str(self.repo_new), "cli", "https://github.com/me/repo-new.git", minute=4),
            Codex.user("second parent delegates", 4),
        ])
        p_child2 = write_jsonl(Codex.path(self.codex, child2), [
            Codex.meta(child2, str(self.repo_new), spawn2, "https://github.com/me/repo-new.git", minute=5),
            Codex.user("second child task", 5),
        ])
        os.utime(p_child2, (now - 300, now - 300))
        os.utime(p_parent2, (now + 60, now + 60))

        store2 = Store.open(self.home, create=False)
        try:
            config = load_config(self.home)
            ingested2 = 0
            real_ingest = hist_sync._ingest
            real_monotonic = time.monotonic
            cutoff2 = real_monotonic() + 10.0

            def fake_monotonic2() -> float:
                return cutoff2 + 1.0 if ingested2 >= 1 else real_monotonic()

            def counting_ingest2(*args: object, **kwargs: object) -> None:
                nonlocal ingested2
                real_ingest(*args, **kwargs)  # type: ignore[arg-type]
                ingested2 += 1

            with (
                mock.patch.object(hist_sync, "_ingest", side_effect=counting_ingest2),
                mock.patch.object(hist_sync.time, "monotonic", side_effect=fake_monotonic2),
            ):
                partial2 = hist_sync.sync(store2, config, deadline=cutoff2, wait=0.0)
            self.assertFalse(partial2.complete)
            self.assertEqual(ingested2, 1)
            self.assertEqual(self.db().execute("SELECT count(*) FROM links WHERE resolved = 0").fetchone(), (1,))
            self.assertTrue(hist_sync.sync(store2, config, deadline=float("inf"), wait=0.0).complete)
        finally:
            store2.close()

        self.assertEqual(self.db().execute("SELECT count(*) FROM links WHERE resolved = 0").fetchone(), (0,))
        worker_events = [
            t
            for (t,) in self.db().execute(
                "SELECT text FROM messages WHERE kind = 'event' AND text LIKE 'worker w-%' ORDER BY id"
            )
        ]
        self.assertEqual(
            worker_events,
            [
                "worker w-0000d022 started: child audit task",
                "worker w-0000d024 started: second child task",
            ],
        )

    def test_last_sync_records_seconds_and_discover_seconds(self) -> None:
        thread = "019a0000-0000-7000-8000-00000000d031"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin),
            Codex.user("timing telemetry check", 1),
        ])
        self.hist("sync")
        raw = self.db().execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()[0]
        last = json.loads(raw)
        self.assertIn("seconds", last)
        self.assertIn("discover_seconds", last)
        self.assertIsInstance(last["seconds"], float)
        self.assertIsInstance(last["discover_seconds"], float)
        self.assertGreaterEqual(last["discover_seconds"], 0.0)
        self.assertGreaterEqual(last["seconds"], last["discover_seconds"])
        self.assertEqual(last["seconds"], round(last["seconds"], 3))
        self.assertEqual(last["discover_seconds"], round(last["discover_seconds"], 3))

    def test_hist_rates_splits_stale_budget_and_stale_busy(self) -> None:
        thread = "019a0000-0000-7000-8000-00000000d041"
        write_jsonl(Codex.path(self.codex, thread), [
            Codex.meta(thread, str(self.repo), "cli", self.origin, minute=0),
            Codex.call("exec_command", json.dumps({"cmd": "hist view"}), "c1", 1),
            Codex.output("c1", "<hist>\nfresh output\n</hist>\n", 1),
            Codex.call("exec_command", json.dumps({"cmd": "hist find guard"}), "c2", 2),
            Codex.output(
                "c2",
                "(index may be behind: the sync time budget ran out; last complete sync 2026-10-01T10:00:00Z; "
                "a background `hist sync` is catching up)\nno matches\n",
                2,
            ),
            Codex.call("exec_command", json.dumps({"cmd": "hist status"}), "c3", 3),
            Codex.output(
                "c3",
                "(index may be behind: another process is syncing; last complete sync 2026-10-01T10:00:00Z; "
                "`hist sync` catches up)\nstore ...\n",
                3,
            ),
        ])
        self.hist("sync")

        script = REPO_ROOT / "scripts" / "hist_rates.py"
        result = subprocess.run(
            [sys.executable, str(script), "--home", str(self.home), "--since", "2026-09-01"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        header = lines[0].split()
        row = lines[1].split()
        columns = dict(zip(header, row, strict=True))
        self.assertEqual(columns["calls"], "3")
        self.assertEqual(columns["stale"], "2")
        self.assertEqual(columns["stale_budget"], "1")
        self.assertEqual(columns["stale_busy"], "1")
        self.assertEqual(columns["stale%"], "66.7%")


class OmemUnaffectedTest(HistCase):
    def test_hist_does_not_import_into_omem(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import omem.cli, sys; print('hist' in sys.modules)"],
            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")
