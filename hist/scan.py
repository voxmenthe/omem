"""Directory listings cached by modification time.

A directory's mtime changes whenever an entry is added, removed, or renamed
in it, so an unchanged mtime means an unchanged listing. The mtime is read
before listing, so a change during the listing shows up next time. File
contents are checked separately, by stat, because appending to a file does
not touch its directory. Directory symlinks are listed as files, so walks
never follow them.
"""

import json
import os
import sqlite3


class CachedListing:
    """`listing(path) -> (subdirs, files) | None`, backed by `scan_dirs`."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._cache = {
            path: (mtime, json.loads(subdirs), json.loads(files))
            for path, mtime, subdirs, files in db.execute(
                "SELECT path, mtime_ns, subdirs, files FROM scan_dirs"
            )
        }
        self.changed: dict[str, tuple[int, list[str], list[str]] | None] = {}

    def __call__(self, path: str) -> tuple[list[str], list[str]] | None:
        key = path
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            if key in self._cache:
                self.changed[key] = None
            return None
        cached = self._cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1], cached[2]
        subdirs: list[str] = []
        files: list[str] = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    (subdirs if entry.is_dir(follow_symlinks=False) else files).append(
                        entry.name
                    )
        except OSError:
            return None
        subdirs.sort()
        files.sort()
        self._cache[key] = (mtime, subdirs, files)
        self.changed[key] = (mtime, subdirs, files)
        return subdirs, files

    def save(self, db: sqlite3.Connection) -> None:
        for path, value in self.changed.items():
            if value is None:
                db.execute("DELETE FROM scan_dirs WHERE path = ?", (path,))
            else:
                db.execute(
                    "INSERT OR REPLACE INTO scan_dirs (path, mtime_ns, subdirs, files) "
                    "VALUES (?, ?, ?, ?)",
                    (path, value[0], json.dumps(value[1]), json.dumps(value[2])),
                )
        self.changed.clear()
