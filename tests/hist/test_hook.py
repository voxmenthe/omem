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
        out, _ = self.claude_start(NEW)
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "SessionStart")
        context = payload["additionalContext"]
        self.assertTrue(context.startswith("<hist>\n"), context)
        self.assertIn("we chose the table-driven guard", context)
        self.assertLessEqual(len(context.encode()), 8000)

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
