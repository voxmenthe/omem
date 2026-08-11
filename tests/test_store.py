"""Fixed-width store, isolation, and identity tests."""

import fcntl
import multiprocessing
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omem import layout
from omem.layout import current_repo, normalize_origin, scope_path
from omem.models import StoreCorrupt
from omem.store import (
    LOG_RECORD_BYTES,
    WAKE_BUDGET,
    MemoryStore,
)

from .helpers import git, make_repo


def _append_many(path: str, count: int, prefix: str) -> None:
    store = MemoryStore(Path(path), "self")
    for index in range(count):
        store.append("fact", "observed", f"{prefix}-{index}")


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()
        self.path = scope_path("self")
        self.store = MemoryStore(self.path, "self")
        self.store.initialize()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_invalid_metadata_and_text_are_rejected(self) -> None:
        cases = [
            ("procedure", "observed", "not valid for self"),
            ("fact", "mixed", "input cannot be mixed"),
            ("fact", "observed", " leading"),
            ("fact", "observed", "line one\nline two"),
            ("fact", "observed", "x" * 280),
        ]
        for kind, provenance, text in cases:
            with self.subTest(kind=kind, provenance=provenance, text=text[:12]):
                with self.assertRaises(StoreCorrupt):
                    self.store.append(kind, provenance, text)
        self.assertEqual(self.store.count(), 0)

    def test_torn_tail_is_repaired_before_next_append(self) -> None:
        first = self.store.append("fact", "observed", "complete")
        with self.store.log_path.open("ab") as handle:
            handle.write(b"partial crash")
        self.assertNotEqual(self.store.log_path.stat().st_size % LOG_RECORD_BYTES, 0)
        second = self.store.append("fact", "observed", "after repair")
        self.assertEqual((first.id, second.id), (0, 1))
        self.assertEqual(
            [note.text for note in self.store.snapshot()],
            ["complete", "after repair"],
        )

    def test_concurrent_processes_assign_unique_contiguous_ids(self) -> None:
        processes = [
            multiprocessing.Process(
                target=_append_many, args=(str(self.path), 20, f"writer-{index}")
            )
            for index in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        notes = self.store.snapshot()
        self.assertEqual(len(notes), 80)
        self.assertEqual([note.id for note in notes], list(range(80)))
        self.assertEqual(len({note.text for note in notes}), 80)

    def test_recall_is_regex_based_and_scope_local(self) -> None:
        self.store.append("fact", "observed", "alpha only")
        self.store.append("preference", "user", "beta alpha")
        self.store.append("episode", "inferred", "gamma")
        matches = self.store.recall(r"alpha|preference")
        self.assertEqual([note.id for note in matches], [0, 1])
        with self.assertRaises(StoreCorrupt):
            self.store.recall("[")

    def test_captured_prefix_excludes_a_complete_concurrent_append(self) -> None:
        self.store.append("fact", "observed", "captured")
        capture = self.store.capture_prefix(
            deadline=time.monotonic() + 1,
            metadata_loader=lambda: "captured metadata",
        )
        self.store.append("fact", "observed", "next invocation")
        with capture:
            chunks = tuple(capture.chunks(LOG_RECORD_BYTES * 2))
        self.assertEqual(capture.record_count, 1)
        self.assertEqual(capture.metadata, "captured metadata")
        self.assertEqual(
            [note.text for note in self.store.decode_records(b"".join(chunks))],
            ["captured"],
        )

    def test_captured_prefix_keeps_its_inode_after_path_replacement(self) -> None:
        self.store.append("fact", "observed", "captured inode")
        capture = self.store.capture_prefix(deadline=time.monotonic() + 1)
        replacement = self.store.path / "replacement.log"
        replacement.write_bytes(
            self.store.log_path.read_bytes().replace(
                b"captured inode", b"replacement text"
            )
        )
        os.replace(replacement, self.store.log_path)
        with capture:
            data = b"".join(capture.chunks(LOG_RECORD_BYTES))
        notes = self.store.decode_records(data)
        self.assertEqual([note.text for note in notes], ["captured inode"])

    def test_captured_prefix_ignores_partial_tail_without_repairing(self) -> None:
        self.store.append("fact", "observed", "complete")
        with self.store.log_path.open("ab") as handle:
            handle.write(b"partial")
        before = self.store.log_path.read_bytes()
        capture = self.store.capture_prefix(deadline=time.monotonic() + 1)
        with capture:
            data = b"".join(capture.chunks(LOG_RECORD_BYTES))
        self.assertEqual(capture.record_count, 1)
        self.assertEqual(self.store.log_path.read_bytes(), before)
        self.assertEqual(
            [note.text for note in self.store.decode_records(data)],
            ["complete"],
        )

    def test_captured_prefix_reports_a_short_retained_inode(self) -> None:
        self.store.append("fact", "observed", "complete")
        capture = self.store.capture_prefix(deadline=time.monotonic() + 1)
        self.store.log_path.write_bytes(b"")
        with capture, self.assertRaisesRegex(StoreCorrupt, "became shorter"):
            tuple(capture.chunks(LOG_RECORD_BYTES))

    def test_shared_capture_lock_has_a_deterministic_deadline(self) -> None:
        lock_fd = os.open(self.store.lock_path, os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ticks = iter((0.0, 0.0, 0.01, 0.02, 0.03))
        try:
            with self.assertRaisesRegex(TimeoutError, "timed out acquiring"):
                self.store.capture_prefix(
                    deadline=1.0,
                    lock_timeout=0.020,
                    clock=lambda: next(ticks),
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_nap_builds_rebuildable_tree_and_honors_budget(self) -> None:
        for index in range(40):
            self.store.append("fact", "observed", f"note {index}")
        while (pending := self.store.pending_nap()) is not None:
            lo, hi, _ = pending
            self.store.apply_nap(lo, hi, f"summary {lo}-{hi}")
        projection = self.store.chronological()
        self.assertLessEqual(len(projection), WAKE_BUDGET["self"])
        self.assertTrue(any(not item.raw for item in projection))
        self.assertEqual(self.store.nap_debt(), 0)

    def test_self_and_repo_budgets_are_independent(self) -> None:
        repo_path = self.root / "repos" / "fixture"
        repo = MemoryStore(repo_path, "repo")
        repo.initialize()
        for index in range(30):
            self.store.append("fact", "observed", f"self {index}")
        for index in range(45):
            repo.append("fact", "observed", f"repo {index}")
        for store in (self.store, repo):
            while (pending := store.pending_nap()) is not None:
                lo, hi, _ = pending
                store.apply_nap(lo, hi, f"summary {lo}-{hi}")
        self.assertLessEqual(len(self.store.chronological()), WAKE_BUDGET["self"])
        self.assertLessEqual(len(repo.chronological()), WAKE_BUDGET["repo"])


class RepositoryIdentityTest(unittest.TestCase):
    def test_repository_discovery_degrades_on_subprocess_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                layout.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["git"], timeout=0.01),
            ):
                self.assertIsNone(
                    current_repo(
                        Path(directory),
                        deadline=time.monotonic() + 0.1,
                    )
                )

    def test_origin_timeout_does_not_substitute_a_local_repo_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root_result = subprocess.CompletedProcess(
                ["git", "rev-parse", "--show-toplevel"],
                returncode=0,
                stdout=f"{root}\n",
            )
            with mock.patch.object(
                layout.subprocess,
                "run",
                side_effect=(
                    root_result,
                    subprocess.TimeoutExpired(["git"], timeout=0.01),
                ),
            ):
                self.assertIsNone(
                    current_repo(
                        root,
                        deadline=time.monotonic() + 0.1,
                    )
                )

    def test_recognizes_only_codex_native_memory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            native_roots = (
                base / ".codex-state" / "repos" / "fixture-id" / "memories",
                base / ".codex-state" / "bare.20260809-1234" / "memories",
                base / ".codex" / "memories",
            )
            for native_root in native_roots:
                with self.subTest(native_root=native_root):
                    self.assertTrue(layout.is_codex_native_memory_path(native_root))
                    self.assertTrue(
                        layout.is_codex_native_memory_path(
                            native_root / "rollout_summaries" / "nested"
                        )
                    )

            ordinary_paths = (
                base / "memories",
                base / "project" / "memories",
                base / ".codex" / "project" / "memories",
                base / ".codex-state" / "repos" / "fixture-id" / "memory",
                base / ".codex-state" / "repositories" / "fixture" / "memories",
                base / ".codex-state" / "repos" / "fixture" / "memories-copy",
            )
            for ordinary_path in ordinary_paths:
                with self.subTest(ordinary_path=ordinary_path):
                    self.assertFalse(layout.is_codex_native_memory_path(ordinary_path))

    def test_normalizes_common_remote_spellings(self) -> None:
        expected = "github.com/Owner/Project"
        self.assertEqual(normalize_origin("git@github.com:Owner/Project.git"), expected)
        self.assertEqual(
            normalize_origin("https://user@github.com/Owner/Project.git/"),
            expected,
        )

    def test_clones_and_worktrees_share_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = make_repo(base / "repo", "git@github.com:Example/Stable-Memory.git")
            worktree = base / "worktree"
            git(repo, "worktree", "add", "-q", "-b", "fixture-worktree", str(worktree))
            clone_like = make_repo(
                base / "clone", "https://github.com/Example/Stable-Memory.git"
            )
            identities = [
                current_repo(repo),
                current_repo(worktree),
                current_repo(clone_like),
            ]
            self.assertTrue(all(identity is not None for identity in identities))
            self.assertEqual(
                {identity.store_id for identity in identities if identity},
                {identities[0].store_id},  # type: ignore[union-attr]
            )

    def test_different_repositories_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            one = make_repo(base / "one", "ssh://git@example.test/team/one.git")
            two = make_repo(base / "two", "ssh://git@example.test/team/two.git")
            identity_one = current_repo(one)
            identity_two = current_repo(two)
            self.assertIsNotNone(identity_one)
            self.assertIsNotNone(identity_two)
            self.assertNotEqual(identity_one.store_id, identity_two.store_id)

    def test_relative_local_origin_resolves_from_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = make_repo(base / "repo", "../remote.git")
            identity = current_repo(repo)
            self.assertIsNotNone(identity)
            self.assertEqual(
                identity.key,
                f"remote:{(base / 'remote.git').resolve()}",
            )
