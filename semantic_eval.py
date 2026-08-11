#!/usr/bin/env python3
"""Score a chronological, nap, or dream projection on the semantic fixture."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "semantic-memory.json"


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def score(projection: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic retention, citation, and safety measurements."""

    items = projection.get("items")
    if not isinstance(items, list):
        raise ValueError("projection.items must be a JSON array")
    budget = projection.get("item_budget")
    if type(budget) is not int or budget < 1:
        raise ValueError("projection.item_budget must be a positive integer")
    source_ids = {note["id"] for note in fixture["notes"]}
    normalized: list[dict[str, Any]] = []
    invalid_citations = 0
    uncited = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"projection item {index} must be an object")
        text = item.get("text")
        standing = item.get("standing")
        citations = item.get("source_ids")
        if not isinstance(text, str) or standing not in ("current", "uncertain"):
            raise ValueError(
                f"projection item {index} needs text and current/uncertain standing"
            )
        if not isinstance(citations, list) or not citations:
            uncited += 1
            citations = []
        invalid_citations += sum(
            type(source_id) is not int or source_id not in source_ids
            for source_id in citations
        )
        normalized.append(
            {"text": text, "standing": standing, "source_ids": set(citations)}
        )

    claims: list[dict[str, Any]] = []
    retained = 0
    unsafe = 0
    for expectation in fixture["expectations"]:
        matches = [
            item
            for item in normalized
            if re.search(expectation["pattern"], item["text"], re.IGNORECASE)
        ]
        expected_standing = expectation["standing"]
        if expected_standing == "must_not_current":
            passed = not any(item["standing"] == "current" for item in matches)
            unsafe += 0 if passed else 1
        else:
            supported = set(expectation["support_ids"])
            passed = any(
                item["standing"] == expected_standing
                and bool(item["source_ids"] & supported)
                for item in matches
            )
            retained += int(passed)
        forbidden = expectation.get("must_not_current")
        if forbidden and any(
            item["standing"] == "current"
            and re.search(forbidden, item["text"], re.IGNORECASE)
            for item in normalized
        ):
            passed = False
            unsafe += 1
        claims.append({"claim": expectation["claim"], "passed": passed})

    retained_total = sum(
        expectation["standing"] != "must_not_current"
        for expectation in fixture["expectations"]
    )
    return {
        "projection": projection.get("projection", "unknown"),
        "age_profile": projection.get("age_profile", "unspecified"),
        "item_budget": budget,
        "item_count": len(items),
        "within_budget": len(items) <= budget,
        "retained_current_claims": retained,
        "retained_current_claims_total": retained_total,
        "unsafe_current_claims": unsafe,
        "uncited_items": uncited,
        "invalid_citations": invalid_citations,
        "claims": claims,
        "manual_review": {
            "invention": (
                "Inspect whether cited sources semantically support each item; "
                "citation range alone cannot detect invention."
            ),
            "uncertainty": (
                "Inspect whether unresolved contradictions are labeled uncertain."
            ),
            "omission_cost": (
                "Assess operational impact of each failed expected claim."
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("projection", type=Path)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    arguments = parser.parse_args(argv)
    try:
        report = score(
            _load_object(arguments.projection), _load_object(arguments.fixture)
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
