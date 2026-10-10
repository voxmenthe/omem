"""The shared append-only reader: progress, boundaries, and recovery checks."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hist import reader
from hist.reader import Cursor, SourceMismatch, UnsupportedFile


class ReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "t.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def append(self, data: bytes, path: Path | None = None) -> None:
        with (path or self.path).open("ab") as handle:
            handle.write(data)

    def consume(self, path: Path | None = None, cursor: Cursor = Cursor()) -> tuple[list[bytes], Cursor]:
        path = path or self.path
        cursor, _ = reader.check(path, cursor)
        out: list[bytes] = []
        while True:
            records, cursor = reader.read(path, cursor, max_bytes=8)
            if not records:
                return out, cursor
            out += [record.data for record in records]

    def test_a_trailing_partial_line_waits_until_complete(self) -> None:
        self.append(b'{"a":1}\n{"b":')
        lines, cursor = self.consume()
        self.assertEqual(lines, [b'{"a":1}'])
        self.append(b'2}\n')
        more, cursor = self.consume(cursor=cursor)
        self.assertEqual(more, [b'{"b":2}'])
        self.assertEqual(cursor.records, 2)

    def test_batches_are_bounded_and_resume_at_any_record(self) -> None:
        self.append(b"".join(b"r%d\n" % n for n in range(10)) + b"x" * 50 + b"\n")
        records, _ = reader.read(self.path, Cursor(), max_bytes=7)
        self.assertEqual([r.data for r in records], [b"r0", b"r1", b"r2"])
        records, end = reader.read(self.path, records[1].after, max_bytes=10_000)
        self.assertEqual(records[0].data, b"r2")
        self.assertEqual(records[-1].data, b"x" * 50)
        big, _ = reader.read(self.path, records[-2].after, max_bytes=1)
        self.assertEqual([r.data for r in big], [b"x" * 50])
        self.assertEqual(end.records, 11)

    def test_appends_pass_the_cheap_check_without_rehashing(self) -> None:
        self.append(b"one\ntwo\n")
        _, cursor = self.consume()
        self.append(b"three\n")
        with mock.patch.object(reader, "verify", side_effect=AssertionError("re-hashed")):
            same, verified = reader.check(self.path, cursor)
        self.assertEqual((same, verified), (cursor, False))

    def test_truncation_and_rewrites_of_checked_records_stop_the_source(self) -> None:
        cases = {
            "truncated": lambda data: data[:-4],
            "first record changed": lambda data: b"ONE" + data[3:],
            "last consumed record changed or moved": lambda data: data.replace(b"two", b"TWO"),
            # Same length overall, but the last record moved.
            "last consumed record changed or moved ": lambda data: data.replace(b"mid\ntwo", b"midt\nwo"),
        }
        for reason, rewrite in cases.items():
            with self.subTest(reason=reason):
                self.path.write_bytes(b"one\nmid\ntwo\n")
                _, cursor = self.consume()
                original = self.path.read_bytes()
                self.path.write_bytes(rewrite(original))
                self.assertTrue(os.stat(self.path).st_ino == cursor.inode)
                with self.assertRaisesRegex(SourceMismatch, reason.strip()):
                    reader.check(self.path, cursor)
                self.path.unlink()

    def test_a_middle_rewrite_needs_verify(self) -> None:
        self.append(b"one\nmid\ntwo\n")
        _, cursor = self.consume()
        self.path.write_bytes(b"one\nMID\ntwo\n")
        self.assertEqual(reader.check(self.path, cursor), (cursor, False))
        with self.assertRaisesRegex(SourceMismatch, "consumed prefix changed"):
            reader.verify(self.path, cursor)

    def test_a_moved_file_is_fully_verified_and_re_anchored(self) -> None:
        self.append(b"one\nmid\ntwo\n")
        _, cursor = self.consume()
        moved = self.dir / "archived.jsonl"
        self.path.rename(moved)
        self.append(b"three\n", moved)
        anchored, verified = reader.check(moved, cursor)
        self.assertTrue(verified)
        self.assertEqual((anchored.path, anchored.offset, anchored.records), (str(moved), 12, 3))
        self.assertEqual(reader.read(moved, anchored)[0][0].data, b"three")
        copy = self.dir / "copy.jsonl"
        copy.write_bytes(b"one\nMID\ntwo\n")
        with self.assertRaisesRegex(SourceMismatch, "consumed prefix changed"):
            reader.check(copy, cursor)
        short = self.dir / "short.jsonl"
        short.write_bytes(b"one\nmid\n")
        with self.assertRaisesRegex(SourceMismatch, "only 2 of 3 consumed records remain"):
            reader.check(short, cursor)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
    def test_a_compressed_file_continues_where_the_plain_one_stopped(self) -> None:
        self.append(b"one\ntwo\n")
        _, cursor = self.consume()
        self.append(b"three\n")
        subprocess.run(["zstd", "-q", "--rm", str(self.path)], check=True)
        compressed = self.dir / "t.jsonl.zst"
        lines, end = self.consume(compressed, cursor)
        self.assertEqual(lines, [b"three"])
        self.assertEqual(end.records, 3)

    def test_without_zstd_a_compressed_file_is_unsupported_not_skipped(self) -> None:
        compressed = self.dir / "t.jsonl.zst"
        compressed.write_bytes(b"\x28\xb5\x2f\xfd")
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaises(UnsupportedFile):
                reader.read(compressed, Cursor())


if __name__ == "__main__":
    unittest.main()
