"""Reuse read-only SQLite connections across calls.

Opening a connection per call throws away SQLite's page cache, so every search
re-reads the same index pages: measured 20-50 ms per call against 5 ms once the
cache survives. The corpus and the derived index are read-only, which is what
makes reuse safe.

Two details keep reuse honest:

* the cache is thread-local, because handlers run in worker threads and a
  ``sqlite3.Connection`` is not shared lightly;
* a cached connection is kept only while the file it points at is unchanged. A
  rebuilt index is a new file, and reading the old one through a stale handle
  would answer from data that no longer exists.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

__all__ = ["shared"]

_CACHE: Final = threading.local()


def _per_thread() -> dict[str, tuple[tuple[int, int], sqlite3.Connection]]:
    cache = getattr(_CACHE, "entries", None)
    if cache is None:
        cache = {}
        _CACHE.entries = cache
    return cache


def shared(path: Path, opener: Callable[[Path], sqlite3.Connection]) -> sqlite3.Connection:
    """Return a reusable connection to ``path``, reopening it if the file changed."""
    try:
        stat = path.stat()
    except OSError:
        # Let the opener raise its own typed error for a missing file.
        return opener(path)

    signature = (stat.st_mtime_ns, stat.st_size)
    entries = _per_thread()
    entry = entries.get(str(path))
    if entry is not None:
        previous, connection = entry
        if previous == signature:
            return connection
        with contextlib.suppress(sqlite3.Error):
            connection.close()

    connection = opener(path)
    entries[str(path)] = (signature, connection)
    return connection


def forget(path: Path | None = None) -> None:
    """Drop cached connections for this thread (used by tests and after a rebuild)."""
    entries = _per_thread()
    for key in [str(path)] if path is not None else list(entries):
        entry = entries.pop(key, None)
        if entry is not None:
            with contextlib.suppress(sqlite3.Error):
                entry[1].close()
