"""End-to-end storage routing contracts for Codex and Claude."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from omem.layout import current_repo
from omem.store import MemoryStore

from .helpers import make_repo, run_memory


class HostRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "memory"
        self.repo = make_repo(
            self.base / "repo", "git@example.test:team/host-routing.git"
        )
        identity = current_repo(self.repo)
        self.assertIsNotNone(identity)
        self.repo_id = identity.store_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _set_mode(self, mode: str) -> None:
        self.root.mkdir(exist_ok=True)
        (self.root / "config.toml").write_text(
            f'[storage]\nmode = "{mode}"\n', encoding="utf-8"
        )

    def _note(self, host: str, text: str) -> None:
        result = run_memory(
            self.root,
            "--host",
            host,
            "note",
            "repo:fact:observed",
            "--context",
            f"{host} routing fixture",
            text,
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _candidates(self, host: str, pattern: str) -> str:
        result = run_memory(
            self.root,
            "--host",
            host,
            "candidates",
            "repo",
            pattern,
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_per_host_mode_isolates_and_mode_switches_never_move_data(self) -> None:
        self._set_mode("per-host")
        missing_host = run_memory(self.root, "init", cwd=self.repo)
        self.assertEqual(missing_host.returncode, 2)
        self.assertFalse((self.root / "hosts").exists())

        for host in ("codex", "claude"):
            initialized = run_memory(
                self.root, "--host", host, "init", cwd=self.repo
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self._note("codex", "codex-only-routing-sentinel")
        self._note("claude", "claude-only-routing-sentinel")

        codex_view = self._candidates("codex", "routing-sentinel")
        claude_view = self._candidates("claude", "routing-sentinel")
        self.assertIn("codex-only-routing-sentinel", codex_view)
        self.assertNotIn("claude-only-routing-sentinel", codex_view)
        self.assertIn("claude-only-routing-sentinel", claude_view)
        self.assertNotIn("codex-only-routing-sentinel", claude_view)

        paths = {
            host: self.root
            / "hosts"
            / host
            / "repos"
            / self.repo_id
            / "OBSERVATIONS.log"
            for host in ("codex", "claude")
        }
        before = {host: path.read_bytes() for host, path in paths.items()}

        self._set_mode("pooled")
        pooled_init = run_memory(self.root, "init", cwd=self.repo)
        self.assertEqual(pooled_init.returncode, 0, pooled_init.stderr)
        pooled = run_memory(
            self.root,
            "candidates",
            "repo",
            "routing-sentinel",
            cwd=self.repo,
        )
        self.assertEqual(pooled.returncode, 0, pooled.stderr)
        self.assertIn("matches=0", pooled.stdout)
        status = run_memory(self.root, "status", cwd=self.repo)
        self.assertIn("inactive_hosts=codex,claude", status.stdout)

        self._set_mode("per-host")
        self.assertIn(
            "codex-only-routing-sentinel",
            self._candidates("codex", "routing-sentinel"),
        )
        self.assertIn(
            "claude-only-routing-sentinel",
            self._candidates("claude", "routing-sentinel"),
        )
        self.assertEqual(
            before, {host: path.read_bytes() for host, path in paths.items()}
        )

    def test_wrong_host_cannot_apply_a_pending_consolidation(self) -> None:
        self._set_mode("per-host")
        initialized = run_memory(
            self.root, "--host", "codex", "init", cwd=self.repo
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self._note("codex", "codex-pending-routing-sentinel")
        request = run_memory(
            self.root,
            "--host",
            "codex",
            "consolidate",
            "repo",
            cwd=self.repo,
        )
        self.assertEqual(request.returncode, 0, request.stderr)
        bundle = json.loads(request.stdout)
        result = {
            "scope": "repo",
            "base_generation": bundle["base_generation"],
            "source_count": bundle["source_count"],
            "policy_digest": bundle["policy_digest"],
            "items": [
                {
                    "kind": "fact",
                    "provenance": "observed",
                    "text": "codex-pending-routing-sentinel",
                    "applicability": {
                        "mode": "general",
                        "when": "Testing selected-host pending lookup",
                    },
                    "promotion_reason": "durable routing fixture",
                    "source_ids": [0],
                }
            ],
            "deferred": [],
            "rejected": [],
            "retired": [],
        }
        encoded = json.dumps(result)

        wrong_host = run_memory(
            self.root,
            "--host",
            "claude",
            "consolidate",
            "apply",
            bundle["request_id"],
            cwd=self.repo,
            stdin=encoded,
        )
        self.assertEqual(wrong_host.returncode, 2)
        self.assertIn("unknown pending consolidation", wrong_host.stderr)
        codex_store = self.root / "hosts" / "codex" / "repos" / self.repo_id
        pending = (
            codex_store
            / "consolidations"
            / "pending"
            / f'{bundle["request_id"]}.json'
        )
        self.assertTrue(pending.is_file())
        self.assertFalse(
            (codex_store / "consolidations" / "current.json").exists()
        )

        correct_host = run_memory(
            self.root,
            "--host",
            "codex",
            "consolidate",
            "apply",
            bundle["request_id"],
            cwd=self.repo,
            stdin=encoded,
        )
        self.assertEqual(correct_host.returncode, 0, correct_host.stderr)

    def test_pooled_cross_host_writers_share_one_contiguous_log(self) -> None:
        initialized = run_memory(
            self.root, "--host", "codex", "init", cwd=self.repo
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        def write(index: int):
            host = "codex" if index % 2 == 0 else "claude"
            return run_memory(
                self.root,
                "--host",
                host,
                "note",
                "repo:fact:observed",
                "--context",
                f"concurrent {host} write {index}",
                f"pooled-writer-{index}",
                cwd=self.repo,
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(write, range(12)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)

        store = MemoryStore(self.root / "repos" / self.repo_id, "repo")
        observations = store.observation_snapshot()
        self.assertEqual([item.id for item in observations], list(range(12)))
        self.assertEqual(
            {item.text for item in observations},
            {f"pooled-writer-{index}" for index in range(12)},
        )


if __name__ == "__main__":
    unittest.main()
