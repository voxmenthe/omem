"""Executable contracts for bounded prompt-conditioned orientation."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omem.dreams import apply_dream, request_dream
from omem.layout import current_repo, scope_path
from omem.models import DreamArtifact
from omem.orientation import (
    EVIDENCE_WARNING,
    OrientationRequest,
    _Candidate,
    _render_items,
    _safe_current_dream,
    _select_candidates,
    fetch_orientation,
)
from omem.store import MemoryStore

from .helpers import make_repo


FIXTURES = Path(__file__).parent / "fixtures" / "orientation_cases.json"


class SelectorFixtureTest(unittest.TestCase):
    def test_exact_selector_and_rendering_fixtures(self) -> None:
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        for case in cases:
            candidates = tuple(
                _Candidate(
                    scope=value["scope"],
                    source=value["source"],
                    source_kind=value["source_kind"],
                    kind=value["kind"],
                    provenance=value["provenance"],
                    claim=value["claim"],
                    raw_ids=tuple(value["raw_ids"]),
                    recency=value["recency"],
                    standing=value.get("standing"),
                )
                for value in case["candidates"]
            )
            with self.subTest(case=case["name"]):
                selected = _select_candidates(case["query"], candidates, max_items=3)
                rendered = _render_items(selected, max_bytes=4096)
                self.assertEqual(rendered, case["expected_rendered"])

    def test_query_bounds_abstain_before_candidate_scoring(self) -> None:
        candidate = _Candidate(
            scope="repo",
            source="repo:raw:0",
            source_kind="raw",
            kind="fact",
            provenance="observed",
            claim="oversized-token would otherwise match",
            raw_ids=(0,),
            recency=0,
        )
        self.assertEqual(
            _select_candidates("oversized-token " * 257, (candidate,), max_items=3),
            (),
        )
        self.assertEqual(
            _select_candidates("x" * 16_385, (candidate,), max_items=3),
            (),
        )

    def test_rendering_never_emits_a_partial_record(self) -> None:
        candidate = _Candidate(
            scope="repo",
            source="repo:raw:0",
            source_kind="raw",
            kind="fact",
            provenance="observed",
            claim="bounded-output",
            raw_ids=(0,),
            recency=0,
        )
        selected = _select_candidates("bounded-output", (candidate,), max_items=3)
        self.assertEqual(_render_items(selected, max_bytes=1), "")

    def test_multibyte_budget_counts_utf8_and_never_emits_warning_alone(self) -> None:
        candidate = _Candidate(
            scope="repo",
            source="repo:raw:0",
            source_kind="raw",
            kind="fact",
            provenance="observed",
            claim="café-path uses 界界界",
            raw_ids=(0,),
            recency=0,
        )
        selected = _select_candidates("café-path", (candidate,), max_items=3)
        rendered = _render_items(selected, max_bytes=4096)
        encoded_bytes = len(rendered.encode("utf-8"))

        self.assertTrue(rendered)
        self.assertEqual(_render_items(selected, max_bytes=encoded_bytes), rendered)
        self.assertEqual(_render_items(selected, max_bytes=encoded_bytes - 1), "")
        self.assertEqual(
            _render_items(
                selected,
                max_bytes=len(EVIDENCE_WARNING.encode("utf-8")),
            ),
            "",
        )


class OrientationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "memory"
        self.repo = make_repo(
            self.base / "repo", "git@example.test:team/orientation.git"
        )
        self.environment = mock.patch.dict(
            os.environ, {"MEMORY_V0_DIR": str(self.root)}
        )
        self.environment.start()
        self.self_store = MemoryStore(scope_path("self"), "self")
        self.self_store.initialize()
        identity = current_repo(self.repo)
        self.assertIsNotNone(identity)
        self.repo_store = MemoryStore(scope_path("repo", identity), "repo")
        self.repo_store.initialize()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def _request(self, query: str, *, max_bytes: int = 4096) -> OrientationRequest:
        return OrientationRequest(
            query=query,
            cwd=self.repo,
            max_evidence_bytes=max_bytes,
            max_items=3,
            deadline=time.monotonic() + 1,
        )

    def _manifest(self) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def test_fetch_merges_matching_self_and_repo_evidence_without_writes(self) -> None:
        self.self_store.append("preference", "user", "Prefer bounded changes")
        self.repo_store.append(
            "procedure", "observed", "release-procedure uses bounded changes"
        )
        before = self._manifest()

        result = fetch_orientation(
            self._request(
                "PRIVATE-QUERY-SENTINEL prefer bounded changes release-procedure"
            )
        )

        self.assertEqual(
            [item.source for item in result.items],
            ["self:raw:0", "repo:raw:0"],
        )
        self.assertIsNone(result.abstention_reason)
        self.assertEqual(result.rendered.count("\n"), 2)
        self.assertNotIn("PRIVATE-QUERY-SENTINEL", result.rendered)
        self.assertEqual(self._manifest(), before)

    def test_fetch_uses_only_a_source_valid_current_dream(self) -> None:
        self.repo_store.append("procedure", "observed", "retry-limit is 3")
        self.repo_store.append("procedure", "observed", "retry-limit is 4")
        bundle = request_dream(self.repo_store)
        apply_dream(
            bundle["dream_id"],
            {
                "version": 1,
                "scope": "repo",
                "source_count": 2,
                "items": [
                    {
                        "kind": "procedure",
                        "standing": "current",
                        "provenance": "observed",
                        "text": "retry-limit is 4",
                        "source_ids": [0, 1],
                    }
                ],
            },
        )

        result = fetch_orientation(self._request("retry-limit"))

        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.items[0].source.startswith("repo:dream:"))
        self.assertEqual(result.items[0].claim, "retry-limit is 4")

    def test_invalid_dream_digest_cannot_supply_standing(self) -> None:
        self.repo_store.append("procedure", "observed", "retry-limit is 3")
        self.repo_store.append("procedure", "observed", "retry-limit is 4")
        bundle = request_dream(self.repo_store)
        apply_dream(
            bundle["dream_id"],
            {
                "version": 1,
                "scope": "repo",
                "source_count": 2,
                "items": [
                    {
                        "kind": "procedure",
                        "standing": "current",
                        "provenance": "observed",
                        "text": "retry-limit is 4",
                        "source_ids": [0, 1],
                    }
                ],
            },
        )
        raw = self.repo_store.log_path.read_bytes()
        self.repo_store.log_path.write_bytes(raw.replace(b"retry-limit is 3", b"retry-limit is 8"))

        result = fetch_orientation(self._request("retry-limit"))

        self.assertEqual(result.items, ())
        self.assertEqual(result.abstention_reason, "no_match")

    def test_oversized_dream_metadata_is_rejected_before_raw_collection(self) -> None:
        artifact = DreamArtifact(
            dream_id="0" * 24,
            scope="repo",
            source_count=257,
            source_digest="0" * 64,
            created_at="2026-08-10T00:00:00+00:00",
            items=(),
        )
        with mock.patch("omem.orientation.load_current", return_value=artifact):
            self.assertIsNone(_safe_current_dream(self.repo_store))

    def test_one_broken_scope_does_not_block_the_other(self) -> None:
        self.self_store.append("preference", "user", "Prefer bounded changes")
        self.repo_store.append("procedure", "observed", "bounded changes procedure")
        self.repo_store.log_path.write_bytes(b"malformed" + b" " * 311)

        result = fetch_orientation(self._request("prefer bounded changes"))

        self.assertEqual([item.source for item in result.items], ["self:raw:0"])

    def test_history_above_the_supported_ceiling_abstains_before_scanning(self) -> None:
        self.repo_store.append("fact", "observed", "ceiling-marker first")
        self.repo_store.append("fact", "observed", "ceiling-marker second")
        with mock.patch("omem.orientation.AUTOMATIC_RECORD_CEILING", 1):
            result = fetch_orientation(self._request("ceiling-marker"))
        self.assertEqual(result.items, ())
        self.assertEqual(result.abstention_reason, "history_above_ceiling")

    def test_query_and_output_bounds_have_fixed_query_free_reasons(self) -> None:
        sentinel = "PRIVATE-SENTINEL"
        oversized = fetch_orientation(self._request(sentinel + (" x" * 257)))
        self.assertEqual(oversized.abstention_reason, "query_too_large")
        self.assertNotIn(sentinel, oversized.abstention_reason)

        self.repo_store.append("fact", "observed", "budget-marker is present")
        budgeted = fetch_orientation(self._request("budget-marker", max_bytes=1))
        self.assertEqual(budgeted.abstention_reason, "output_budget")
        self.assertEqual(budgeted.rendered, "")
