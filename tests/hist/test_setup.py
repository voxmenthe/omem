"""A new user's setup: the example configuration and the instruction block."""

import shutil

from tests.hist.fixtures import REPO_ROOT, Claude, Codex, HistCase, write_jsonl

T1 = "019a0000-0000-7000-8000-00000000e0d1"
S2 = "3f2a0000-0000-4000-8000-0000000e1a02"


class NewUserTest(HistCase):
    def test_the_example_configuration_indexes_the_default_locations_unchanged(self) -> None:
        shutil.copy(REPO_ROOT / "hist" / "config.example.toml", self.home / "config.toml")
        cwd = str(self.repo)
        write_jsonl(Codex.path(self.codex, T1), [
            Codex.meta(T1, cwd, "cli", self.origin, minute=0),
            Codex.user("codex: where is the guard?", 1),
            Codex.assistant("in layout.py", 2),
        ])
        write_jsonl(Claude.project(self.claude, cwd) / f"{S2}.jsonl", [
            Claude.user("u1", S2, cwd, "claude: review the guard", 3, promptSource="typed"),
        ])
        home = {"HOME": str(self.base)}
        self.hist("sync", "--all", env=home)
        status = self.hist("status", env=home).stdout
        self.assertIn("codex (codex): 1 indexed", status)
        self.assertIn("claude (claude): 1 indexed", status)

    def test_instructions_print_the_packaged_block_before_any_configuration(self) -> None:
        (self.home / "config.toml").unlink()
        printed = self.hist("instructions").stdout
        self.assertEqual((REPO_ROOT / "hist" / "INSTRUCTIONS.md").read_text(encoding="utf-8"), printed)
