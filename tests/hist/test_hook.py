"""`hist hook`: the view at start, parent links, and never breaking the host."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.hist.fixtures import Claude, Codex, HistCase, Kimi, git_repo, write_jsonl

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

    def claude_start(self, session: str, source: str = "startup", env: dict[str, str] | None = None,
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
               env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
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
        holder.execute("BEGIN EXCLUSIVE")
        result = self.prompt(KIMI)
        holder.execute("ROLLBACK")
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
