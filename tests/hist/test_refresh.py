"""`hist refresh` brings rows stored under older text rules up to the current ones."""

import json

from tests.hist.fixtures import Claude, Codex, HistCase, write_jsonl
from tests.hist.test_redact import run

THREAD = "019a0000-0000-7000-8000-0000000004a1"
SESSION = "7f000000-0000-4000-8000-0000000000a1"
# Synthetic, assembled so that no scanner mistakes it for a leak.
TOKEN = "GOCSPX" + "-" + run(28)


class RefreshTest(HistCase):
    def setUp(self) -> None:
        super().setUp()
        self.hist("status")
        cwd = str(self.repo)
        escaped = 'Script completed\nOutput:\n\n{"output":"ok\\nTraceback (most recent call last):"}'
        write_jsonl(Codex.path(self.codex, THREAD), [
            Codex.meta(THREAD, cwd, "cli", self.origin),
            Codex.user("run it", 1),
            Codex.call("exec", json.dumps({"cmd": "x"}), "c1", 2),
            Codex.output("c1", escaped, 3),
            Codex.assistant("the client secret is SECRET-SLOT here", 4),
        ])
        # The saved output's file appears only after the sync, as for rows
        # indexed before outputs were read whole.
        self.saved = self.claude / Claude.project(self.claude, cwd).name / SESSION / "tool-results" / "b1.txt"
        write_jsonl(Claude.project(self.claude, cwd) / f"{SESSION}.jsonl", [
            Claude.user("u1", SESSION, cwd, "list it", 5),
            Claude.assistant("a1", SESSION, cwd, [Claude.tool_use("t1", "Bash", {"command": "ls"})], 6),
            Claude.user("r1", SESSION, cwd, [Claude.tool_result("t1", Claude.saved_output(self.saved, "PREVIEW"))], 7),
        ])
        self.hist("sync")
        self.saved.parent.mkdir(parents=True)
        self.saved.write_text("WHOLE-OUTPUT " * 10)
        # Rows as the earlier rules stored them: a secret the old patterns
        # missed, JSON escapes indexed undecoded, and no text version.
        db = self.db()
        with db:
            for message_id, text in db.execute("SELECT id, text FROM messages").fetchall():
                old = text.replace("SECRET-SLOT", TOKEN)
                db.execute("UPDATE messages SET text = ? WHERE id = ?", (old, message_id))
                db.execute("DELETE FROM search WHERE rowid = ?", (message_id,))
                db.execute("INSERT INTO search (rowid, text) VALUES (?, ?)", (message_id, old))
            db.execute("DELETE FROM meta WHERE key = 'text_version'")

    def test_a_dry_run_counts_and_changes_nothing(self) -> None:
        self.assertIn("follow text rules version 1, not 2", self.hist("status").stdout)
        before = self.db().execute("SELECT id, text FROM messages ORDER BY id").fetchall()
        out = self.hist("refresh", "--dry-run").stdout
        self.assertIn("saved outputs: 1 previews would be replaced", out)
        self.assertIn("redaction: 1 messages would be masked further: google-oauth-secret ×1", out)
        self.assertIn("google-oauth-secret: proj:", out)
        self.assertIn("the client secret is [redacted:google-oauth-secret] here", out)
        self.assertNotIn(TOKEN, out)
        self.assertIn("search: 1 messages would be reindexed", out)
        self.assertEqual(self.db().execute("SELECT id, text FROM messages ORDER BY id").fetchall(), before)
        self.assertEqual(self.hist("find", "Traceback").stdout, "no matches\n")

    def test_refresh_replaces_text_in_place_and_can_run_again(self) -> None:
        positions = self.db().execute("SELECT id, timeline_id, position FROM messages ORDER BY id").fetchall()
        self.hist("refresh")
        db = self.db()
        texts = [text for (text,) in db.execute("SELECT text FROM messages ORDER BY id")]
        self.assertFalse([text for text in texts if TOKEN in text])
        self.assertIn("the client secret is [redacted:google-oauth-secret] here", texts)
        self.assertIn("WHOLE-OUTPUT " * 10, texts)
        self.assertEqual(db.execute("SELECT id, timeline_id, position FROM messages ORDER BY id").fetchall(),
                         positions)
        self.assertIn("Traceback (most recent call last)", self.hist("find", "Traceback").stdout)
        self.assertIn("WHOLE-OUTPUT", self.hist("find", "WHOLE-OUTPUT").stdout)
        self.assertNotIn("text rules version", self.hist("status").stdout)
        again = self.hist("refresh").stdout
        self.assertIn("0 previews were replaced", again)
        self.assertIn("redaction: 0 messages were masked further", again)
        self.assertIn("search: 0 messages were reindexed", again)

    def test_an_interrupted_refresh_resumes_after_its_last_batch(self) -> None:
        db = self.db()
        (last,) = db.execute("SELECT max(id) FROM messages").fetchone()
        with db:
            db.execute("INSERT INTO meta (key, value) VALUES ('refresh_cursor', ?)",
                       (json.dumps({"version": 2, "after": last}),))
        self.assertIn("scanned 0 messages", self.hist("refresh").stdout)
        self.assertIsNone(db.execute("SELECT value FROM meta WHERE key = 'refresh_cursor'").fetchone())
        self.assertIn("redaction: 1 messages were masked further", self.hist("refresh").stdout)
