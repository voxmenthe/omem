"""Consolidation protocol, policy, and retirement tests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omem.consolidation import (
    MAX_OFFERED_OBSERVATIONS,
    ROUTINE_DUE_OBSERVATIONS,
    apply_consolidation,
    consolidation_due,
    consolidation_failure_summary,
    pending_consolidation_counts,
    review_opportunity,
    request_consolidation,
)
from omem.generations import (
    DEFAULT_POLICY,
    ITEM_BUDGET,
    config_path,
    fetch_policy,
    live_layout,
    load_current_generation,
    policy_digest,
    validate_generation_sources,
)
from omem.layout import atomic_write_json, canonical_digest, scope_path
from omem.models import ConsolidationError
from omem.store import MemoryStore

from .helpers import make_legacy_store


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def _write_config(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        config_path().write_text(text, encoding="utf-8")

    def test_absent_config_yields_the_documented_default(self) -> None:
        policy = fetch_policy()
        self.assertEqual(policy, DEFAULT_POLICY)
        self.assertEqual(policy.catalog_pruning, "off")
        self.assertEqual(policy.catalog_target("self"), 16)
        self.assertEqual(policy.catalog_target("repo"), 24)
        self.assertEqual(len(policy_digest(policy)), 64)

    def test_valid_non_default_profile_round_trips(self) -> None:
        self._write_config(
            "[consolidation]\n"
            'catalog_pruning = "balanced"\n'
            "self_catalog_target = 8\n"
            "repo_catalog_target = 20\n"
        )
        policy = fetch_policy()
        self.assertEqual(policy.catalog_pruning, "balanced")
        self.assertEqual(policy.self_catalog_target, 8)
        self.assertEqual(policy.repo_catalog_target, 20)
        self.assertNotEqual(policy_digest(policy), policy_digest(DEFAULT_POLICY))

    def test_malformed_configs_fail_clearly(self) -> None:
        cases = (
            "not toml = [",
            'catalog_pruning = "off"\n',
            '[consolidation]\nunknown_key = 1\n',
            '[other]\ncatalog_pruning = "off"\n',
            '[consolidation]\ncatalog_pruning = "ruthless"\n',
            '[consolidation]\ncatalog_pruning = 3\n',
            '[consolidation]\nself_catalog_target = 0\n',
            '[consolidation]\nself_catalog_target = 129\n',
            '[consolidation]\nrepo_catalog_target = 129\n',
            '[consolidation]\nself_catalog_target = true\n',
        )
        for text in cases:
            with self.subTest(text=text):
                self._write_config(text)
                with self.assertRaises(ConsolidationError):
                    fetch_policy()


class ConsolidationTest(unittest.TestCase):
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

    def _observe(self, text: str, context: str = "while testing", provenance: str = "observed") -> int:
        return self.store.append_observation("fact", provenance, text, context).id

    def _result(self, bundle: dict, **overrides) -> dict:
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

    def _item(self, text: str, source_ids: list[int], provenance: str = "observed", when: str = "Across tasks") -> dict:
        return {
            "kind": "fact",
            "provenance": provenance,
            "text": text,
            "applicability": {"mode": "general", "when": when},
            "promotion_reason": "durable and reusable",
            "source_ids": sorted(source_ids),
        }

    def test_legacy_store_is_refused(self) -> None:
        legacy = make_legacy_store(self.root / "legacy", "self")
        with self.assertRaisesRegex(ConsolidationError, "legacy store"):
            request_consolidation(legacy)

    def test_routine_cadence_is_twelve_new_observations(self) -> None:
        for index in range(ROUTINE_DUE_OBSERVATIONS - 1):
            self._observe(f"observation {index}")
        new, due = consolidation_due(self.store)
        self.assertEqual((new, due), (ROUTINE_DUE_OBSERVATIONS - 1, False))
        self._observe("twelfth")
        new, due = consolidation_due(self.store)
        self.assertEqual((new, due), (ROUTINE_DUE_OBSERVATIONS, True))

    def test_sparse_review_uses_age_without_repeated_deferred_reviews(self) -> None:
        import datetime as dt

        self._observe("initial candidate")
        self.assertEqual(review_opportunity(self.store, None, 1)[1], "initial_catalog")
        bundle = request_consolidation(self.store)
        current = apply_consolidation(bundle["request_id"], self._result(
            bundle, deferred=[{"source_id": 0, "reason": "needs independent support"}]
        ))
        self.assertEqual(review_opportunity(self.store, current, 1), (None, None))
        self._observe("later candidate")
        self.assertIsNone(review_opportunity(self.store, current, 2)[1])
        future = dt.datetime.now(dt.UTC) + dt.timedelta(days=8)
        with mock.patch("omem.consolidation.now", return_value=future.isoformat()):
            age, reason = review_opportunity(self.store, current, 2)
        self.assertGreaterEqual(age, 7 * 86400)
        self.assertEqual(reason, "aged_candidates")

    def test_request_binds_policy_evidence_and_checkpoint(self) -> None:
        first = self._observe("first candidate")
        bundle = request_consolidation(self.store)
        self.assertEqual(bundle["protocol"], "memory-consolidation-request")
        self.assertEqual(bundle["base_generation"], None)
        self.assertEqual(bundle["source_checkpoint"], 0)
        self.assertEqual(bundle["source_count"], 1)
        self.assertEqual(bundle["item_budget"], ITEM_BUDGET["self"])
        self.assertEqual(bundle["catalog_target"], 16)
        self.assertEqual(bundle["policy"]["catalog_pruning"], "off")
        self.assertEqual(bundle["observations"][0]["id"], first)
        self.assertEqual(bundle["observations"][0]["trigger_context"], "while testing")
        again = request_consolidation(self.store)
        self.assertEqual(again, bundle)

    def test_retained_v1_pending_request_still_applies(self) -> None:
        self._observe("legacy request lesson")
        bundle = request_consolidation(self.store)
        del bundle["request_version"]
        bundle["item_budget"] = 16
        binding = {key: bundle[key] for key in (
            "scope", "store_key", "base_generation", "source_count", "policy_digest", "supplied_digest"
        )}
        binding["migration"] = False
        bundle["request_id"] = canonical_digest(binding)[:24]
        atomic_write_json(live_layout(self.store).pending_dir / f"{bundle['request_id']}.json", bundle)
        generation = apply_consolidation(bundle["request_id"], self._result(
            bundle, items=[self._item("legacy request lesson", [0])]
        ))
        self.assertEqual(generation.items[0].text, "legacy request lesson")

    def test_status_validates_citations_and_distinguishes_unchanged_catalog(self) -> None:
        import io
        from contextlib import redirect_stdout
        from omem.cli import _status_current

        self._observe("stored lesson")
        first = request_consolidation(self.store)
        result = self._result(first, items=[self._item("stored lesson", [0])])
        apply_consolidation(first["request_id"], result)
        next_bundle = request_consolidation(self.store)
        apply_consolidation(next_bundle["request_id"], self._result(
            next_bundle, items=result["items"]
        ))
        output = io.StringIO()
        with redirect_stdout(output):
            _status_current("self", self.store)
        self.assertIn("catalog_outcome=unchanged", output.getvalue())
        raw = self.store.observations_path.read_bytes()
        self.store.observations_path.write_bytes(raw.replace(b"stored lesson", b"forged lesson"))
        output = io.StringIO()
        with redirect_stdout(output):
            _status_current("self", self.store)
        self.assertIn("current_generation=invalid:", output.getvalue())
        self.assertIn("no longer matches its digest", output.getvalue())

    def test_apply_promotes_defers_and_rejects_exactly_once(self) -> None:
        promoted = self._observe("durable lesson")
        deferred = self._observe("plausible but narrow")
        rejected = self._observe("status narration")
        bundle = request_consolidation(self.store)
        result = self._result(
            bundle,
            items=[self._item("durable lesson", [promoted])],
            deferred=[{"source_id": deferred, "reason": "needs recurrence"}],
            rejected=[{"source_id": rejected, "reason": "no decision value"}],
        )
        generation = apply_consolidation(bundle["request_id"], result)
        self.assertEqual(generation.source_count, 3)
        self.assertEqual(len(generation.items), 1)
        self.assertEqual(generation.items[0].text, "durable lesson")
        self.assertEqual(generation.items[0].source_ids, (promoted,))
        self.assertEqual(generation.deferred[0].source_id, deferred)
        self.assertEqual(generation.deferred[0].review_count, 1)
        self.assertEqual(generation.rejected[0].source_id, rejected)
        self.assertEqual(generation.citation_digests[promoted], generation.citation_digests[promoted])
        validate_generation_sources(self.store, generation)

        loaded = load_current_generation(self.store)
        self.assertEqual(loaded, generation)
        new, due = consolidation_due(self.store)
        self.assertEqual((new, due), (0, False))

    def test_every_offered_observation_needs_exactly_one_disposition(self) -> None:
        first, second = self._observe("one"), self._observe("two")
        bundle = request_consolidation(self.store)
        missing = self._result(bundle, items=[self._item("one", [first])])
        with self.assertRaisesRegex(ConsolidationError, "lack a disposition"):
            apply_consolidation(bundle["request_id"], missing)

        doubled = self._result(
            bundle,
            items=[self._item("one", [first])],
            deferred=[{"source_id": first, "reason": "also deferred"}],
            rejected=[{"source_id": second, "reason": "noise"}],
        )
        with self.assertRaisesRegex(ConsolidationError, "promoted and deferred"):
            apply_consolidation(bundle["request_id"], doubled)

        two_items = self._result(
            bundle,
            items=[self._item("one", [first]), self._item("one again", [first, second])],
        )
        with self.assertRaisesRegex(ConsolidationError, "supports two integrated items"):
            apply_consolidation(bundle["request_id"], two_items)

    def test_provenance_is_derived_from_citations(self) -> None:
        observed = self._observe("observed lesson")
        inferred = self.store.append_observation(
            "fact", "inferred", "inferred lesson", "while testing"
        ).id
        bundle = request_consolidation(self.store)
        wrong = self._result(
            bundle,
            items=[
                self._item("observed lesson", [observed], provenance="user"),
                self._item("inferred lesson", [inferred], provenance="inferred"),
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "provenance must be"):
            apply_consolidation(bundle["request_id"], wrong)
        mixed = self._result(
            bundle,
            items=[self._item("combined", [observed, inferred], provenance="mixed")],
        )
        generation = apply_consolidation(bundle["request_id"], mixed)
        self.assertEqual(generation.items[0].provenance, "mixed")

    def test_apply_is_idempotent_and_failure_is_logged(self) -> None:
        self._observe("lesson")
        bundle = request_consolidation(self.store)
        result = self._result(bundle, items=[self._item("lesson", [0])])
        first = apply_consolidation(bundle["request_id"], result)
        pointer = live_layout(self.store).current_path.read_bytes()
        second = apply_consolidation(bundle["request_id"], result)
        self.assertEqual(first, second)
        self.assertEqual(live_layout(self.store).current_path.read_bytes(), pointer)

        bad = self._result(bundle, source_count=99)
        with self.assertRaisesRegex(ConsolidationError, "source_count"):
            apply_consolidation(bundle["request_id"], bad)
        count, latest = consolidation_failure_summary(self.store)
        self.assertEqual(count, 1)
        self.assertIn("source_count", latest["reason"])
        self.assertNotIn("lesson", latest["reason"])

    def test_stale_base_and_policy_changes_preserve_the_pointer(self) -> None:
        self._observe("one")
        first_bundle = request_consolidation(self.store)
        apply_consolidation(
            first_bundle["request_id"],
            self._result(first_bundle, items=[self._item("one", [0])]),
        )
        self._observe("two")
        second_bundle = request_consolidation(self.store)
        self._observe("three")
        third_bundle = request_consolidation(self.store)
        apply_consolidation(
            third_bundle["request_id"],
            self._result(third_bundle, items=[self._item("one", [0])], rejected=[{"source_id": 1, "reason": "superseded"}, {"source_id": 2, "reason": "noise"}]),
        )
        pointer = live_layout(self.store).current_path.read_bytes()
        with self.assertRaisesRegex(ConsolidationError, "stale"):
            apply_consolidation(
                second_bundle["request_id"],
                self._result(second_bundle, items=[self._item("one", [0])], rejected=[{"source_id": 1, "reason": "late"}]),
            )
        self.assertEqual(live_layout(self.store).current_path.read_bytes(), pointer)

        fourth_bundle = request_consolidation(self.store)
        self.root.joinpath("config.toml").write_text(
            '[consolidation]\ncatalog_pruning = "balanced"\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ConsolidationError, "policy changed"):
            apply_consolidation(
                fourth_bundle["request_id"],
                self._result(fourth_bundle),
            )
        self.assertEqual(live_layout(self.store).current_path.read_bytes(), pointer)
        count, _ = consolidation_failure_summary(self.store)
        self.assertEqual(count, 2)

    def test_deferred_replay_does_not_change_generation_or_reviews(self) -> None:
        self._observe("awaiting corroboration")
        for review in range(2):
            bundle = request_consolidation(self.store)
            result = self._result(bundle, deferred=[{"source_id": 0, "reason": "needs context"}])
            with mock.patch("omem.consolidation.now", return_value=f"2026-09-19T00:00:0{review}+00:00"):
                first = apply_consolidation(bundle["request_id"], result)
            pointer = live_layout(self.store).current_path.read_bytes()
            with mock.patch("omem.consolidation.now", return_value="2026-09-20T00:00:00+00:00"):
                self.assertEqual(apply_consolidation(bundle["request_id"], result), first)
            self.assertEqual(first.deferred[0].review_count, review + 1)
            self.assertEqual(live_layout(self.store).current_path.read_bytes(), pointer)
            self.assertEqual(consolidation_failure_summary(self.store), (0, None))
        conflicting = self._result(bundle, deferred=[{"source_id": 0, "reason": "different judgment"}])
        with self.assertRaisesRegex(ConsolidationError, "conflicting result"):
            apply_consolidation(bundle["request_id"], conflicting)
        self.assertEqual(live_layout(self.store).current_path.read_bytes(), pointer)

    def test_deferred_only_request_is_actionable_and_stale_request_is_not(self) -> None:
        self._observe("awaiting corroboration")
        initial = request_consolidation(self.store)
        apply_consolidation(initial["request_id"], self._result(
            initial, deferred=[{"source_id": 0, "reason": "needs context"}]
        ))
        review = request_consolidation(self.store)
        self.assertEqual(pending_consolidation_counts(self.store), (1, 2))
        self._observe("new context")
        newer = request_consolidation(self.store)
        apply_consolidation(newer["request_id"], self._result(
            newer, items=[self._item("resolved lesson", [0, 1])]
        ))
        self.assertEqual(pending_consolidation_counts(self.store), (0, 3))

    def test_malformed_config_blocks_apply_but_reads_still_work(self) -> None:
        self._observe("one")
        bundle = request_consolidation(self.store)
        apply_consolidation(
            bundle["request_id"], self._result(bundle, items=[self._item("one", [0])])
        )
        config_path().write_text("not toml = [", encoding="utf-8")
        self._observe("two")
        with self.assertRaises(ConsolidationError):
            request_consolidation(self.store)
        loaded = load_current_generation(self.store)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.items[0].text, "one")

    def test_base_items_are_retained_unchanged_or_retired_with_reason(self) -> None:
        self._observe("one")
        first_bundle = request_consolidation(self.store)
        apply_consolidation(
            first_bundle["request_id"],
            self._result(first_bundle, items=[self._item("one", [0])]),
        )
        self._observe("two")
        bundle = request_consolidation(self.store)
        self.assertEqual(len(bundle["base_items"]), 1)
        omitted = self._result(
            bundle, rejected=[{"source_id": 1, "reason": "noise"}]
        )
        with self.assertRaisesRegex(ConsolidationError, "neither retained unchanged nor retired"):
            apply_consolidation(bundle["request_id"], omitted)

        retired = self._result(
            bundle,
            items=[self._item("two", [1])],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "safety",
                    "reason": "superseded by new evidence",
                    "evidence_source_ids": [1],
                }
            ],
        )
        generation = apply_consolidation(bundle["request_id"], retired)
        self.assertEqual([item.text for item in generation.items], ["two"])
        self.assertEqual(generation.retired[0].basis, "safety")
        validate_generation_sources(self.store, generation)

    def test_retirement_profile_gates(self) -> None:
        self._observe("one")
        first_bundle = request_consolidation(self.store)
        apply_consolidation(
            first_bundle["request_id"],
            self._result(first_bundle, items=[self._item("one", [0])]),
        )
        self._observe("two")
        bundle = request_consolidation(self.store)

        staleness_off = self._result(
            bundle,
            items=[self._item("two", [1])],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "staleness",
                    "reason": "narrow applicability and non-recurrence",
                    "evidence_source_ids": [],
                }
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "staleness retirement is disabled"):
            apply_consolidation(bundle["request_id"], staleness_off)

        capacity_off = self._result(
            bundle,
            items=[self._item("two", [1])],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "capacity",
                    "reason": "redundant",
                    "evidence_source_ids": [],
                }
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "capacity retirement is disabled"):
            apply_consolidation(bundle["request_id"], capacity_off)

        safety_without_evidence = self._result(
            bundle,
            items=[self._item("two", [1])],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "safety",
                    "reason": "misleading",
                    "evidence_source_ids": [],
                }
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "requires cited evidence"):
            apply_consolidation(bundle["request_id"], safety_without_evidence)

    def test_capacity_retirement_requires_pressure_under_a_profile(self) -> None:
        self.root.joinpath("config.toml").write_text(
            '[consolidation]\ncatalog_pruning = "conservative"\nself_catalog_target = 2\n',
            encoding="utf-8",
        )
        for text in ("alpha", "beta"):
            self._observe(text)
        first_bundle = request_consolidation(self.store)
        apply_consolidation(
            first_bundle["request_id"],
            self._result(
                first_bundle,
                items=[self._item("alpha", [0]), self._item("beta", [1])],
            ),
        )
        self._observe("gamma")
        bundle = request_consolidation(self.store)
        no_pressure = self._result(
            bundle,
            items=[self._item("beta", [1])],
            rejected=[{"source_id": 2, "reason": "kept backlog clean"}],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "capacity",
                    "reason": "redundant",
                    "evidence_source_ids": [],
                }
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "target pressure"):
            apply_consolidation(bundle["request_id"], no_pressure)

        with_pressure = self._result(
            bundle,
            items=[self._item("beta", [1]), self._item("gamma", [2])],
            retired=[
                {
                    "base_item_index": 0,
                    "basis": "capacity",
                    "reason": "redundant beside beta",
                    "evidence_source_ids": [],
                }
            ],
        )
        generation = apply_consolidation(bundle["request_id"], with_pressure)
        self.assertEqual([item.text for item in generation.items], ["beta", "gamma"])

    def test_deferred_review_history_is_derived_by_apply(self) -> None:
        first = self._observe("narrow lesson")
        bundle = request_consolidation(self.store)
        generation = apply_consolidation(
            bundle["request_id"],
            self._result(
                bundle, deferred=[{"source_id": first, "reason": "needs recurrence"}]
            ),
        )
        self.assertEqual(generation.deferred[0].review_count, 1)
        first_reviewed = generation.deferred[0].first_reviewed

        self._observe("another")
        second_bundle = request_consolidation(self.store)
        self.assertEqual(second_bundle["deferred"][0]["review_count"], 1)
        generation = apply_consolidation(
            second_bundle["request_id"],
            self._result(
                second_bundle,
                deferred=[{"source_id": first, "reason": "still narrow"}],
                rejected=[{"source_id": 1, "reason": "noise"}],
            ),
        )
        self.assertEqual(generation.deferred[0].review_count, 2)
        self.assertEqual(generation.deferred[0].first_reviewed, first_reviewed)

    def test_post_request_appends_remain_later_debt(self) -> None:
        self._observe("one")
        bundle = request_consolidation(self.store)
        self._observe("arrived during model work")
        generation = apply_consolidation(
            bundle["request_id"],
            self._result(bundle, items=[self._item("one", [0])]),
        )
        self.assertEqual(generation.source_count, 1)
        new, due = consolidation_due(self.store)
        self.assertEqual((new, due), (1, False))

    def test_requests_are_bounded_above_256_observations(self) -> None:
        total = MAX_OFFERED_OBSERVATIONS + 30
        for index in range(total):
            self._observe(f"observation {index}")
        bundle = request_consolidation(self.store)
        self.assertEqual(len(bundle["observations"]), MAX_OFFERED_OBSERVATIONS)
        self.assertEqual(bundle["source_count"], MAX_OFFERED_OBSERVATIONS)
        generation = apply_consolidation(
            bundle["request_id"],
            self._result(
                bundle,
                rejected=[
                    {"source_id": index, "reason": "bulk fixture noise"}
                    for index in range(MAX_OFFERED_OBSERVATIONS)
                ],
            ),
        )
        self.assertEqual(generation.source_count, MAX_OFFERED_OBSERVATIONS)
        new, _ = consolidation_due(self.store)
        self.assertEqual(new, 30)
        second_bundle = request_consolidation(self.store)
        self.assertEqual(len(second_bundle["observations"]), 30)

    def test_saturated_deferred_backlog_preserves_unoffered_records_and_advances(self) -> None:
        for index in range(MAX_OFFERED_OBSERVATIONS):
            self._observe(f"lesson {index}")
        initial = request_consolidation(self.store)
        before = apply_consolidation(initial["request_id"], self._result(
            initial, deferred=[{"source_id": index, "reason": "needs context"}
                               for index in range(MAX_OFFERED_OBSERVATIONS)]
        ))
        self._observe("new resolving correction", provenance="user")
        bundle = request_consolidation(self.store)
        self.assertEqual([item["id"] for item in bundle["observations"]], [256])
        self.assertEqual(len(bundle["deferred"]), 255)
        self.assertFalse(bundle["headroom_required"])
        reviewed = {entry["source_id"] for entry in bundle["deferred"]}
        result = self._result(bundle, deferred=[
            {"source_id": entry["source_id"], "reason": "still needs context"}
            for entry in bundle["deferred"]
        ] + [{"source_id": 256, "reason": "requires comparison"}])
        after = apply_consolidation(bundle["request_id"], result)
        self.assertEqual(after.source_count, 257)
        self.assertEqual(len(after.deferred), 257)
        carried = [record for record in before.deferred if record.source_id not in reviewed]
        self.assertEqual([record for record in after.deferred if record.source_id in {r.source_id for r in carried}], carried)
        self.assertEqual(apply_consolidation(bundle["request_id"], result), after)
        next_bundle = request_consolidation(self.store)
        self.assertEqual(len(next_bundle["deferred"]), 256)
        self.assertTrue({record.source_id for record in carried}.issubset(
            {entry["source_id"] for entry in next_bundle["deferred"]}
        ))

    def test_item_budget_is_a_hard_ceiling(self) -> None:
        for index in range(ITEM_BUDGET["self"] + 1):
            self._observe(f"lesson {index}")
        bundle = request_consolidation(self.store)
        over = self._result(
            bundle,
            items=[
                self._item(f"lesson {index}", [index])
                for index in range(ITEM_BUDGET["self"] + 1)
            ],
        )
        with self.assertRaisesRegex(ConsolidationError, "budget"):
            apply_consolidation(bundle["request_id"], over)

    def test_catalog_can_grow_past_wake_target_without_losing_old_rules(self) -> None:
        from omem.cli import _render_current_scope

        for index in range(20):
            self._observe(f"distinct lesson {index}")
        bundle = request_consolidation(self.store)
        generation = apply_consolidation(bundle["request_id"], self._result(
            bundle, items=[self._item(f"distinct lesson {index}", [index]) for index in range(20)]
        ))
        self.assertEqual(len(generation.items), 20)
        rendered = "\n".join(_render_current_scope("self", self.store))
        self.assertEqual(rendered.count("[self|integrated|"), 16)
        self.assertIn("distinct lesson 19", rendered)
        self.assertIn("omitted_integrated=4", rendered)
        self.assertEqual(generation.items[0].text, "distinct lesson 0")

    def test_wake_keeps_explicit_user_choices_ahead_of_newer_observed_facts(self) -> None:
        from omem.cli import _render_current_scope

        self._observe("Require consent before interrupting an active app", provenance="user")
        for index in range(20):
            self._observe(f"recent observation {index}")
        bundle = request_consolidation(self.store)
        apply_consolidation(bundle["request_id"], self._result(bundle, items=[
            self._item("Require consent before interrupting an active app", [0], provenance="user"),
            *(self._item(f"recent observation {index}", [index+1]) for index in range(20)),
        ]))
        rendered = "\n".join(_render_current_scope("self", self.store))
        self.assertIn("Require consent before interrupting an active app", rendered)
        self.assertEqual(rendered.count("[self|integrated|"), 16)

    def test_pending_counts_track_actionable_requests(self) -> None:
        self.assertEqual(pending_consolidation_counts(self.store), (0, 0))
        self._observe("one")
        request_consolidation(self.store)
        self.assertEqual(pending_consolidation_counts(self.store), (1, 1))

    def test_unknown_or_ambiguous_request_ids_fail(self) -> None:
        with self.assertRaisesRegex(ConsolidationError, "hexadecimal"):
            apply_consolidation("nope", {})
        with self.assertRaisesRegex(ConsolidationError, "unknown pending"):
            apply_consolidation("0" * 24, {})

    def test_failure_reasons_never_carry_candidate_text(self) -> None:
        self._observe("lesson")
        bundle = request_consolidation(self.store)
        oversized = self._result(
            bundle,
            items=[self._item("lesson " + "x" * 600, [0])],
        )
        with self.assertRaises(ConsolidationError):
            apply_consolidation(bundle["request_id"], oversized)
        _, latest = consolidation_failure_summary(self.store)
        self.assertIsNotNone(latest)
        self.assertNotIn("lesson", latest["reason"])
        self.assertNotIn("xxx", latest["reason"])
