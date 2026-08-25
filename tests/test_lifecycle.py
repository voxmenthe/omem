"""Lifecycle corpus: candidate capture to integrated memory, driven by fixtures.

Each fixture case supplies the consolidation judgment (standing in for the
acting agent); the harness drives the real CLI and store, then checks the
agent-facing boundary: expected promotions surface, forbidden content never
does, and audit labels match each observation's disposition.
"""

import json
import tempfile
import unittest
from pathlib import Path

from .helpers import PROJECT, make_repo, run_memory

CORPUS = PROJECT / "fixtures" / "memory-lifecycle.json"


class LifecycleCorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "memory"
        self.repo = make_repo(
            self.base / "repo", "git@example.test:team/lifecycle.git"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, *args: str, stdin: str | None = None, ok: bool = True):
        result = run_memory(self.root, *args, cwd=self.repo, stdin=stdin)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _write_config(self, config: dict) -> None:
        lines = ["[consolidation]"]
        for key, value in config.items():
            rendered = json.dumps(value) if isinstance(value, str) else str(value)
            lines.append(f"{key} = {rendered}")
        (self.root / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _consolidate_round(self, scope: str, round_case: dict) -> None:
        request = self._run("consolidate", scope)
        bundle = json.loads(request.stdout)
        if "expect_offered" in round_case:
            self.assertEqual(
                len(bundle["observations"]), round_case["expect_offered"]
            )
        if "set_config_after_request" in round_case:
            self._write_config(round_case["set_config_after_request"])
        template = round_case["result"]
        result = {
            "scope": scope,
            "base_generation": bundle["base_generation"],
            "source_count": bundle["source_count"],
            "policy_digest": bundle["policy_digest"],
            "items": template.get("items", []),
            "deferred": template.get("deferred", []),
            "rejected": template.get("rejected", []),
            "retired": template.get("retired", []),
        }
        if "rejected_from" in template:
            result["rejected"] = [
                {"source_id": index, "reason": "bulk synthetic noise"}
                for index in range(template["rejected_from"], bundle["source_count"])
            ]
        applied = run_memory(
            self.root,
            "consolidate",
            "apply",
            bundle["request_id"],
            cwd=self.repo,
            stdin=json.dumps(result),
        )
        expect = round_case["expect"]
        if expect == "ok":
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("applied consolidation", applied.stdout)
        else:
            self.assertEqual(applied.returncode, 2, applied.stdout)
            self.assertIn(expect["error"], applied.stderr)

    def _check_expectations(self, case: dict) -> None:
        scope = case["scope"]
        expect = case["expect"]
        wake = self._run("wake").stdout
        for needle in expect.get("wake_contains", []):
            self.assertIn(needle, wake)
        forbidden = [
            *expect.get("wake_lacks", []),
            *expect.get("forbidden_surfaces", []),
        ]
        for needle in forbidden:
            self.assertNotIn(needle, wake)
        if "recall" in expect:
            recall = self._run("recall", scope, expect["recall"]["pattern"]).stdout
            count = int(recall.rsplit("matches=", 1)[1])
            self.assertEqual(count, expect["recall"]["count"])
            for needle in forbidden:
                self.assertNotIn(needle, recall)
        for orient_case in expect.get("orient", []):
            oriented = self._run("orient", orient_case["query"]).stdout
            if orient_case["expect_match"]:
                self.assertNotIn("abstained", oriented)
            else:
                self.assertIn("abstained", oriented)
                for needle in forbidden:
                    self.assertNotIn(needle, oriented)
        if "hook_lacks" in expect:
            hook = self._run(
                "codex-hook",
                stdin=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "ordinary work on the repository",
                        "cwd": str(self.repo),
                    }
                ),
            ).stdout
            for needle in expect["hook_lacks"]:
                self.assertNotIn(needle, hook)
        audit_labels = expect.get("audit")
        if audit_labels:
            audit = self._run("candidates", scope, ".").stdout
            for observation_id, disposition in audit_labels.items():
                marker = f"|{disposition}] #{observation_id} "
                self.assertIn(marker, audit)

    def _execute_case(self, case: dict) -> None:
        scope = case["scope"]
        self._run("init")
        if case.get("config"):
            self._write_config(case["config"])
        appended = 0
        total = len(case.get("observations", []))
        generated = case.get("generate_observations", 0)

        def append_through(count: int) -> None:
            nonlocal appended
            while appended < count:
                kind, provenance, text, context = case["observations"][appended]
                self._run(
                    "note",
                    f"{scope}:{kind}:{provenance}",
                    "--context",
                    context,
                    text,
                )
                appended += 1

        for round_case in case["rounds"]:
            through = round_case.get("only_through")
            append_through(total if through is None else through)
            if generated and appended == total:
                for index in range(generated - total):
                    self._run(
                        "note",
                        f"{scope}:fact:observed",
                        "--context",
                        "synthetic bulk context",
                        f"synthetic bulk noise {index}",
                    )
            self._consolidate_round(scope, round_case)
        self._check_expectations(case)

    def test_lifecycle_corpus(self) -> None:
        cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(len(cases), 16)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.tearDown()
                self.setUp()
                self._execute_case(case)
