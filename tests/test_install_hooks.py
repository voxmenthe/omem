"""scripts/install_hooks.py merges hook definitions into host settings exactly once."""

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_hooks.py"
CLAUDE_COMMANDS = {"memory hook claude", "hist hook claude SessionStart", "hist hook claude SubagentStart"}
CODEX_COMMANDS = {"memory codex-hook", "hist hook codex SessionStart", "hist hook codex SubagentStart"}


def commands(hooks: dict) -> list[str]:
    return [hook["command"] for groups in hooks.values() if isinstance(groups, list)
            for group in groups for hook in group["hooks"]]


class InstallHooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.claude = base / ".claude" / "settings.json"
        self.codex = base / ".codex" / "config.toml"
        self.claude.parent.mkdir()
        self.codex.parent.mkdir()
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith(("CODEX_", "CLAUDE_", "HIST_"))},
            "HOME": str(base), "CLAUDE_CONFIG_DIR": str(self.claude.parent), "CODEX_HOME": str(self.codex.parent),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def files(self) -> list[Path]:
        return sorted(self.claude.parent.iterdir()) + sorted(self.codex.parent.iterdir())

    def test_fresh_hosts_get_every_handler_once_and_reruns_change_nothing(self) -> None:
        self.assertEqual(0, self.install().returncode)
        self.assertEqual([], self.files(), "a dry run writes nothing")
        self.assertEqual(0, self.install("--apply").returncode)
        claude = json.loads(self.claude.read_text())
        self.assertEqual({"OMEM_HOST": "claude"}, claude["env"])
        self.assertEqual(sorted(["memory hook claude", *CLAUDE_COMMANDS]), sorted(commands(claude["hooks"])))
        codex = tomllib.loads(self.codex.read_text())
        self.assertEqual({"OMEM_HOST": "codex"}, codex["shell_environment_policy"]["set"])
        self.assertEqual(sorted(["memory codex-hook", *CODEX_COMMANDS]), sorted(commands(codex["hooks"])))
        written = {path: path.read_bytes() for path in self.files()}
        rerun = self.install("--apply")
        self.assertEqual(0, rerun.returncode)
        self.assertEqual(2, rerun.stdout.count("already installed"))
        self.assertEqual(written, {path: path.read_bytes() for path in self.files()})

    def test_existing_settings_survive_and_the_previous_file_is_kept(self) -> None:
        self.claude.write_text(json.dumps({
            "model": "opus", "env": {"FOO": "1"},
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]},
        }))
        original = (
            'model = "gpt"\n\n[shell_environment_policy.set]\nFOO = "1"\n\n'
            '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "other"\n\n'
            '[hooks.state."/x/config.toml:session_start:0:0"]\ntrusted_hash = "sha256:abc"\n'
        )
        self.codex.write_text(original)
        self.assertEqual(0, self.install("--apply").returncode)
        claude = json.loads(self.claude.read_text())
        self.assertEqual(("opus", {"FOO": "1", "OMEM_HOST": "claude"}), (claude["model"], claude["env"]))
        self.assertEqual("other", claude["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        codex = tomllib.loads(self.codex.read_text())
        self.assertEqual("gpt", codex["model"])
        self.assertEqual({"FOO": "1", "OMEM_HOST": "codex"}, codex["shell_environment_policy"]["set"])
        self.assertEqual("other", codex["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(4, len(codex["hooks"]["SessionStart"]) + len(codex["hooks"]["SubagentStart"]))
        self.assertEqual("sha256:abc", codex["hooks"]["state"]["/x/config.toml:session_start:0:0"]["trusted_hash"])
        backups = list(self.codex.parent.glob("config.toml.before-hooks-*"))
        self.assertEqual([original], [path.read_text() for path in backups])

    def test_settings_that_cannot_be_merged_exactly_are_left_untouched(self) -> None:
        cases = {
            "inline hooks table": (self.codex, "[hooks]\nSessionStart = []\n"),
            "conflicting host binding": (self.claude, json.dumps({"env": {"OMEM_HOST": "devin"}})),
        }
        for name, (path, text) in cases.items():
            with self.subTest(name):
                path.write_text(text)
                result = self.install("--apply")
                self.assertEqual(1, result.returncode)
                self.assertIn("nothing written", result.stdout)
                self.assertEqual(text, path.read_text())
                path.unlink()

    def test_one_tool_installs_only_its_own_handlers(self) -> None:
        self.assertEqual(0, self.install("hist", "--apply").returncode)
        claude = json.loads(self.claude.read_text())
        self.assertNotIn("env", claude)
        self.assertEqual(sorted(CLAUDE_COMMANDS - {"memory hook claude"}), sorted(commands(claude["hooks"])))
        codex = tomllib.loads(self.codex.read_text())
        self.assertEqual(sorted(CODEX_COMMANDS - {"memory codex-hook"}), sorted(commands(codex["hooks"])))


if __name__ == "__main__":
    unittest.main()
