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

from .helpers import git, make_legacy_store, make_repo


def _append_many(path: str, count: int, prefix: str) -> None:
    store = MemoryStore(Path(path), "self")
    for index in range(count):
        store.append("fact", "observed", f"{prefix}-{index}")


def _observe_many(path: str, count: int, prefix: str) -> None:
    store = MemoryStore(Path(path), "self")
    for index in range(count):
        store.append_observation(
            "fact", "observed", f"{prefix}-{index}", "concurrent test"
        )


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()
        self.path = scope_path("self")
        self.store = make_legacy_store(self.path, "self")

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
        repo = make_legacy_store(repo_path, "repo")
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


class ObservationStoreTest(unittest.TestCase):
    """Candidate observation log: fixed-width, validated, crash-safe."""

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

    def _observe(self, text: str, context: str = "while testing") -> None:
        self.store.append_observation("fact", "observed", text, context)

    def test_new_store_initializes_current_layout_only(self) -> None:
        self.assertTrue(self.store.uses_observation_log())
        self.assertFalse(self.store.log_path.exists())
        self.assertTrue(
            (self.path / "consolidations" / "pending").is_dir()
        )
        self.assertTrue(
            (self.path / "consolidations" / "generations").is_dir()
        )
        self.assertEqual(
            self.store.observations_path.stat().st_mode & 0o777, 0o600
        )

    def test_legacy_store_stays_legacy_on_initialize(self) -> None:
        legacy = make_legacy_store(self.root / "legacy", "self")
        legacy.append("fact", "observed", "old note")
        legacy.initialize()
        self.assertFalse(legacy.uses_observation_log())
        self.assertFalse(legacy.observations_path.exists())
        with self.assertRaisesRegex(StoreCorrupt, "legacy store"):
            legacy.append_observation("fact", "observed", "text", "context")

    def test_context_and_field_validation(self) -> None:
        cases = [
            ("procedure", "observed", "text", "context"),
            ("fact", "mixed", "text", "context"),
            ("fact", "observed", "", "context"),
            ("fact", "observed", "text", ""),
            ("fact", "observed", " leading", "context"),
            ("fact", "observed", "text", "trailing "),
            ("fact", "observed", "line\none", "context"),
            ("fact", "observed", "text", "line\none"),
            ("fact", "observed", "x" * 281, "context"),
            ("fact", "observed", "text", "x" * 769),
        ]
        for kind, provenance, text, context in cases:
            with self.subTest(kind=kind, text=text[:12], context=context[:12]):
                with self.assertRaises(StoreCorrupt):
                    self.store.append_observation(kind, provenance, text, context)
        self.assertEqual(self.store.observation_count(), 0)

    def test_utf8_byte_limits_are_exact_and_multibyte(self) -> None:
        self._observe("界" * 70, "界" * 192)  # 210 and 576 bytes: fit
        with self.assertRaisesRegex(StoreCorrupt, "maximum is 280"):
            self._observe("界" * 94)  # 282 bytes
        with self.assertRaisesRegex(StoreCorrupt, "maximum is 768"):
            self.store.append_observation(
                "fact", "observed", "ok", "界" * 257
            )  # 771 bytes
        self.assertEqual(self.store.observation_count(), 1)

    def test_worst_case_realistic_record_fits_the_fixed_width(self) -> None:
        observation = self.store.append_observation(
            "preference",
            "observed",
            "界" * 70 + "x" * (280 - 210),
            "界" * 192 + "x" * (768 - 576),
        )
        self.assertEqual(observation.id, 0)
        decoded = self.store.observation_at(0)
        self.assertEqual(decoded.text, observation.text)
        self.assertEqual(decoded.trigger_context, observation.trigger_context)

    def test_control_heavy_record_fails_with_encoded_size_error(self) -> None:
        with self.assertRaisesRegex(StoreCorrupt, "encoded observation record"):
            self._observe("\x1b" * 280, "\x1b" * 768)

    def test_torn_tail_is_ignored_by_reads_and_repaired_on_append(self) -> None:
        self._observe("complete")
        with self.store.observations_path.open("ab") as handle:
            handle.write(b"partial crash")
        with self.assertRaises(StoreCorrupt):
            self.store.observation_count()
        self.assertEqual(
            [item.text for item in self.store.observation_snapshot()],
            ["complete"],
        )
        repaired = self.store.append_observation(
            "fact", "observed", "after repair", "while testing"
        )
        self.assertEqual(repaired.id, 1)
        self.assertEqual(
            [item.text for item in self.store.observation_snapshot()],
            ["complete", "after repair"],
        )

    def test_slice_and_seek_decode_with_positional_identity(self) -> None:
        for index in range(5):
            self._observe(f"observation {index}")
        self.assertEqual(
            [item.text for item in self.store.observation_slice(1, 4)],
            ["observation 1", "observation 2", "observation 3"],
        )
        self.assertEqual(self.store.observation_at(4).text, "observation 4")
        with self.assertRaisesRegex(StoreCorrupt, "outside the log"):
            self.store.observation_slice(4, 6)

    def test_decode_rejects_identity_and_timestamp_abuse(self) -> None:
        from omem.store import OBSERVATION_RECORD_BYTES, decode_observation_records

        self._observe("real")
        data = bytearray(self.store.observations_path.read_bytes())
        record = data[: OBSERVATION_RECORD_BYTES].decode("utf-8").rstrip()
        import json

        payload = json.loads(record)
        payload["id"] = 7
        tampered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        data[: OBSERVATION_RECORD_BYTES] = (
            tampered.encode("utf-8")
            + b" " * (OBSERVATION_RECORD_BYTES - 1 - len(tampered))
            + b"\n"
        )
        with self.assertRaisesRegex(StoreCorrupt, "identity mismatch"):
            decode_observation_records(bytes(data[:OBSERVATION_RECORD_BYTES]), "self")

        payload["id"] = 0
        payload["recorded_at"] = "2026-08-23"
        tampered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        data[: OBSERVATION_RECORD_BYTES] = (
            tampered.encode("utf-8")
            + b" " * (OBSERVATION_RECORD_BYTES - 1 - len(tampered))
            + b"\n"
        )
        with self.assertRaisesRegex(StoreCorrupt, "timestamp"):
            decode_observation_records(bytes(data[:OBSERVATION_RECORD_BYTES]), "self")

    def test_concurrent_observation_writers_stay_contiguous(self) -> None:
        processes = [
            multiprocessing.Process(
                target=_observe_many, args=(str(self.path), 10, f"writer-{index}")
            )
            for index in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        observations = self.store.observation_snapshot()
        self.assertEqual(
            [item.id for item in observations], list(range(40))
        )
        self.assertEqual(len({item.text for item in observations}), 40)


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
            state = base / ".codex-state"
            native_roots = (
                state / "repos" / "fixture-id" / "memories",
                state / "bare.20260809-1234" / "memories",
                base / ".codex" / "memories",
                # Observed account, launcher, and memories_v2 layouts.
                base / ".codex2" / "memories",
                base / ".codex" / "memories_v2",
                base / ".codex-state2" / "repos" / "fixture-id" / "memories_v2",
                state / "bare" / "codex-bare.20260909094306.83358.x" / "memories",
                state / "bare" / "repos" / "fixture-id" / "variant" / "memories",
                state / "exec" / "cdxe.20260612100811.61022.x" / "memories",
                state / "fresh" / "codex-fresh.20260810151848.1.x" / "memories",
                state / "Volumes_cdrive_repos_fixture" / "main" / "memories",
                base / ".codex-par" / "run1" / "memories",
                base / ".codex-state-par" / "run1" / "memories",
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
                base / "memories_v2",
                base / "project" / "memories",
                base / ".codex" / "project" / "memories",
                base / ".codex2" / "project" / "memories_v2",
                base / ".codex-tools" / "memories",
                state / "memories",
                state / "repos" / "fixture-id" / "memory",
                state / "repos" / "fixture" / "memories-copy",
                state / "a" / "b" / "c" / "d" / "e" / "memories",
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
