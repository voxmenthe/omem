#!/usr/bin/env python3
"""Measure Release A orientation latency on structurally valid synthetic stores."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from omem import orientation
from omem.dreams import apply_dream, request_dream
from omem.layout import current_repo, scope_path
from omem.orientation import (
    OrientationRequest,
    _query_evidence,
    _render_complete_items,
    _safe_current_dream,
    _scan_captured_prefix,
    _select_candidates,
    fetch_orientation,
)
from omem.store import LOG_RECORD_BYTES, MemoryStore, _pad

QUERY = "target-marker"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_records(store: MemoryStore, start: int, stop: int) -> None:
    batch = bytearray()
    with store.log_path.open("ab") as handle:
        for note_id in range(start, stop):
            marker = " target-marker" if note_id == 0 else ""
            line = (
                f"#{note_id} 2026-08-10 [fact|observed] "
                f"synthetic record {note_id}{marker}"
            )
            batch.extend(_pad(line, LOG_RECORD_BYTES))
            if len(batch) >= LOG_RECORD_BYTES * 1024:
                handle.write(batch)
                batch.clear()
        if batch:
            handle.write(batch)
        handle.flush()
        os.fsync(handle.fileno())


def _install_valid_dream(store: MemoryStore) -> None:
    bundle = request_dream(store)
    apply_dream(
        bundle["dream_id"],
        {
            "version": 1,
            "scope": store.scope,
            "source_count": bundle["source_count"],
            "items": [
                {
                    "kind": "fact",
                    "standing": "current",
                    "provenance": "observed",
                    "text": "target-marker is retained for benchmark selection",
                    "source_ids": [0],
                }
            ],
        },
    )


def _prepare(records: int, with_dream: bool) -> tuple[tempfile.TemporaryDirectory, Path, list[MemoryStore], str | None]:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    root = base / "memory"
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@example.test:bench/orientation.git")
    prior_root = os.environ.get("MEMORY_V0_DIR")
    os.environ["MEMORY_V0_DIR"] = str(root)
    identity = current_repo(repo)
    if identity is None:
        raise RuntimeError("benchmark repository identity was not resolved")
    stores = [
        MemoryStore(scope_path("self"), "self"),
        MemoryStore(scope_path("repo", identity), "repo"),
    ]
    for store in stores:
        store.initialize()
        checkpoint = min(records, 256) if with_dream else 0
        if checkpoint:
            _write_records(store, 0, checkpoint)
            _install_valid_dream(store)
        _write_records(store, checkpoint, records)
    return temporary, repo, stores, prior_root


def _restore_root(prior_root: str | None) -> None:
    if prior_root is None:
        os.environ.pop("MEMORY_V0_DIR", None)
    else:
        os.environ["MEMORY_V0_DIR"] = prior_root


def _trial(repo: Path, stores: list[MemoryStore]) -> dict[str, Any]:
    query = _query_evidence(QUERY)
    if query is None:
        raise RuntimeError("benchmark query unexpectedly exceeded selector bounds")

    # Measure the integrated request first so the cold proxy is genuinely the
    # first orientation pass after fixture construction. Phase timings follow;
    # warm scenarios have already completed at least one integrated request.
    started = time.perf_counter()
    result = fetch_orientation(
        OrientationRequest(
            query=QUERY,
            cwd=repo,
            max_evidence_bytes=4096,
            max_items=3,
            deadline=time.monotonic() + 0.500,
        )
    )
    total_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    resolved = current_repo(repo, deadline=time.monotonic() + 2)
    repo_ms = (time.perf_counter() - started) * 1000
    if resolved is None:
        raise RuntimeError("benchmark repository resolution failed")

    deadline = time.monotonic() + 5
    captures = []
    started = time.perf_counter()
    for store in stores:
        captures.append(
            store.capture_prefix(
                deadline=deadline,
                metadata_loader=lambda selected=store: _safe_current_dream(selected),
            )
        )
    capture_ms = (time.perf_counter() - started) * 1000

    candidates = []
    started = time.perf_counter()
    with ExitStack() as stack:
        for store, capture in zip(stores, captures, strict=True):
            stack.enter_context(capture)
            candidates.extend(
                _scan_captured_prefix(
                    store,
                    capture,
                    query,
                    deadline=deadline,
                    clock=time.monotonic,
                )
            )
    selected = _select_candidates(QUERY, candidates, max_items=3)
    scan_selection_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    rendered, _ = _render_complete_items(selected, max_bytes=4096)
    encoding_ms = (time.perf_counter() - started) * 1000

    return {
        "repo_resolution_ms": repo_ms,
        "capture_ms": capture_ms,
        "scan_selection_ms": scan_selection_ms,
        "encoding_ms": encoding_ms,
        "total_ms": total_ms,
        "total_reason": result.abstention_reason,
        "rendered_bytes": len(rendered.encode("utf-8")),
    }


def _percentiles(samples: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field in (
        "repo_resolution_ms",
        "capture_ms",
        "scan_selection_ms",
        "encoding_ms",
        "total_ms",
    ):
        values = sorted(float(sample[field]) for sample in samples)
        p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
        output[field] = {
            "p50": round(statistics.median(values), 3),
            "p95": round(values[p95_index], 3),
            "max": round(values[-1], 3),
        }
    output["deadline_misses"] = sum(
        sample["total_reason"] == "deadline" for sample in samples
    )
    output["reasons"] = sorted({sample["total_reason"] for sample in samples}, key=str)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--sizes", type=int, nargs="+", default=(1_000, 10_000, 100_000))
    args = parser.parse_args()
    if args.runs < 1 or any(size < 1 for size in args.sizes):
        parser.error("runs and sizes must be positive")

    orientation.AUTOMATIC_RECORD_CEILING = max(args.sizes)
    scenarios = []
    for records in args.sizes:
        for with_dream in (False, True):
            print(
                f"benchmark records_per_scope={records} dream={with_dream}",
                file=sys.stderr,
                flush=True,
            )
            temporary, repo, stores, prior_root = _prepare(records, with_dream)
            try:
                cold_proxy = _trial(repo, stores)
                warm = [_trial(repo, stores) for _ in range(args.runs)]
            finally:
                _restore_root(prior_root)
                temporary.cleanup()
            scenarios.append(
                {
                    "records_per_scope": records,
                    "valid_dream": with_dream,
                    "cold_first_pass_proxy": {
                        key: round(value, 3) if isinstance(value, float) else value
                        for key, value in cold_proxy.items()
                    },
                    "warm_runs": args.runs,
                    "warm": _percentiles(warm),
                }
            )

    print(
        json.dumps(
            {
                "schema": 1,
                "query": QUERY,
                "cold_definition": (
                    "total_ms is the first integrated pass after fixture construction; "
                    "phase fields follow on the same fixture; OS cache was not purged"
                ),
                "automatic_deadline_ms": 500,
                "scenarios": scenarios,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
