"""The shared adapter conformance suite (plan section 5.10).

Every adapter passes the same checks through the `hist` command: idempotent
resync, a crash between batches, no injected text indexed, unknown records
counted, and a stop on a failed expectation. A new host adds one subclass.
"""

import unittest
from pathlib import Path
from typing import Any

from tests.hist.fixtures import (
    Antigravity, Claude, Codex, CommandCode, Devin, HistCase, OpenCode, protobuf, write_jsonl,
)

BIG = "y" * 4000


class Conformance(HistCase):
    """Subclasses supply transcripts; the checks are shared. Deleted below, so
    only the subclasses are collected."""

    def transcript(self, number: int) -> tuple[str, Path]:
        """(alias, path) for a new interactive session in the scratch repository."""
        raise NotImplementedError

    def opening(self, number: int) -> list[dict[str, Any]]:
        return []

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        raise NotImplementedError

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        raise NotImplementedError

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        """Host-injected records, each containing the text `INJECTED`."""
        raise NotImplementedError

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        """A record of a type the adapter has never seen."""
        raise NotImplementedError

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        """A record that breaks the adapter's expectations."""
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()
        self.hist("status")

    def write(self, number: int, records: list[dict[str, Any]], fresh: bool = True) -> tuple[str, Path]:
        """(alias, source path) after appending records; a host that keeps a
        database overrides this."""
        alias, path = self.transcript(number)
        write_jsonl(path, (self.opening(number) if fresh else []) + records)
        return alias, path

    def texts(self) -> list[tuple[int, str]]:
        return self.db().execute(
            "SELECT position, text FROM messages WHERE kind != 'event' ORDER BY position"
        ).fetchall()

    def test_resync_is_idempotent(self) -> None:
        self.write(1, [self.user(1, "first", 1), self.reply(1, "second", 2)])
        self.hist("sync")
        before = self.texts()
        self.assertEqual([text for _, text in before], ["first", "second"])
        self.hist("sync")
        self.assertEqual(self.texts(), before)
        self.write(1, [self.user(1, "third", 3)], fresh=False)
        self.hist("sync")
        self.assertEqual(self.texts(), before + [(before[-1][0] + 1, "third")])

    def test_a_crash_between_batches_resumes_exactly(self) -> None:
        # About 12 MB: three or more commits within one source.
        records = [self.user(1, f"{n} {BIG}", 1 + n // 100) for n in range(3000)]
        self.write(1, records)
        crashed = self.hist("sync", env={"HIST_FAULT": "commit:2"}, check=False)
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        partial = len(self.texts())
        self.assertTrue(0 < partial < 3000, partial)
        self.hist("sync")
        self.assertEqual([int(text.split()[0]) for _, text in self.texts()], list(range(3000)))

    def test_no_injected_text_is_indexed(self) -> None:
        self.write(1, [*self.injected(1, 1), self.user(1, "typed", 1), self.reply(1, "answer", 2)])
        self.hist("sync")
        self.assertEqual([text for _, text in self.texts()], ["typed", "answer"])
        self.assertNotStored(b"INJECTED")

    def test_unknown_records_are_counted_not_indexed(self) -> None:
        self.write(1, [self.user(1, "typed", 1), self.unknown(1, 1), self.reply(1, "answer", 2)])
        self.hist("sync")
        self.assertEqual([text for _, text in self.texts()], ["typed", "answer"])
        self.assertIn("unknown record types (counted, not indexed): ", self.hist("status").stdout)
        self.assertNotStored(b"UNKNOWN-TEXT")

    def test_a_lone_surrogate_is_stored_as_a_replacement_character(self) -> None:
        # JSON may escape half of an emoji (a truncated tool output, say);
        # SQLite cannot encode it, and one such record once stalled every sync.
        self.write(1, [self.user(1, "cut \ud83d here", 1), self.reply(1, "answer", 2)])
        self.hist("sync")
        self.assertEqual([text for _, text in self.texts()], ["cut � here", "answer"])

    def test_a_failed_expectation_stops_only_its_source(self) -> None:
        _, bad = self.write(1, [self.user(1, "before", 1), self.broken(1, 2), self.user(1, "after", 3)])
        self.write(2, [self.user(2, "other session", 4)])
        result = self.hist("sync")
        self.assertIn(f"stopped: {self.label}:{bad.name}", result.stdout)
        status = self.hist("status").stdout
        self.assertIn("adapter expectation failed", status)
        texts = [text for _, text in self.texts()]
        self.assertIn("other session", texts)
        self.assertNotIn("after", texts)


class CodexConformance(Conformance):
    label = "codex"

    def native(self, number: int) -> str:
        return f"019a0000-0000-7000-8000-00000000c{number:03d}"

    def transcript(self, number: int) -> tuple[str, Path]:
        native = self.native(number)
        return native[-8:], Codex.path(self.codex, native)

    def opening(self, number: int) -> list[dict[str, Any]]:
        return [Codex.meta(self.native(number), str(self.repo), "cli", self.origin)]

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return Codex.user(text, minute)

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return Codex.assistant(text, minute)

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        return [
            {**Codex.injected_user(minute)},
            Codex.legacy_injected_user(minute),
            Codex.developer("INJECTED developer", minute),
            Codex.user("<hist>\nINJECTED view\n</hist>", minute),
        ]

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        return Codex.item({"type": "future_item", "text": "UNKNOWN-TEXT"}, minute)

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        return Codex.item({"type": "message", "role": "user", "content": "not a list"}, minute)


class ClaudeConformance(Conformance):
    label = "claude"

    def native(self, number: int) -> str:
        return f"7d000000-0000-4000-8000-00000000c{number:03d}"

    def transcript(self, number: int) -> tuple[str, Path]:
        native = self.native(number)
        return native[-8:], Claude.project(self.claude, str(self.repo)) / f"{native}.jsonl"

    def _uuid(self) -> str:
        self._count = getattr(self, "_count", 0) + 1
        return f"u{self._count}"

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return Claude.user(self._uuid(), self.native(number), str(self.repo), text, minute)

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return Claude.assistant(self._uuid(), self.native(number), str(self.repo), [Claude.text(text)], minute)

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        native, cwd = self.native(number), str(self.repo)
        return [
            Claude.hook_context(self._uuid(), native, cwd, minute, "INJECTED hook"),
            Claude.user(self._uuid(), native, cwd, "<system-reminder>INJECTED reminder</system-reminder>", minute),
            Claude.user(self._uuid(), native, cwd, "INJECTED skill body", minute, isMeta=True),
            Claude.user(self._uuid(), native, cwd, "<hist>\nINJECTED view\n</hist>", minute),
        ]

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        return Claude.metadata("future-record", self.native(number), text="UNKNOWN-TEXT")

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        record = Claude.user(self._uuid(), self.native(number), str(self.repo), "x", minute)
        del record["message"]
        return record


class CommandCodeConformance(Conformance):
    label = "commandcode"

    def native(self, number: int) -> str:
        return f"cc000000-0000-4000-8000-00000000c{number:03d}"

    def transcript(self, number: int) -> tuple[str, Path]:
        native = self.native(number)
        path = CommandCode.path(self.commandcode, str(self.repo), native)
        CommandCode.write_meta(path)
        return native[-8:], path

    def opening(self, number: int) -> list[dict[str, Any]]:
        return [CommandCode.session(self.native(number), str(self.repo))]

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return CommandCode.user(text, minute)

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return CommandCode.assistant([{"type": "text", "text": text}], minute)

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        return [CommandCode.user("Please continue your response. INJECTED", minute, source="followup")]

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        return {"type": "future_record", "id": "f0", "timestamp": "x", "text": "UNKNOWN-TEXT"}

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        record = CommandCode.user("x", minute)
        del record["message"]["content"]
        return record


class OpenCodeConformance(Conformance):
    label = "opencode"

    def setUp(self) -> None:
        super().setUp()
        self.host = OpenCode(self.opencode)
        self.addCleanup(self.host.close)

    def native(self, number: int) -> str:
        return f"ses_conformance{number:08d}"

    def write(self, number: int, records: list[dict[str, Any]], fresh: bool = True) -> tuple[str, Path]:
        native = self.native(number)
        if fresh:
            self.host.session(native, str(self.repo))
        # A turn's rows become final when its `idle` row is written.
        self.host.add(native, records + [OpenCode.idle(records[-1]["minute"])])
        return native[-8:], self.host.source(native)

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return OpenCode.user(text, minute)

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return OpenCode.assistant([{"type": "text", "text": text}], minute)

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        return [
            OpenCode.row("synthetic", minute, text="INJECTED instructions from AGENTS.md"),
            OpenCode.row("system", minute, text="INJECTED date notice", description="d", metadata={}),
        ]

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        return OpenCode.row("future-row", minute, text="UNKNOWN-TEXT")

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        return OpenCode.row("user", minute, files=[])


class DevinConformance(Conformance):
    label = "devin"

    def setUp(self) -> None:
        super().setUp()
        self.host = Devin(self.devin)
        self.addCleanup(self.host.close)
        self.last: dict[str, int] = {}

    def native(self, number: int) -> str:
        return f"devin-conformance-{number:08d}"

    def write(self, number: int, records: list[dict[str, Any]], fresh: bool = True) -> tuple[str, Path]:
        native = self.native(number)
        if fresh:
            self.host.session(native, str(self.repo))
            self.host.add(native, [(0, None, self.host.prompt(0))])
            self.last[native] = 0
        first = self.last[native] + 1
        self.host.add(native, [(first + n, first + n - 1, record) for n, record in enumerate(records)])
        self.last[native] = first + len(records) - 1
        return native[-8:], self.host.source(native)

    def user(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return self.host.user(text, minute)

    def reply(self, number: int, text: str, minute: int) -> dict[str, Any]:
        return self.host.reply(text, minute)

    def injected(self, number: int, minute: int) -> list[dict[str, Any]]:
        return [
            self.host.message("user", "INJECTED keep-alive: continue", minute, source="user"),
            self.host.message("system", "<available_skills>INJECTED</available_skills>", minute),
        ]

    def unknown(self, number: int, minute: int) -> dict[str, Any]:
        return self.host.message("future", "UNKNOWN-TEXT", minute)

    def broken(self, number: int, minute: int) -> dict[str, Any]:
        return {**self.host.user("x", minute), "content": ["not", "text"]}


class AntigravityConformance(Conformance):
    label = "antigravity"

    def setUp(self) -> None:
        super().setUp()
        self.host = Antigravity(self.antigravity)
        self.addCleanup(self.host.close)

    def native(self, number: int) -> str:
        return f"a9000000-0000-4000-8000-00000000c{number:03d}"

    def write(self, number: int, records: list[Any], fresh: bool = True) -> tuple[str, Path]:
        native = self.native(number)
        if fresh:
            self.host.conversation(native, str(self.repo), self.origin)
            self.host.typed(native, "a prompt")
        self.host.add(native, records)
        return native[-8:], self.host.path(native)

    def user(self, number: int, text: str, minute: int) -> Any:
        return Antigravity.user(text, minute)

    def reply(self, number: int, text: str, minute: int) -> Any:
        return Antigravity.reply(text, minute)

    def injected(self, number: int, minute: int) -> list[Any]:
        return [
            Antigravity.step(101, minute, (114, protobuf((1, "INJECTED system message")))),
            Antigravity.step(90, minute, (103, protobuf((1, "INJECTED ephemeral message")))),
            Antigravity.step(98, minute, (111, protobuf((1, "INJECTED conversation history")))),
            Antigravity.reply("", minute, thinking="INJECTED thinking"),
        ]

    def unknown(self, number: int, minute: int) -> Any:
        return Antigravity.step(8, minute, (14, protobuf((1, "UNKNOWN-TEXT"))))

    def broken(self, number: int, minute: int) -> Any:
        return 14, Antigravity.DONE, b"\x0f\x00"

    def test_a_lone_surrogate_is_stored_as_a_replacement_character(self) -> None:
        # Protobuf strings are bytes: an encoded lone surrogate is invalid
        # UTF-8, and each of its bytes becomes a replacement character.
        self.write(1, [self.user(1, "cut \ud83d here", 1), self.reply(1, "answer", 2)])
        self.hist("sync")
        self.assertEqual([text for _, text in self.texts()], ["cut \ufffd\ufffd\ufffd here", "answer"])


del Conformance

if __name__ == "__main__":
    unittest.main()
