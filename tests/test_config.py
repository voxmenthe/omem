"""Root configuration and storage-routing contract tests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omem.config import (
    DEFAULT_POLICY,
    ConfigError,
    config_path,
    consolidation_refusal,
    fetch_policy,
    load_consolidation_trust,
    load_storage_mode,
    memory_root,
    parse_host,
)
from omem.layout import StoreContext, resolve_store_context
from omem.models import ConsolidationError


class ConfigParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.env = {"MEMORY_V0_DIR": str(self.root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        config_path(self.env).write_text(text, encoding="utf-8")

    def test_absent_file_defaults_to_pooled_without_creating_anything(self) -> None:
        mode = load_storage_mode(self.env)
        self.assertEqual(mode, "pooled")
        self.assertFalse(self.root.exists())

    def test_absent_storage_section_and_empty_section_mean_pooled(self) -> None:
        for text in (
            "[consolidation]\ncatalog_pruning = \"balanced\"\n",
            "[storage]\n[consolidation]\n",
            "[storage]\nmode = \"pooled\"\n",
        ):
            with self.subTest(text=text):
                self._write(text)
                self.assertEqual(load_storage_mode(self.env), "pooled")

    def test_per_host_exact_value_routes_per_host(self) -> None:
        self._write("[storage]\nmode = \"per-host\"\n")
        self.assertEqual(load_storage_mode(self.env), "per-host")

    def test_sections_parse_in_either_textual_order(self) -> None:
        self._write(
            "[consolidation]\nself_catalog_target = 8\n\n[storage]\n"
            "mode = \"per-host\"\n"
        )
        self.assertEqual(load_storage_mode(self.env), "per-host")
        policy = fetch_policy(self.env)
        self.assertEqual(policy.self_catalog_target, 8)

    def test_existing_consolidation_only_files_keep_policy_and_digest(self) -> None:
        text = (
            "[consolidation]\n"
            "catalog_pruning = \"balanced\"\n"
            "self_catalog_target = 8\n"
            "repo_catalog_target = 20\n"
        )
        self._write(text)
        policy = fetch_policy(self.env)
        self.assertEqual(policy.catalog_pruning, "balanced")
        from omem.generations import policy_digest

        self.assertNotEqual(policy_digest(policy), policy_digest(DEFAULT_POLICY))

    def test_unknown_sections_keys_and_types_fail_clearly(self) -> None:
        cases = (
            "not toml = [",
            '[other]\nmode = "pooled"\n',
            '[storage]\nmode = "pooled"\nextra = 1\n',
            '[storage]\nmode = "shared"\n',
            '[storage]\nmode = 3\n',
            "[storage]\nmode = true\n",
        )
        for text in cases:
            with self.subTest(text=text):
                self._write(text)
                with self.assertRaises(ConfigError):
                    load_storage_mode(self.env)

    def test_malformed_toml_blocks_storage_routing(self) -> None:
        self._write("not toml = [")
        with self.assertRaises(ConfigError):
            load_storage_mode(self.env)

    def test_consolidation_semantic_errors_stay_independent_of_routing(self) -> None:
        self._write('[storage]\nmode = "pooled"\n\n[consolidation]\ncatalog_pruning = "ruthless"\n')
        self.assertEqual(load_storage_mode(self.env), "pooled")
        with self.assertRaises(ConsolidationError):
            fetch_policy(self.env)

    def test_storage_error_is_config_error_not_consolidation_error(self) -> None:
        self._write('[storage]\nmode = "shared"\n')
        with self.assertRaises(ConfigError):
            load_storage_mode(self.env)

    def test_no_config_read_creates_a_root_or_file(self) -> None:
        load_storage_mode(self.env)
        fetch_policy(self.env)
        resolve_store_context(None, self.env)
        self.assertFalse(self.root.exists())


class HostSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _env(self, **overrides: str) -> dict[str, str]:
        env = {"MEMORY_V0_DIR": str(self.root)}
        env.update(overrides)
        return env

    def test_host_slugs_are_open_but_path_safe(self) -> None:
        for value in ("codex", "claude", "devin", "kimi", "grok", "command-code", "pi"):
            with self.subTest(value=value):
                self.assertEqual(parse_host(value), value)
        bad_values = (
            "",
            "CODEX",
            "Claude",
            "co dex",
            "codex/",
            "..\\codex",
            "../codex",
            ".",
            "-codex",
            "x" * 33,
            3,
            None,
        )
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_host(value)

    def test_pooled_mode_permits_no_host_and_both_hosts(self) -> None:
        none_ctx = resolve_store_context(None, self._env())
        self.assertIsNone(none_ctx.host)
        self.assertEqual(none_ctx.store_root, self.root)
        codex_ctx = resolve_store_context("codex", self._env())
        self.assertEqual(codex_ctx.host, "codex")
        self.assertEqual(codex_ctx.store_root, self.root)
        claude_env_ctx = resolve_store_context(None, self._env(OMEM_HOST="claude"))
        self.assertEqual(claude_env_ctx.host, "claude")
        self.assertEqual(claude_env_ctx.store_root, self.root)

    def test_explicit_and_environment_agreement_is_accepted(self) -> None:
        ctx = resolve_store_context("claude", self._env(OMEM_HOST="claude"))
        self.assertEqual(ctx.host, "claude")

    def test_conflicting_hosts_fail_before_store_access(self) -> None:
        with self.assertRaises(Exception) as caught:
            resolve_store_context("claude", self._env(OMEM_HOST="codex"))
        self.assertFalse(self.root.exists())

    def test_invalid_environment_host_fails_even_in_pooled_mode(self) -> None:
        with self.assertRaises(Exception):
            resolve_store_context(None, self._env(OMEM_HOST="Typo Host"))
        self.assertFalse(self.root.exists())

    def test_per_host_mode_requires_a_host(self) -> None:
        (self.root / "config.toml").parent.mkdir(parents=True)
        (self.root / "config.toml").write_text(
            '[storage]\nmode = "per-host"\n', encoding="utf-8"
        )
        with self.assertRaises(Exception):
            resolve_store_context(None, self._env())
        codex_ctx = resolve_store_context("codex", self._env())
        self.assertEqual(codex_ctx.store_root, self.root / "hosts" / "codex")
        claude_ctx = resolve_store_context(None, self._env(OMEM_HOST="claude"))
        self.assertEqual(claude_ctx.store_root, self.root / "hosts" / "claude")


class ConsolidationTrustTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        self.env = {"MEMORY_V0_DIR": str(self.root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        config_path(self.env).write_text(text, encoding="utf-8")

    def test_absent_file_section_or_key_means_unrestricted(self) -> None:
        self.assertIsNone(load_consolidation_trust(self.env))
        for text in ('[storage]\nmode = "pooled"\n', "[hosts]\n"):
            with self.subTest(text=text):
                self._write(text)
                self.assertIsNone(load_consolidation_trust(self.env))

    def test_present_list_is_exact_including_empty(self) -> None:
        self._write('[hosts]\nconsolidation_trusted = ["kimi", "codex"]\n')
        self.assertEqual(load_consolidation_trust(self.env), frozenset({"kimi", "codex"}))
        self._write("[hosts]\nconsolidation_trusted = []\n")
        self.assertEqual(load_consolidation_trust(self.env), frozenset())

    def test_malformed_hosts_fail_trust_only(self) -> None:
        for text in (
            "hosts = 1\n",
            '[hosts]\nconsolidation_trusted = "kimi"\n',
            '[hosts]\nconsolidation_trusted = ["Kimi"]\n',
            '[hosts]\nconsolidation_trusted = [1]\n',
            "[hosts]\nother = 1\n",
        ):
            with self.subTest(text=text):
                self._write(text)
                with self.assertRaises(ConfigError):
                    load_consolidation_trust(self.env)
                self.assertEqual(load_storage_mode(self.env), "pooled")
                self.assertEqual(fetch_policy(self.env), DEFAULT_POLICY)

    def test_refusal_names_the_host_and_the_trusted_set(self) -> None:
        self.assertIsNone(consolidation_refusal(None, None))
        self.assertIsNone(consolidation_refusal("grok", None))
        self.assertIsNone(consolidation_refusal("kimi", frozenset({"kimi"})))
        unbound = consolidation_refusal(None, frozenset({"kimi", "codex"}))
        self.assertIn("requires a trusted host", unbound)
        self.assertIn("trusted: codex, kimi", unbound)
        self.assertIn("OMEM_HOST", unbound)
        untrusted = consolidation_refusal("grok", frozenset({"kimi"}))
        self.assertIn("host grok is not trusted to consolidate", untrusted)
        nobody = consolidation_refusal("kimi", frozenset())
        self.assertIn("trusted: none", nobody)


if __name__ == "__main__":
    unittest.main()
