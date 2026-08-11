"""Dream protocol correctness and failure-safety tests."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omem.dreams import (
    DREAM_BUDGET,
    DreamError,
    apply_dream,
    dream_debt,
    load_current,
    request_dream,
)
from omem.layout import scope_path
from omem.store import MemoryStore


def _result(bundle: dict, text: str = "Canonical fact") -> dict:
    return {
        "version": 1,
        "scope": bundle["scope"],
        "source_count": bundle["source_count"],
        "items": (
            [
                {
                    "kind": "fact",
                    "standing": "current",
                    "provenance": "observed",
                    "text": text,
                    "source_ids": [bundle["source_count"] - 1],
                }
            ]
            if bundle["source_count"]
            else []
        ),
    }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()
        self.store = MemoryStore(scope_path("self"), "self")
        self.store.initialize()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def _note(self, text: str) -> None:
        self.store.append("fact", "observed", text)

    def test_request_is_raw_only_and_binds_source_digest(self) -> None:
        self._note("raw source")
        tree = self.store.tree_path / "2"
        tree.write_text("derived tree must not appear\n", encoding="utf-8")
        bundle = request_dream(self.store)
        serialized = str(bundle)
        self.assertIn("raw source", serialized)
        self.assertNotIn("derived tree must not appear", serialized)
        self.assertEqual(bundle["source_count"], 1)
        self.assertEqual(len(bundle["source_digest"]), 64)

    def test_request_requires_selective_consolidation(self) -> None:
        repo_store = MemoryStore(self.root / "repos" / "fixture", "repo")
        repo_store.initialize()
        self._note("self source")
        repo_store.append("fact", "observed", "repo source")

        for store in (self.store, repo_store):
            with self.subTest(scope=store.scope):
                bundle = request_dream(store)
                contract = " ".join(
                    f"{bundle['rubric']} {bundle['instructions']}".split()
                )
                self.assertEqual(bundle["prompt_version"], 2)
                self.assertIn("smallest decision-useful current set", contract)
                self.assertIn("Merge sources", contract)
                self.assertIn(
                    "later corrections supersede earlier claims",
                    contract,
                )
                self.assertIn(
                    "one-off, obsolete, or cheaply recoverable details",
                    contract,
                )
                self.assertIn(
                    "hard ceiling, not a target or source-coverage requirement",
                    contract,
                )

    def test_prompt_v2_upgrades_v1_at_the_same_checkpoint_without_downgrade(
        self,
    ) -> None:
        self._note("source")
        current_bundle = request_dream(self.store)
        legacy_bundle = dict(current_bundle)
        legacy_bundle["prompt_version"] = 1
        legacy_bundle["rubric"] = "Legacy version 1 rubric"
        legacy_bundle["instructions"] = "Legacy version 1 instructions"
        legacy_bundle["dream_id"] = _digest(
            {
                "prompt_version": 1,
                "scope": legacy_bundle["scope"],
                "store_key": legacy_bundle["store_key"],
                "source_count": legacy_bundle["source_count"],
                "source_digest": legacy_bundle["source_digest"],
            }
        )[:24]
        pending_path = (
            self.store.path
            / "dreams"
            / "pending"
            / f"{legacy_bundle['dream_id']}.json"
        )
        pending_path.write_text(
            json.dumps(legacy_bundle, sort_keys=True),
            encoding="utf-8",
        )

        legacy = apply_dream(
            legacy_bundle["dream_id"],
            _result(legacy_bundle, "Legacy projection"),
        )
        self.assertEqual(legacy.items[0].text, "Legacy projection")

        upgraded = apply_dream(
            current_bundle["dream_id"],
            _result(current_bundle, "Selective projection"),
        )
        self.assertEqual(upgraded.source_count, legacy.source_count)
        self.assertEqual(upgraded.items[0].text, "Selective projection")
        pointer_before_downgrade = (
            self.store.path / "dreams" / "current.json"
        ).read_bytes()
        generation = json.loads(pointer_before_downgrade)["generation"]
        payload = json.loads(
            (
                self.store.path / "dreams" / "generations" / generation
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["prompt_version"], 2)

        with self.assertRaisesRegex(DreamError, "older prompt version"):
            apply_dream(
                legacy_bundle["dream_id"],
                _result(legacy_bundle, "Legacy projection"),
            )
        self.assertEqual(
            (self.store.path / "dreams" / "current.json").read_bytes(),
            pointer_before_downgrade,
        )

    def test_identical_repo_snapshots_have_store_unique_dream_ids(self) -> None:
        first = MemoryStore(self.root / "repos" / "first", "repo")
        second = MemoryStore(self.root / "repos" / "second", "repo")
        first.initialize()
        second.initialize()
        for store in (first, second):
            store.append("fact", "observed", "same source")
        first_bundle = request_dream(first)
        second_bundle = request_dream(second)
        self.assertNotEqual(
            first_bundle["dream_id"], second_bundle["dream_id"]
        )
        self.assertNotEqual(
            first_bundle["store_key"], second_bundle["store_key"]
        )

    def test_valid_result_is_source_cited_and_idempotent(self) -> None:
        self._note("source")
        bundle = request_dream(self.store)
        first = apply_dream(bundle["dream_id"], _result(bundle))
        pointer = (self.store.path / "dreams" / "current.json").read_bytes()
        second = apply_dream(bundle["dream_id"], _result(bundle))
        self.assertEqual(first, second)
        self.assertEqual(
            (self.store.path / "dreams" / "current.json").read_bytes(),
            pointer,
        )
        self.assertEqual(first.items[0].source_ids, (0,))

    def test_range_and_budget_violations_preserve_pointer(self) -> None:
        self._note("source")
        bundle = request_dream(self.store)
        apply_dream(bundle["dream_id"], _result(bundle))
        pointer_path = self.store.path / "dreams" / "current.json"
        before = pointer_path.read_bytes()

        outside = _result(bundle)
        outside["items"][0]["source_ids"] = [1]
        with self.assertRaises(DreamError):
            apply_dream(bundle["dream_id"], outside)
        self.assertEqual(pointer_path.read_bytes(), before)

        too_many = _result(bundle)
        too_many["items"] = too_many["items"] * (DREAM_BUDGET["self"] + 1)
        with self.assertRaises(DreamError):
            apply_dream(bundle["dream_id"], too_many)
        self.assertEqual(pointer_path.read_bytes(), before)

    def test_pending_request_must_match_canonical_raw_prefix(self) -> None:
        self._note("source")
        bundle = request_dream(self.store)
        pending_path = (
            self.store.path
            / "dreams"
            / "pending"
            / f"{bundle['dream_id']}.json"
        )
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pending["sources"][0]["text"] = "tampered"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        with self.assertRaisesRegex(DreamError, "source digest is invalid"):
            apply_dream(bundle["dream_id"], _result(bundle))
        self.assertFalse(
            (self.store.path / "dreams" / "current.json").exists()
        )

    def test_result_version_and_source_count_reject_json_booleans(self) -> None:
        self._note("source")
        bundle = request_dream(self.store)
        result = _result(bundle)
        result["version"] = True
        with self.assertRaisesRegex(DreamError, "version must be 1"):
            apply_dream(bundle["dream_id"], result)
        result = _result(bundle)
        result["source_count"] = True
        with self.assertRaisesRegex(DreamError, "source_count must be 1"):
            apply_dream(bundle["dream_id"], result)

    def test_stale_and_conflicting_results_preserve_newer_pointer(self) -> None:
        self._note("old")
        old_bundle = request_dream(self.store)
        apply_dream(old_bundle["dream_id"], _result(old_bundle, "old dream"))
        self._note("correction")
        new_bundle = request_dream(self.store)
        apply_dream(new_bundle["dream_id"], _result(new_bundle, "corrected"))
        pointer_path = self.store.path / "dreams" / "current.json"
        newer = pointer_path.read_bytes()

        with self.assertRaisesRegex(DreamError, "stale"):
            apply_dream(old_bundle["dream_id"], _result(old_bundle, "old dream"))
        self.assertEqual(pointer_path.read_bytes(), newer)

        conflict = _result(new_bundle, "conflicting")
        with self.assertRaisesRegex(DreamError, "conflicting"):
            apply_dream(new_bundle["dream_id"], conflict)
        self.assertEqual(pointer_path.read_bytes(), newer)

    def test_correction_remains_raw_until_next_dream_supersedes(self) -> None:
        self._note("The setting is alpha")
        old = request_dream(self.store)
        apply_dream(old["dream_id"], _result(old, "The setting is alpha"))
        self._note("Correction: the setting is beta")
        current = load_current(self.store)
        self.assertIsNotNone(current)
        self.assertEqual(current.items[0].text, "The setting is alpha")
        self.assertEqual(
            self.store.slice(1, 2)[0].text,
            "Correction: the setting is beta",
        )
        self.assertEqual(dream_debt(self.store), (1, False))

        new = request_dream(self.store)
        applied = apply_dream(
            new["dream_id"],
            {
                "version": 1,
                "scope": "self",
                "source_count": 2,
                "items": [
                    {
                        "kind": "fact",
                        "standing": "current",
                        "provenance": "observed",
                        "text": "The setting is beta",
                        "source_ids": [0, 1],
                    }
                ],
            },
        )
        self.assertEqual(applied.items[0].text, "The setting is beta")

    def test_post_checkpoint_writes_do_not_invalidate_pending_result(self) -> None:
        self._note("snapshot")
        bundle = request_dream(self.store)
        self._note("arrived during model reasoning")
        applied = apply_dream(bundle["dream_id"], _result(bundle))
        self.assertEqual(applied.source_count, 1)
        self.assertEqual(self.store.count() - applied.source_count, 1)

    def test_unknown_artifact_version_is_ignored_with_visible_error(self) -> None:
        self._note("source")
        bundle = request_dream(self.store)
        apply_dream(bundle["dream_id"], _result(bundle))
        current = load_current(self.store)
        self.assertIsNotNone(current)
        pointer = self.store.path / "dreams" / "current.json"
        pointer.write_text(
            '{"pointer_version":99,"dream_id":"x","generation":"x.json"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DreamError, "unknown dream pointer version"):
            load_current(self.store)
