"""`hist hook`: the view at start, parent links, and never breaking the host."""

import fcntl
import io
import json
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from hist import hook
from tests.hist.fixtures import REPO_ROOT, Claude, Codex, HistCase, Kimi, git_repo, write_jsonl

PRIOR = "019a0000-0000-7000-8000-0000000003a1"
PARENT = "019a0000-0000-7000-8000-0000000003b1"
WORKER = "6c000000-0000-4000-8000-0000000003c1"
NEW = "6c000000-0000-4000-8000-0000000003d1"
KIMI = "session_00000000-0000-4000-8000-0000000003e1"
NESTED = "session_00000000-0000-4000-8000-0000000003e2"
CLAUDE_PARENT = "5e000000-0000-4000-8000-0000000003f1"


class HookTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        write_jsonl(Codex.path(self.codex, PRIOR), [
            Codex.meta(PRIOR, str(self.repo), "cli", self.origin),
            Codex.user("we chose the table-driven guard", 1),
            Codex.assistant("prior reply", 2),
        ])

    def claude_start(self, session: str, source: str = "startup", env: dict[str, str | None] | None = None,
                     transcript: str | None = None) -> tuple[str, str]:
        path = transcript or str(Claude.project(self.claude, str(self.repo)) / f"{session}.jsonl")
        payload = {"session_id": session, "transcript_path": path, "cwd": str(self.repo),
                   "hook_event_name": "SessionStart", "source": source}
        result = self.hist("hook", "claude", "SessionStart", stdin=json.dumps(payload), env=env)
        return result.stdout, result.stderr

    def test_a_new_session_in_an_injected_repository_starts_with_the_view(self) -> None:
        self.hist("sync")
        out, _ = self.claude_start(NEW)
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "SessionStart")
        context = payload["additionalContext"]
        self.assertTrue(context.startswith("<hist>\n"), context)
        self.assertIn("we chose the table-driven guard", context)
        self.assertLessEqual(len(context.encode()), 8000)

    def test_hooks_leave_pending_transcripts_for_normal_commands(self) -> None:
        self.hist("sync")
        db = self.db()
        indexed = db.execute("SELECT path, token, cursor, state FROM sources").fetchall()
        messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        pending = "this message arrived after the index was refreshed"
        write_jsonl(Codex.path(self.codex, PRIOR), [Codex.user(pending, 3)])
        for host in ("codex", "claude"):
            for event, source in (("SessionStart", "startup"), ("SessionStart", "compact"),
                                  ("SessionStart", "clear"), ("SubagentStart", "startup")):
                with self.subTest(host=host, event=event, source=source):
                    payload = {"session_id": NEW, "agent_id": WORKER, "cwd": str(self.repo),
                               "hook_event_name": event, "source": source}
                    result = self.hist("hook", host, event, stdin=json.dumps(payload))
                    self.assertEqual(result.stderr, "")
                    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                    self.assertNotIn(pending, context)
                    if event == "SessionStart":
                        self.assertIn("we chose the table-driven guard", context)
                    self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], messages)
                    self.assertEqual(db.execute("SELECT path, token, cursor, state FROM sources").fetchall(),
                                     indexed)
        self.assertIn(pending, self.hist("view").stdout)
        self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], messages + 1)

    def test_hook_defers_changed_archive_verification_to_sync(self) -> None:
        self.hist("sync")
        db = self.db()
        indexed = db.execute("SELECT path, token, cursor, state FROM sources").fetchall()
        path = Codex.path(self.codex, PRIOR)
        archived = Codex.path(self.codex, PRIOR, archived=True)
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        archived.write_bytes(archived.read_bytes().replace(b"we chose", b"WE chose"))
        out, err = self.claude_start(NEW)
        self.assertEqual(err, "")
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("we chose the table-driven guard", context)
        self.assertEqual(db.execute("SELECT path, token, cursor, state FROM sources").fetchall(), indexed)
        self.hist("sync")
        state, reason = db.execute("SELECT state, reason FROM sources").fetchone()
        self.assertEqual(state, "stopped")
        self.assertIn("transcript changed under its indexed prefix", reason)
        self.assertEqual(db.execute("SELECT text FROM messages WHERE kind = 'user'").fetchall(),
                         [("we chose the table-driven guard",)])

    def test_hook_without_cached_history_leaves_sources_unindexed(self) -> None:
        out, err = self.claude_start(NEW)
        self.assertEqual((out, err), ("", ""))
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone(), (0,))
        self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone(), (0,))
        self.hist("sync")
        self.assertIn("we chose the table-driven guard", self.claude_start(NEW)[0])

    def test_subagent_context_respects_worker_view_before_indexing(self) -> None:
        self.hist("sync")
        for host in ("codex", "claude"):
            for mode in ("header", "view", "none"):
                with self.subTest(host=host, mode=mode):
                    self.write_config(extra=f'worker = "{mode}"')
                    payload = {"session_id": PARENT, "agent_id": WORKER, "cwd": str(self.repo),
                               "hook_event_name": "SubagentStart"}
                    result = self.hist("hook", host, "SubagentStart", stdin=json.dumps(payload))
                    self.assertEqual(result.stderr, "")
                    if mode == "none":
                        self.assertEqual(result.stdout, "")
                    else:
                        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                        self.assertTrue(context.startswith("<hist>\n"))
                        self.assertEqual("we chose the table-driven guard" in context, mode == "view")
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM links").fetchone(), (0,))
        self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE native_id = ?", (WORKER,)).fetchone(),
                         (0,))

    def test_unindexed_codex_callers_keep_their_session_class(self) -> None:
        self.hist("sync")
        for number, source in enumerate(("exec", "mcp", "cli", "vscode", "missing", "empty")):
            with self.subTest(source=source):
                session = f"6c000000-0000-4000-8000-{number:012x}"
                path = write_jsonl(Codex.path(self.codex, session), [
                    Codex.meta(session, str(self.repo), source, self.origin),
                ])
                if source == "missing":
                    path.unlink()
                elif source == "empty":
                    path.write_bytes(b"")
                payload = {"session_id": session, "transcript_path": str(path), "cwd": str(self.repo),
                           "hook_event_name": "SessionStart", "source": "startup"}
                result = self.hist("hook", "codex", "SessionStart", stdin=json.dumps(payload))
                self.assertEqual(result.stderr, "")
                context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertTrue(context.startswith("<hist>\n"))
                self.assertEqual("we chose the table-driven guard" in context, source not in ("exec", "mcp"))
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone(), (1,))

    def test_resume_and_uninjected_repositories_print_nothing(self) -> None:
        self.assertEqual(self.claude_start(NEW, source="resume")[0], "")
        self.write_config(inject="[]")
        self.assertEqual(self.claude_start(NEW)[0], "")
        self.hist("sync")
        self.assertIn("we chose the table-driven guard", self.hist("read", PRIOR[-8:]).stdout)

    def test_a_shell_launched_worker_links_to_its_parent_before_indexing(self) -> None:
        self.write_config(inject="[]")
        out, _ = self.claude_start(WORKER, env={"CODEX_THREAD_ID": PARENT, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        self.assertEqual(out, "")
        db = self.db()
        self.assertEqual(db.execute("SELECT child_native, parent_native, via, resolved FROM links").fetchall(),
                         [(WORKER, PARENT, "CODEX_THREAD_ID", 0)])
        write_jsonl(Codex.path(self.codex, PARENT), [
            Codex.meta(PARENT, str(self.repo), "cli", self.origin, minute=3),
            Codex.user("run a headless review", 3)])
        write_jsonl(Claude.project(self.claude, str(self.repo)) / f"{WORKER}.jsonl", [
            Claude.user("w1", WORKER, str(self.repo), "review the guard headlessly", 4, entrypoint="sdk-cli")])
        self.hist("sync")
        row = db.execute(
            "SELECT c.class, p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id "
            "WHERE c.native_id = ?", (WORKER,)).fetchone()
        self.assertEqual(row, ("worker", PARENT))
        self.assertEqual(db.execute("SELECT resolved FROM links").fetchone(), (1,))

    def test_a_worker_gets_the_header_naming_its_parent(self) -> None:
        write_jsonl(Codex.path(self.codex, PARENT), [
            Codex.meta(PARENT, str(self.repo), "cli", self.origin, minute=3),
            Codex.user("run a headless review", 3)])
        self.hist("sync")
        out, _ = self.claude_start(WORKER, env={"CODEX_THREAD_ID": PARENT, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("run a headless review", context)
        self.assertIn(f"You were started by session s:{PARENT[-8:]} (codex); `hist view proj`", context)

    def test_a_hook_inside_its_own_host_ignores_its_own_session_variable(self) -> None:
        self.claude_start(NEW, env={"CLAUDE_CODE_SESSION_ID": "some-other-session"})
        self.assertEqual(self.db().execute("SELECT count(*) FROM links").fetchone(), (0,))

    def test_hist_parent_links_its_worker_but_not_that_workers_own_workers(self) -> None:
        # A launcher in PARENT's shell starts Claude worker NEW with HIST_PARENT;
        # NEW's shell then starts Codex worker WORKER, which inherits it stale.
        self.write_config(inject="[]")
        launched = {"HIST_PARENT": f"claude:{PARENT}"}
        self.claude_start(NEW, env={**launched, "CLAUDE_CODE_SESSION_ID": NEW})
        payload = {"session_id": WORKER, "transcript_path": str(Codex.path(self.codex, WORKER)),
                   "cwd": str(self.repo), "hook_event_name": "SessionStart", "source": "startup"}
        self.hist("hook", "codex", "SessionStart", stdin=json.dumps(payload),
                  env={**launched, "CLAUDE_CODE_SESSION_ID": NEW})
        links = self.db().execute("SELECT child_native, parent_native, via FROM links").fetchall()
        self.assertEqual(sorted(links), sorted([(NEW, PARENT, "HIST_PARENT"),
                                                (WORKER, NEW, "CLAUDE_CODE_SESSION_ID")]))

    def test_a_transcript_outside_every_source_raises_an_alarm(self) -> None:
        stray = self.base / ".claude-other" / "projects" / "x" / f"{NEW}.jsonl"
        self.claude_start(NEW, transcript=str(stray))
        self.assertIn(str(stray), self.hist("status").stdout)

    def test_bad_input_never_breaks_the_host(self) -> None:
        for stdin in ("not json", "[]", json.dumps({"cwd": str(self.repo)})):
            result = self.hist("hook", "claude", "SessionStart", stdin=stdin)
            self.assertEqual(result.stdout, "")
            self.assertIn("the agent continues without history", result.stderr)
        for host, event in (("grok", "SessionStart"), ("kimi", "SessionStart"), ("claude", "UserPromptSubmit")):
            result = self.hist("hook", host, event, stdin="{}")
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertIn(f"unsupported {host} {event}", result.stderr)

    def _run_main(self, host: str, event: str, stdin_text: str,
                  env: dict[str, str | None] | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        clean_env = self.environment(env)
        with (
            mock.patch.dict(os.environ, clean_env, clear=True),
            mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ):
            code = hook.main(host, event, self.home)
        return code, stdout.getvalue(), stderr.getvalue()

    def _wait_for_catch_up_idle(self, timeout: float = 30.0) -> None:
        lock_path = self.home / "sync.lock"
        if not lock_path.exists():
            return
        deadline = time.monotonic() + timeout
        with open(lock_path, "a+") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    return
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline, "background catch-up still holds sync.lock")
                    time.sleep(0.05)

    def _wait_for_message(self, text: str, timeout: float = 30.0) -> None:
        db = self.db()
        deadline = time.monotonic() + timeout
        while not db.execute("SELECT 1 FROM messages WHERE text = ? LIMIT 1", (text,)).fetchone():
            self.assertLess(time.monotonic(), deadline, f"background catch-up never indexed {text!r}")
            time.sleep(0.05)
        self._wait_for_catch_up_idle(timeout)

    def test_normal_hooks_skip_write_transactions_while_another_writer_holds_lock(self) -> None:
        self.hist("sync")
        holder = sqlite3.connect(self.home / "hist.db", isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            out, err = self.claude_start(NEW)
            self.assertEqual(err, "")
            context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("we chose the table-driven guard", context)

            codex_payload = {
                "session_id": NEW, "transcript_path": str(Codex.path(self.codex, NEW)),
                "cwd": str(self.repo), "hook_event_name": "SessionStart", "source": "startup",
            }
            codex_res = self.hist("hook", "codex", "SessionStart", stdin=json.dumps(codex_payload))
            self.assertEqual(codex_res.stderr, "")
            self.assertIn("we chose the table-driven guard",
                          json.loads(codex_res.stdout)["hookSpecificOutput"]["additionalContext"])

            sub_payload = {"session_id": PARENT, "agent_id": WORKER, "cwd": str(self.repo),
                           "hook_event_name": "SubagentStart"}
            sub_res = self.hist("hook", "claude", "SubagentStart", stdin=json.dumps(sub_payload))
            self.assertEqual(sub_res.stderr, "")
            self.assertTrue(json.loads(sub_res.stdout)["hookSpecificOutput"]["additionalContext"].startswith("<hist>\n"))

            # Contrast: hooks that actually need a link or alarm still attempt store.write()
            # and observe the competing BEGIN IMMEDIATE lock as StoreBusy.
            linked_out, linked_err = self.claude_start(WORKER, env={"CODEX_THREAD_ID": PARENT})
            self.assertEqual(linked_out, "")
            self.assertIn("StoreBusy", linked_err)

            stray = self.base / ".claude-other" / "projects" / "x" / f"{NEW}.jsonl"
            stray_out, stray_err = self.claude_start(NEW, transcript=str(stray))
            self.assertEqual(stray_out, "")
            self.assertIn("StoreBusy", stray_err)
        finally:
            holder.execute("ROLLBACK")

        out, err = self.claude_start(WORKER, env={"CODEX_THREAD_ID": PARENT})
        self.assertEqual(err, "")
        self.assertTrue(json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith("<hist>\n"))

    def test_hook_catch_up_debounces_with_stamp_and_syncs_in_background(self) -> None:
        self.hist("sync")
        pending1 = "pending message indexed by first hook catch-up"
        write_jsonl(Codex.path(self.codex, PRIOR), [Codex.user(pending1, 3)])

        out, err = self.claude_start(NEW, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual(err, "")
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("we chose the table-driven guard", context)
        self.assertNotIn(pending1, context)

        stamp = self.home / "catch-up.stamp"
        self.assertTrue(stamp.is_file())
        self._wait_for_message(pending1)
        err_log = self.home / "catch-up.err"
        if err_log.exists():
            self.assertEqual(err_log.read_text(encoding="utf-8"), "")

        # Immediate second call within cooldown skips updating the stamp and does not spawn.
        first_mtime_ns = stamp.stat().st_mtime_ns
        pending2 = "pending message waiting for cooldown expiry"
        write_jsonl(Codex.path(self.codex, PRIOR), [Codex.user(pending2, 4)])
        out2, err2 = self.claude_start(WORKER, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual(err2, "")
        self.assertIn(pending1, json.loads(out2)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(stamp.stat().st_mtime_ns, first_mtime_ns)

        # Once the stamp expires, the next hook updates the stamp and respawns catch-up.
        expired = time.time() - hook.HOOK_CATCH_UP_COOLDOWN - 5.0
        os.utime(stamp, (expired, expired))
        out3, err3 = self.claude_start(NEW, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual(err3, "")
        self.assertGreater(stamp.stat().st_mtime, expired)
        self._wait_for_message(pending2)

    def test_hook_catch_up_dispatch_contract_and_guards(self) -> None:
        self.hist("sync")
        stamp = self.home / "catch-up.stamp"
        err_path = self.home / "catch-up.err"
        captured: list[dict[str, object]] = []

        real_popen = subprocess.Popen

        def fake_popen(cmd: object, *args: object, **kwargs: object) -> object:
            if not (isinstance(cmd, list) and cmd[1:4] == ["-m", "hist", "sync"]):
                return real_popen(cmd, *args, **kwargs)  # type: ignore[arg-type]
            stderr_handle = kwargs["stderr"]
            captured.append({
                "cmd": cmd,
                "cwd": kwargs["cwd"],
                "env": kwargs["env"],
                "stdin": kwargs["stdin"],
                "stdout": kwargs["stdout"],
                "stderr_handle": stderr_handle,
                "stderr_name": getattr(stderr_handle, "name", None),
                "stderr_mode": getattr(stderr_handle, "mode", None),
                "start_new_session": kwargs["start_new_session"],
                "stamp_exists": stamp.is_file(),
                "stamp_mtime": stamp.stat().st_mtime if stamp.exists() else None,
                "stdout_so_far": sys.stdout.getvalue(),  # type: ignore[attr-defined]
            })
            return mock.MagicMock()

        def failing_popen(cmd: object, *args: object, **kwargs: object) -> object:
            if isinstance(cmd, list) and cmd[1:4] == ["-m", "hist", "sync"]:
                raise OSError("spawn failed")
            return real_popen(cmd, *args, **kwargs)  # type: ignore[arg-type]

        claude_payload = json.dumps({
            "session_id": NEW,
            "transcript_path": str(Claude.project(self.claude, str(self.repo)) / f"{NEW}.jsonl"),
            "cwd": str(self.repo), "hook_event_name": "SessionStart", "source": "startup",
        })

        with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
            # First valid SessionStart flushes stdout and touches stamp BEFORE Popen.
            code, out, err = self._run_main("claude", "SessionStart", claude_payload,
                                            env={"HIST_NO_CATCH_UP": None})
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(captured), 1)
            call = captured[0]
            self.assertEqual(call["cmd"], [sys.executable, "-m", "hist", "sync", "--catch-up"])
            self.assertEqual(call["cwd"], self.home)
            self.assertEqual(call["env"]["HIST_HOME"], str(self.home))  # type: ignore[index]
            self.assertIs(call["stdin"], subprocess.DEVNULL)
            self.assertIs(call["stdout"], subprocess.DEVNULL)
            self.assertEqual(Path(str(call["stderr_name"])), err_path)
            self.assertEqual(call["stderr_mode"], "a")
            self.assertEqual(stat.S_IMODE(err_path.stat().st_mode), 0o600)
            self.assertTrue(call["stderr_handle"].closed)  # type: ignore[attr-defined]
            self.assertTrue(call["start_new_session"])
            self.assertTrue(call["stamp_exists"])
            self.assertEqual(stat.S_IMODE(stamp.stat().st_mode), 0o600)
            self.assertEqual(call["stdout_so_far"], out)
            self.assertIn("we chose the table-driven guard", out)

            # Immediate second call skips Popen.
            code, _, err = self._run_main("claude", "SessionStart", claude_payload,
                                          env={"HIST_NO_CATCH_UP": None})
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(captured), 1)

            # Future-skewed stamp (backward clock jump) self-heals instead of wedging.
            future = time.time() + 3600.0
            os.utime(stamp, (future, future))
            code, _, err = self._run_main("claude", "SessionStart", claude_payload,
                                          env={"HIST_NO_CATCH_UP": None})
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(captured), 2)
            self.assertLess(stamp.stat().st_mtime, future)

            # HIST_NO_CATCH_UP=1 suppresses spawn even with no stamp.
            stamp.unlink()
            code, _, err = self._run_main("claude", "SessionStart", claude_payload,
                                          env={"HIST_NO_CATCH_UP": "1"})
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(captured), 2)
            self.assertFalse(stamp.exists())

            # SubagentStart NEVER triggers catch-up for Claude or Codex.
            for host in ("claude", "codex"):
                sub = json.dumps({"session_id": PARENT, "agent_id": WORKER,
                                  "cwd": str(self.repo), "hook_event_name": "SubagentStart"})
                code, _, err = self._run_main(host, "SubagentStart", sub, env={"HIST_NO_CATCH_UP": None})
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(len(captured), 2, host)
                self.assertFalse(stamp.exists(), host)

            # Invalid/malformed/unsupported inputs NEVER trigger catch-up.
            for host, event, raw in (
                ("claude", "SessionStart", "not json"),
                ("claude", "SessionStart", "[]"),
                ("claude", "SessionStart", json.dumps({"cwd": str(self.repo)})),
                ("grok", "SessionStart", "{}"),
                ("kimi", "SessionStart", "{}"),
                ("claude", "UserPromptSubmit", "{}"),
            ):
                code, out, _ = self._run_main(host, event, raw, env={"HIST_NO_CATCH_UP": None})
                self.assertEqual((code, out), (0, ""), (host, event, raw))
                self.assertEqual(len(captured), 2, (host, event, raw))
                self.assertFalse(stamp.exists(), (host, event, raw))

            # Early-return paths STILL trigger catch-up: resume, fork, uninjected repo, worker=none.
            early_cases = [
                ("resume", "claude", {"session_id": NEW, "cwd": str(self.repo), "source": "resume"}, '["proj"]', ""),
                ("fork", "claude", {"session_id": NEW, "cwd": str(self.repo), "source": "fork"}, '["proj"]', ""),
                ("uninjected", "claude", {"session_id": NEW, "cwd": str(self.repo), "source": "startup"}, "[]", ""),
                ("worker-none", "claude", {"session_id": WORKER, "cwd": str(self.repo), "source": "startup"},
                 '["proj"]', 'worker = "none"'),
                ("codex-resume", "codex", {"session_id": NEW, "cwd": str(self.repo), "source": "resume"},
                 '["proj"]', ""),
            ]
            for label, host, payload_obj, inject, extra in early_cases:
                with self.subTest(case=label):
                    if stamp.exists():
                        stamp.unlink()
                    self.write_config(extra=extra, inject=inject)
                    before = len(captured)
                    env_override: dict[str, str | None] = {"HIST_NO_CATCH_UP": None}
                    if label == "worker-none":
                        env_override["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
                    code, out, err = self._run_main(host, "SessionStart", json.dumps(payload_obj), env=env_override)
                    self.assertEqual((code, out, err), (0, "", ""))
                    self.assertEqual(len(captured), before + 1)
                    self.assertTrue(stamp.is_file())

        # Fail open: Popen error or uncreatable stamp never emits non-protocol stdout or exits nonzero.
        self.write_config(inject='["proj"]')
        if stamp.exists():
            stamp.unlink()
        with mock.patch.object(subprocess, "Popen", side_effect=failing_popen):
            code, out, err = self._run_main("claude", "SessionStart", claude_payload,
                                            env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("we chose the table-driven guard",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])

        if stamp.exists():
            stamp.unlink()
        stamp.mkdir()
        code, out, err = self._run_main("claude", "SessionStart", claude_payload,
                                        env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("we chose the table-driven guard",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])

        # Missing DB skips spawn without creating stamp.
        stamp.rmdir()
        missing_home = self.base / "no-db-home"
        missing_home.mkdir()
        with (
            mock.patch.dict(os.environ, self.environment({"HIST_NO_CATCH_UP": None}), clear=True),
            mock.patch.object(subprocess, "Popen") as popen,
        ):
            self.assertFalse(hook._maybe_catch_up(missing_home))
            popen.assert_not_called()
            self.assertFalse((missing_home / "catch-up.stamp").exists())

    def test_launchd_example_plist_is_valid_and_portable(self) -> None:
        raw = (REPO_ROOT / "hist" / "integrations" / "launchd.plist.example").read_bytes()
        plist = plistlib.loads(raw)
        self.assertEqual(plist["Label"], "com.omem.hist-sync")
        self.assertEqual(plist["ProgramArguments"], ["/Users/YOU/.local/bin/hist", "sync", "--catch-up"])
        path_entries = plist["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn("/opt/homebrew/bin", path_entries)
        self.assertIn("/usr/local/bin", path_entries)
        self.assertEqual(plist["StartInterval"], 300)
        self.assertEqual(plist["Nice"], 10)
        self.assertIs(plist["LowPriorityIO"], True)
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertEqual(plist["StandardErrorPath"], "/Users/YOU/.hist/launchd.err")


class KimiPromptTest(HistCase):
    """Kimi's UserPromptSubmit hook: the view once, on a session's first prompt."""

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        write_jsonl(Codex.path(self.codex, PRIOR), [
            Codex.meta(PRIOR, str(self.repo), "cli", self.origin),
            Codex.user("we chose the table-driven guard", 1),
        ])
        self.hist("sync")

    def shells(self, *names: str) -> tuple[str, ...]:
        """A command prefix that runs hist under shells with these process
        names, outermost first, as Kimi runs a hook under its own process."""

        prefix: list[str] = []
        for name in names:
            link = os.path.join(tempfile.mkdtemp(dir=self.base), name)
            os.symlink("/bin/sh", link)
            # `; exit $?` keeps the shell from replacing itself with the command.
            prefix += [link, "-c", '"$@"; exit $?', name]
        return tuple(prefix)

    def payload(self, session: str, steer: bool = False, cwd: Path | None = None) -> str:
        return json.dumps({"session_id": session, "cwd": str(cwd or self.repo), "prompt": "fix the guard",
                           "is_steer": steer, "session_title": ""})

    def prompt(self, session: str = KIMI, steer: bool = False, shells: tuple[str, ...] = (),
               env: dict[str, str | None] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.hist("hook", "kimi", "UserPromptSubmit", stdin=self.payload(session, steer, cwd), env=env,
                         prefix=self.shells(*shells))

    def marker(self, session: str) -> bool:
        return (self.home / "hook-state" / "kimi" / session).is_file()

    def claude_above(self) -> None:
        """The test runner, the hook's nearest ancestor above its shells, as a Claude Code session."""

        sessions = self.base / ".claude" / "sessions"
        sessions.mkdir()
        (sessions / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": CLAUDE_PARENT}))

    def links(self) -> list[tuple[str, str, str, str]]:
        return self.db().execute("SELECT child_native, parent_adapter, parent_native, via FROM links").fetchall()

    def test_the_first_prompt_prints_the_view_as_plain_text_and_later_ones_nothing(self) -> None:
        first = self.prompt(KIMI)
        self.assertEqual(first.stderr, "")
        self.assertTrue(first.stdout.startswith("<hist>\n"), first.stdout)
        self.assertIn("we chose the table-driven guard", first.stdout)
        with self.assertRaises(ValueError):
            json.loads(first.stdout)
        self.assertTrue(self.marker(KIMI))
        # Neither a later prompt nor a steer reads the configuration or the store.
        (self.home / "config.toml").write_text("not [toml")
        for session, steer in ((KIMI, False), (KIMI, True), (NESTED, True)):
            result = self.prompt(session, steer)
            self.assertEqual((result.stdout, result.stderr), ("", ""), (session, steer))
        self.assertFalse(self.marker(NESTED))

    def test_a_session_already_indexed_gets_its_marker_and_no_view(self) -> None:
        Kimi.write_state(self.kimi, str(self.repo), KIMI, title="Fix the guard")
        write_jsonl(Kimi.journal(self.kimi, str(self.repo), KIMI), [Kimi.metadata(), Kimi.prompt("fix the guard", 1)])
        self.hist("sync")
        self.assertEqual(self.prompt(KIMI).stdout, "")
        self.assertTrue(self.marker(KIMI))

    def test_a_session_indexed_before_its_first_prompt_still_gets_the_view(self) -> None:
        # Kimi writes the prompt after the hook runs; a titled session can be indexed first,
        # holding only hist's own "session started" note.
        Kimi.write_state(self.kimi, str(self.repo), KIMI, title="New Session")
        write_jsonl(Kimi.journal(self.kimi, str(self.repo), KIMI), [Kimi.metadata()])
        self.hist("sync")
        self.assertEqual(self.db().execute("SELECT class, message_count FROM sessions WHERE native_id = ?",
                                           (KIMI,)).fetchone(), ("interactive", 1))
        self.assertIn("we chose the table-driven guard", self.prompt(KIMI).stdout)

    def test_kimi_started_from_claude_is_linked_and_gets_the_header(self) -> None:
        self.claude_above()
        write_jsonl(Claude.project(self.claude, str(self.repo)) / f"{CLAUDE_PARENT}.jsonl", [
            Claude.user("p1", CLAUDE_PARENT, str(self.repo), "delegate the audit", 2, promptSource="typed")])
        self.hist("sync")
        alias = self.db().execute("SELECT alias FROM sessions WHERE native_id = ?", (CLAUDE_PARENT,)).fetchone()[0]
        # Inherited variables name other sessions; the process walk decides.
        inherited = {"CODEX_THREAD_ID": PRIOR, "CLAUDE_CODE_SESSION_ID": "5e000000-0000-4000-8000-0000000003ff"}
        out = self.prompt(KIMI, shells=("kimi-code",), env=inherited).stdout
        self.assertIn(f"You were started by session s:{alias} (claude)", out)
        self.assertNotIn("we chose the table-driven guard", out)
        # Claude, then Kimi, then this Kimi: the Kimi above names nobody, so no link and the view.
        out = self.prompt(NESTED, shells=("kimi", "kimi-code"), env=inherited).stdout
        self.assertIn("we chose the table-driven guard", out)
        self.prompt(KIMI, shells=("kimi-code",))
        self.assertEqual(self.links(), [(KIMI, "claude", CLAUDE_PARENT, "walk:claude-sessions")])
        Kimi.write_state(self.kimi, str(self.repo), KIMI)
        write_jsonl(Kimi.journal(self.kimi, str(self.repo), KIMI), [
            Kimi.metadata(), Kimi.prompt("audit the guard", 3), Kimi.ended(4)])
        self.hist("sync")
        self.assertEqual(self.db().execute(
            "SELECT c.class, p.native_id FROM sessions c JOIN sessions p ON p.id = c.parent_id "
            "WHERE c.native_id = ?", (KIMI,)).fetchone(), ("worker", CLAUDE_PARENT))

    def test_outside_inject_the_link_is_recorded_and_nothing_printed(self) -> None:
        self.write_config(inject="[]")
        self.claude_above()
        self.assertEqual(self.prompt(KIMI, shells=("kimi",)).stdout, "")
        self.assertEqual(self.links(), [(KIMI, "claude", CLAUDE_PARENT, "walk:claude-sessions")])

    def test_bad_input_a_busy_store_or_no_home_never_break_kimi(self) -> None:
        for stdin in ("not json", "[]", json.dumps({"cwd": str(self.repo)}),
                      json.dumps({"session_id": "../../escape", "cwd": str(self.repo)})):
            result = self.hist("hook", "kimi", "UserPromptSubmit", stdin=stdin)
            self.assertEqual((result.returncode, result.stdout), (0, ""), stdin)
            self.assertIn("the agent continues without history", result.stderr)
        self.assertFalse((self.home / "hook-state").exists())
        holder = sqlite3.connect(self.home / "hist.db", isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA locking_mode = EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("UPDATE meta SET value = value WHERE key = 'generation'")
        try:
            result = self.prompt(KIMI)
        finally:
            holder.execute("ROLLBACK")
            holder.execute("PRAGMA locking_mode = NORMAL")
            holder.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertIn("the agent continues without history", result.stderr)
        missing = self.base / "no-hist-home"
        result = self.prompt(NESTED, env={"HIST_HOME": str(missing)})
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse(missing.exists())

    def test_a_slow_git_still_ends_the_first_prompt_inside_kimis_timeout(self) -> None:
        # A directory no sync has seen yet, so the hook asks Git which repository it is in.
        fresh = git_repo(self.base / "work" / "fresh", "git@github.com:me/fresh.git")
        slow = Path(tempfile.mkdtemp(dir=self.base))
        (slow / "git").write_text("#!/bin/sh\nexec sleep 20\n")
        (slow / "git").chmod(0o755)
        started = time.monotonic()
        result = self.prompt(KIMI, cwd=fresh, env={"PATH": f"{slow}:{os.environ['PATH']}"})
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(self.marker(KIMI))

    def test_a_reader_gone_before_the_view_still_exits_zero(self) -> None:
        reader, writer = os.pipe()
        os.close(reader)
        try:
            result = subprocess.run([sys.executable, "-m", "hist", "hook", "kimi", "UserPromptSubmit"],
                                    cwd=self.repo, env=self.environment(), input=self.payload(KIMI),
                                    stdout=writer, stderr=subprocess.PIPE, text=True, timeout=60)
        finally:
            os.close(writer)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(self.marker(KIMI))

    def test_concurrent_first_prompts_of_one_session_print_one_view(self) -> None:
        runs = [subprocess.Popen([sys.executable, "-m", "hist", "hook", "kimi", "UserPromptSubmit"],
                                 cwd=self.repo, env=self.environment(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(4)]
        # Each waits for its whole input, so all four start reading the store together.
        for run in runs:
            assert run.stdin is not None
            run.stdin.write(self.payload(KIMI))
        for run in runs:
            run.stdin.close()  # type: ignore[union-attr]
        outputs = [run.communicate(timeout=60) for run in runs]
        self.assertEqual([(run.returncode, err) for run, (_, err) in zip(runs, outputs)], [(0, "")] * 4)
        printed = [out for out, _ in outputs if out]
        self.assertEqual(len(printed), 1, outputs)
        self.assertIn("we chose the table-driven guard", printed[0])

    def test_kimi_first_prompt_without_parent_skips_write_transaction(self) -> None:
        holder = sqlite3.connect(self.home / "hist.db", isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = self.prompt(KIMI)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertIn("we chose the table-driven guard", result.stdout)

            # Contrast: a Kimi session with a parent above needs store.write() for the link.
            self.claude_above()
            linked = self.prompt(NESTED, shells=("kimi-code",))
            self.assertEqual((linked.returncode, linked.stdout), (0, ""))
            self.assertIn("StoreBusy", linked.stderr)
        finally:
            holder.execute("ROLLBACK")

    def test_kimi_prompts_trigger_debounced_catch_up_including_later_prompts(self) -> None:
        stamp = self.home / "catch-up.stamp"
        pending1 = "message indexed by kimi first prompt catch-up"
        write_jsonl(Codex.path(self.codex, PRIOR), [Codex.user(pending1, 3)])
        first = self.prompt(KIMI, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((first.returncode, first.stderr), (0, ""))
        self.assertIn("we chose the table-driven guard", first.stdout)
        self.assertTrue(stamp.is_file())
        first_mtime_ns = stamp.stat().st_mtime_ns
        db = self.db()
        deadline = time.monotonic() + 30.0
        while not db.execute("SELECT 1 FROM messages WHERE text = ? LIMIT 1", (pending1,)).fetchone():
            self.assertLess(time.monotonic(), deadline, "kimi first prompt catch-up never indexed pending message")
            time.sleep(0.05)
        lock_path = self.home / "sync.lock"
        if lock_path.exists():
            with open(lock_path, "a+") as handle:
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        break
                    except BlockingIOError:
                        self.assertLess(time.monotonic(), deadline, "first kimi catch-up still holds sync.lock")
                        time.sleep(0.05)

        # Immediate second prompt stays inside cooldown and skips respawn.
        second = self.prompt(KIMI, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        self.assertEqual(stamp.stat().st_mtime_ns, first_mtime_ns)

        # After cooldown expires, prompt 2+ (early return via marker) still triggers catch-up.
        pending2 = "message indexed by kimi prompt 2 catch-up"
        write_jsonl(Codex.path(self.codex, PRIOR), [Codex.user(pending2, 4)])
        expired = time.time() - hook.HOOK_CATCH_UP_COOLDOWN - 5.0
        os.utime(stamp, (expired, expired))
        third = self.prompt(KIMI, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((third.returncode, third.stdout, third.stderr), (0, "", ""))
        self.assertGreater(stamp.stat().st_mtime, expired)
        deadline = time.monotonic() + 30.0
        while not db.execute("SELECT 1 FROM messages WHERE text = ? LIMIT 1", (pending2,)).fetchone():
            self.assertLess(time.monotonic(), deadline, "kimi prompt 2+ catch-up never indexed pending message")
            time.sleep(0.05)
        if lock_path.exists():
            with open(lock_path, "a+") as handle:
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        break
                    except BlockingIOError:
                        self.assertLess(time.monotonic(), deadline, "second kimi catch-up still holds sync.lock")
                        time.sleep(0.05)

        # Invalid steer payload without session_id is rejected and never triggers catch-up.
        stamp.unlink()
        bad_steer = self.hist("hook", "kimi", "UserPromptSubmit",
                              stdin=json.dumps({"is_steer": True}),
                              env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((bad_steer.returncode, bad_steer.stdout), (0, ""))
        self.assertIn("the agent continues without history", bad_steer.stderr)
        self.assertFalse(stamp.exists())

        # Prompt 2+ with missing DB never spawns or creates stamp or DB.
        db.close()
        (self.home / "hist.db").unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = self.home / f"hist.db{suffix}"
            if sidecar.exists():
                sidecar.unlink()
        later = self.prompt(KIMI, env={"HIST_NO_CATCH_UP": None})
        self.assertEqual((later.returncode, later.stdout, later.stderr), (0, "", ""))
        self.assertFalse(stamp.exists())
        self.assertFalse((self.home / "hist.db").exists())
