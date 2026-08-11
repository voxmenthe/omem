"""Regression tests for the installed package contract."""

import unittest
from importlib import resources
from importlib.metadata import distribution

from .helpers import PROJECT


class PackagingTest(unittest.TestCase):
    def test_memory_entry_point_targets_packaged_omem_cli(self) -> None:
        installed = distribution("scoped-omem")
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
            "omem/INSTRUCTIONS.md",
            {str(path) for path in installed.files or ()},
        )
        self.assertIn(
            "omem/SESSION_REVIEW.md",
            {str(path) for path in installed.files or ()},
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


if __name__ == "__main__":
    unittest.main()
