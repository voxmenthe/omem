#!/usr/bin/env python3
"""Compare Release A selector fixtures with the static-checkpoint baseline."""

from __future__ import annotations

import json
from pathlib import Path

from omem.orientation import _Candidate, _render_items, _select_candidates

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "orientation_cases.json"


def main() -> int:
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    results = []
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
        rendered = _render_items(
            _select_candidates(case["query"], candidates, max_items=3),
            max_bytes=4096,
        )
        expected = case["expected_rendered"]
        positive = bool(expected)
        passed = rendered == expected
        results.append(
            {
                "name": case["name"],
                "expected_retrieval": positive,
                "orientation_retrieved": bool(rendered),
                "static_checkpoint_retrieved": False,
                "exact_match": passed,
            }
        )

    positive = [result for result in results if result["expected_retrieval"]]
    negative = [result for result in results if not result["expected_retrieval"]]
    report = {
        "schema": 1,
        "trial_count": len(results),
        "positive_trials": len(positive),
        "negative_trials": len(negative),
        "orientation_retrieval_wins": sum(
            result["orientation_retrieved"] and result["exact_match"]
            for result in positive
        ),
        "static_retrieval_wins": 0,
        "negative_abstention_ties": sum(
            not result["orientation_retrieved"] and result["exact_match"]
            for result in negative
        ),
        "orientation_false_positives": sum(
            result["orientation_retrieved"] for result in negative
        ),
        "exact_contract_failures": sum(not result["exact_match"] for result in results),
        "results": results,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["exact_contract_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
