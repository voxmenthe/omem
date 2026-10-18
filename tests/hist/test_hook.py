"""`hist hook`: the view at start, parent links, and never breaking the host."""

import json

from tests.hist.fixtures import Claude, Codex, HistCase, write_jsonl

PRIOR = "019a0000-0000-7000-8000-0000000003a1"
PARENT = "019a0000-0000-7000-8000-0000000003b1"
WORKER = "6c000000-0000-4000-8000-0000000003c1"
NEW = "6c000000-0000-4000-8000-0000000003d1"


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
        result = self.hist("hook", "kimi", "SessionStart", stdin="{}")
        self.assertEqual((result.returncode, result.stdout), (0, ""))
