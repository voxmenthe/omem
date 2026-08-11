"""Tests for the reproducible semantic evaluation fixture."""

import json
import unittest
from pathlib import Path

from semantic_eval import score

PROJECT = Path(__file__).resolve().parents[1]


class SemanticEvaluationTest(unittest.TestCase):
    def test_example_retains_expected_claims_without_unsafe_imperative(self) -> None:
        fixture = json.loads(
            (PROJECT / "fixtures" / "semantic-memory.json").read_text(encoding="utf-8")
        )
        projection = json.loads(
            (PROJECT / "fixtures" / "semantic-projection-example.json").read_text(
                encoding="utf-8"
            )
        )
        report = score(projection, fixture)
        self.assertTrue(report["within_budget"])
        self.assertEqual(report["retained_current_claims"], 5)
        self.assertEqual(report["unsafe_current_claims"], 0)
        self.assertEqual(report["uncited_items"], 0)
        self.assertEqual(report["invalid_citations"], 0)
