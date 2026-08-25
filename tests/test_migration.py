"""Staged legacy-to-current migration tests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omem.consolidation import request_consolidation
from omem.dreams import DreamError, apply_dream, request_dream
from omem.generations import load_current_generation
from omem.layout import scope_path
from omem.migration import (
    LEGACY_TRIGGER_CONTEXT,
    _staging_root,
    apply_migration,
    request_migration,
)
from omem.models import ConsolidationError
from omem.store import MemoryStore

from .helpers import make_legacy_store


def _result(bundle: dict, **overrides) -> dict:
    result = {
        "scope": bundle["scope"],
        "base_generation": bundle["base_generation"],
        "source_count": bundle["source_count"],
        "policy_digest": bundle["policy_digest"],
        "items": [],
        "deferred": [],
        "rejected": [],
        "retired": [],
    }
    result.update(overrides)
    return result


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()
        self.store = make_legacy_store(scope_path("self"), "self")

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def _note(self, text: str, provenance: str = "observed") -> None:
        self.store.append("fact", provenance, text)

    def test_full_migration_commits_atomically_and_preserves_legacy_files(self) -> None:
        self._note("durable legacy lesson")
        self._note("legacy noise")
        legacy_bytes = self.store.log_path.read_bytes()
        bundle = request_migration(self.store)
        self.assertEqual(bundle["scope"], "self")
        self.assertIn("migration", bundle)
        self.assertEqual(bundle["migration"]["legacy_note_count"], 2)
        self.assertEqual(
            bundle["observations"][1]["trigger_context"], LEGACY_TRIGGER_CONTEXT
        )
        # Legacy behavior is unchanged while staging is pending.
        self.assertFalse(self.store.uses_observation_log())
        self._note("arrived mid-migration")

        with self.assertRaisesRegex(ConsolidationError, "changed during migration"):
            apply_migration(
                bundle["request_id"],
                _result(
                    bundle,
                    rejected=[
                        {"source_id": 0, "reason": "kept"},
                        {"source_id": 1, "reason": "noise"},
                    ],
                ),
            )
        # The concurrent append invalidated staging; migrate rebuilds it.
        bundle = request_migration(self.store)
        self.assertEqual(bundle["migration"]["legacy_note_count"], 3)
        legacy_bytes = self.store.log_path.read_bytes()
        message = apply_migration(
            bundle["request_id"],
            _result(
                bundle,
                items=[
                    {
                        "kind": "fact",
                        "provenance": "observed",
                        "text": "durable legacy lesson",
                        "applicability": {"mode": "general", "when": "Any review"},
                        "promotion_reason": "authoritative legacy content",
                        "source_ids": [0],
                    }
                ],
                rejected=[
                    {"source_id": 1, "reason": "noise"},
                    {"source_id": 2, "reason": "status"},
                ],
            ),
        )
        self.assertIn("migration committed", message)
        self.assertTrue(self.store.uses_observation_log())
        self.assertEqual(self.store.log_path.read_bytes(), legacy_bytes)
        self.assertFalse(_staging_root(self.store).exists())

        generation = load_current_generation(self.store)
        self.assertIsNotNone(generation)
        self.assertEqual(generation.source_count, 3)
        self.assertEqual(generation.items[0].text, "durable legacy lesson")
        self.assertEqual(self.store.observation_count(), 3)
        # The new store continues with the ordinary protocol.
        followup = request_consolidation(self.store)
        self.assertEqual(followup["source_checkpoint"], 3)

    def test_uncommitted_migration_leaves_legacy_behavior_unchanged(self) -> None:
        self._note("legacy lesson")
        request_migration(self.store)
        self.assertFalse(self.store.uses_observation_log())
        note = self.store.append("fact", "observed", "legacy writes continue")
        self.assertEqual(note.id, 1)
        dream = request_dream(self.store)
        self.assertEqual(dream["source_count"], 2)

    def test_failed_bootstrap_never_activates_the_new_layout(self) -> None:
        self._note("legacy lesson")
        bundle = request_migration(self.store)
        with self.assertRaises(ConsolidationError):
            apply_migration(bundle["request_id"], _result(bundle, source_count=99))
        self.assertFalse(self.store.uses_observation_log())
        self.assertFalse(self.store.observations_path.exists())
        # Deleting only the staging directory abandons the migration cleanly.
        import shutil

        shutil.rmtree(_staging_root(self.store))
        bundle = request_migration(self.store)
        self.assertEqual(bundle["source_checkpoint"], 0)

    def test_empty_and_dreamed_legacy_stores_migrate(self) -> None:
        # Empty legacy store.
        empty = make_legacy_store(self.root / "repos" / "empty-store", "self")
        bundle = request_migration(empty)
        message = apply_migration(bundle["request_id"], _result(bundle))
        self.assertIn("migration committed", message)
        self.assertTrue(empty.uses_observation_log())
        self.assertEqual(empty.observation_count(), 0)

        # Legacy store with a valid dream: the dream is a labeled review aid.
        self._note("dreamed lesson")
        dream_bundle = request_dream(self.store)
        apply_dream(
            dream_bundle["dream_id"],
            {
                "version": 1,
                "scope": "self",
                "source_count": 1,
                "items": [
                    {
                        "kind": "fact",
                        "standing": "current",
                        "provenance": "observed",
                        "text": "dreamed lesson",
                        "source_ids": [0],
                    }
                ],
            },
        )
        bundle = request_migration(self.store)
        projection = bundle["migration"]["prior_projection"]
        self.assertIsNotNone(projection)
        self.assertEqual(projection["items"][0]["text"], "dreamed lesson")
        apply_migration(
            bundle["request_id"],
            _result(
                bundle,
                rejected=[{"source_id": 0, "reason": "context unavailable"}],
            ),
        )
        self.assertTrue(self.store.uses_observation_log())
        # An old pending dream can no longer replace the integrated generation.
        stale_dream = request_dream  # guard against accidental reuse
        del stale_dream
        with self.assertRaisesRegex(DreamError, "legacy dream results"):
            apply_dream(
                dream_bundle["dream_id"],
                {
                    "version": 1,
                    "scope": "self",
                    "source_count": 1,
                    "items": [],
                },
            )

    def test_store_above_256_migrates_through_bounded_batches(self) -> None:
        total = 260
        for index in range(total):
            self._note(f"legacy note {index}")
        bundle = request_migration(self.store)
        self.assertEqual(len(bundle["observations"]), 256)
        message = apply_migration(
            bundle["request_id"],
            _result(
                bundle,
                deferred=[
                    {"source_id": index, "reason": "awaiting context"}
                    for index in range(256)
                ],
            ),
        )
        self.assertIn("4 legacy notes remain", message)
        self.assertFalse(self.store.uses_observation_log())

        # The deferred backlog alone fills the next request; headroom must be
        # restored before new notes can advance.
        bundle = request_migration(self.store)
        self.assertEqual(bundle["source_count"], 256)
        self.assertTrue(bundle["headroom_required"])
        self.assertEqual(len(bundle["deferred"]), 256)
        apply_migration(
            bundle["request_id"],
            _result(
                bundle,
                rejected=[
                    {"source_id": index, "reason": "no reuse evidence"}
                    for index in range(256)
                ],
            ),
        )

        bundle = request_migration(self.store)
        self.assertEqual(bundle["source_count"], total)
        message = apply_migration(
            bundle["request_id"],
            _result(
                bundle,
                rejected=[
                    {"source_id": index, "reason": "no reuse evidence"}
                    for index in range(256, total)
                ],
            ),
        )
        self.assertIn("migration committed", message)
        self.assertTrue(self.store.uses_observation_log())
        self.assertEqual(self.store.observation_count(), total)

    def test_config_change_during_bootstrap_makes_apply_stale(self) -> None:
        self._note("legacy lesson")
        bundle = request_migration(self.store)
        self.root.joinpath("config.toml").write_text(
            '[consolidation]\ncatalog_pruning = "aggressive"\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ConsolidationError, "policy changed"):
            apply_migration(
                bundle["request_id"],
                _result(
                    bundle,
                    rejected=[{"source_id": 0, "reason": "noise"}],
                ),
            )
        self.assertFalse(self.store.uses_observation_log())
        self.assertFalse(self.store.observations_path.exists())
        # The root policy file is never copied into the scope store.
        self.assertFalse((self.store.path / "config.toml").exists())

    def test_corrupt_staged_log_rebuilds_on_next_migrate(self) -> None:
        self._note("legacy lesson")
        bundle = request_migration(self.store)
        staged_log = _staging_root(self.store) / "OBSERVATIONS.log"
        staged_log.write_bytes(b"corrupt")
        with self.assertRaises(ConsolidationError):
            apply_migration(
                bundle["request_id"],
                _result(
                    bundle, rejected=[{"source_id": 0, "reason": "noise"}]
                ),
            )
        self.assertFalse(self.store.uses_observation_log())
        rebuilt = request_migration(self.store)
        self.assertEqual(rebuilt["migration"]["legacy_note_count"], 1)
        self.assertEqual(rebuilt["request_id"], bundle["request_id"])

    def test_migrate_refuses_current_stores(self) -> None:
        current = MemoryStore(self.root / "repos" / "current-store", "self")
        current.initialize()
        with self.assertRaisesRegex(ConsolidationError, "already uses the current"):
            request_migration(current)
