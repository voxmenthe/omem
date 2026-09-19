"""Regression tests for the installed package contract."""

import unittest
from importlib import resources
from importlib.metadata import distribution

from omem import __version__
from omem.hook_context import CONTEXT_LIMIT_BYTES

from .helpers import PROJECT


class PackagingTest(unittest.TestCase):
    def test_memory_entry_point_targets_packaged_omem_cli(self) -> None:
        installed = distribution("scoped-omem")
        self.assertEqual(installed.version, __version__)
        memory_entry_points = [
            entry_point
            for entry_point in installed.entry_points
            if entry_point.group == "console_scripts"
            and entry_point.name == "memory"
        ]

        self.assertEqual(1, len(memory_entry_points))
        self.assertEqual("omem.cli:main", memory_entry_points[0].value)
        self.assertTrue(callable(memory_entry_points[0].load()))
        self.assertIn(
            "omem/cli.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/host_hook.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/hook_context.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/config.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertNotIn(
            "omem/codex_hook.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/orientation.py",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/INSTRUCTIONS.md",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/SESSION_REVIEW.md",
            {str(path) for path in installed.files or ()},
        )
        self.assertEqual(
            ["LICENSE"],
            installed.metadata.get_all("License-File"),
        )
        self.assertEqual(
            "Apache-2.0",
            installed.metadata["License-Expression"],
        )
        self.assertIn(
            "LICENSE",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "Apache License",
            (PROJECT / "LICENSE").read_text(encoding="utf-8"),
        )
        instructions = resources.files("omem").joinpath("INSTRUCTIONS.md")
        self.assertIn("## Memory Store", instructions.read_text(encoding="utf-8"))
        self.assertEqual(
            (PROJECT / "INSTRUCTIONS.md").read_bytes(),
            instructions.read_bytes(),
        )
        reviewer = resources.files("omem").joinpath("SESSION_REVIEW.md")
        self.assertIn(
            "## Post-hoc Session Review",
            reviewer.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (PROJECT / "SESSION_REVIEW.md").read_bytes(),
            reviewer.read_bytes(),
        )
        self.assertTrue(
            (PROJECT / "omem" / "INSTRUCTIONS.md").samefile(
                PROJECT / "INSTRUCTIONS.md"
            )
        )
        self.assertTrue(
            (PROJECT / "omem" / "SESSION_REVIEW.md").samefile(
                PROJECT / "SESSION_REVIEW.md"
            )
        )

    def test_tracked_codex_hooks_keep_two_read_only_routes(self) -> None:
        import tomllib

        config = tomllib.loads(
            (PROJECT / "integrations" / "codex" / "hooks.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["shell_environment_policy"]["set"],
            {"OMEM_HOST": "codex"},
        )
        hooks = config["hooks"]
        self.assertEqual(set(hooks), {"UserPromptSubmit", "SessionStart"})
        prompt_routes = hooks["UserPromptSubmit"]
        self.assertEqual(len(prompt_routes), 1)
        self.assertEqual(
            prompt_routes[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": "memory codex-hook",
                    "timeout": 3,
                    "statusMessage": "Loading memory turn context",
                    "additionalContextLimit": CONTEXT_LIMIT_BYTES,
                }
            ],
        )
        session_routes = hooks["SessionStart"]
        self.assertEqual(len(session_routes), 1)
        self.assertEqual(session_routes[0]["matcher"], "^compact$")
        self.assertEqual(
            session_routes[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": "memory codex-hook",
                    "timeout": 3,
                    "statusMessage": "Loading memory recovery checkpoint",
                    "additionalContextLimit": 800,
                }
            ],
        )

    def test_tracked_claude_settings_bind_host_and_two_routes(self) -> None:
        import json

        settings = json.loads(
            (PROJECT / "integrations" / "claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(settings.get("env"), {"OMEM_HOST": "claude"})
        hooks = settings["hooks"]
        self.assertEqual(set(hooks), {"UserPromptSubmit", "SessionStart"})
        prompt_routes = hooks["UserPromptSubmit"]
        self.assertEqual(len(prompt_routes), 1)
        self.assertEqual(
            prompt_routes[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": "memory hook claude",
                    "timeout": 3,
                    "statusMessage": "Loading memory turn context",
                }
            ],
        )
        session_routes = hooks["SessionStart"]
        self.assertEqual(len(session_routes), 1)
        self.assertEqual(session_routes[0]["matcher"], "compact")
        self.assertEqual(
            session_routes[0]["hooks"],
            [
                {
                    "type": "command",
                    "command": "memory hook claude",
                    "timeout": 3,
                    "statusMessage": "Loading memory recovery checkpoint",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
