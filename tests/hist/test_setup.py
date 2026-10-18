"""A new user's setup: the example configuration and the instruction block."""

import shutil

from tests.hist.fixtures import (
    REPO_ROOT,
    Antigravity,
    Claude,
    Codex,
    CommandCode,
    Devin,
    HistCase,
    OpenCode,
    write_jsonl,
)

T1 = "019a0000-0000-7000-8000-00000000e0d1"
S2 = "3f2a0000-0000-4000-8000-0000000e1a02"
C3 = "cc000000-0000-4000-8000-0000000e1a03"
A4 = "a9000000-0000-4000-8000-0000000e1a04"


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
        commandcode = CommandCode.path(self.commandcode, cwd, C3)
        CommandCode.write_meta(commandcode)
        write_jsonl(commandcode, [CommandCode.session(C3, cwd, 4), CommandCode.user("commandcode: and now?", 4)])
        opencode, devin, antigravity = OpenCode(self.opencode), Devin(self.devin), Antigravity(self.antigravity)
        for host in (opencode, devin, antigravity):
            self.addCleanup(host.close)
        opencode.session("ses_newuser0000e1a5", cwd)
        opencode.add("ses_newuser0000e1a5", [OpenCode.user("opencode: and now?", 5), OpenCode.idle(5)])
        devin.session("devin-newuser-e1a6", cwd)
        devin.add("devin-newuser-e1a6", [(0, None, devin.prompt(6)), (1, 0, devin.user("devin: and now?", 6))])
        antigravity.conversation(A4, cwd)
        antigravity.add(A4, [Antigravity.user("antigravity: and now?", 7)])
        antigravity.finish(A4)
        home = {"HOME": str(self.base)}
        self.hist("sync", "--all", env=home)
        status = self.hist("status", env=home).stdout
        for label in ("codex", "claude", "commandcode", "opencode", "devin", "antigravity"):
            self.assertIn(f"{label} ({label}): 1 indexed", status)

    def test_instructions_print_the_packaged_block_before_any_configuration(self) -> None:
        (self.home / "config.toml").unlink()
        printed = self.hist("instructions").stdout
        self.assertEqual((REPO_ROOT / "hist" / "INSTRUCTIONS.md").read_text(encoding="utf-8"), printed)
