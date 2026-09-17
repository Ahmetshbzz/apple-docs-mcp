"""Derived full-text index over Xcode's documentation corpus.

The corpus is a 1.2 GB SQLite database of JSON pages. Scanning it per query is
not viable, so this module derives a compact FTS5 index once and reuses it:

* the build streams the corpus and writes into a temporary file, then swaps it
  in atomically, so an interrupted build never leaves a half-index behind;
* freshness is decided from the corpus file's size and modification time plus
  the index schema version, which is cheap to check on every call;
* page text is not duplicated — the FTS table is contentless and full text is
  read back from the corpus for the handful of pages a query returns.

Measured on the reference machine: 263,513 pages indexed in ~7 s into a ~414 MB
index, queries answered in 19-88 ms with no subprocess involved.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apple_docs_mcp.corpus import DocumentationDBMissingError, connect

__all__ = [
    "INDEX_SCHEMA_VERSION",
    "IndexInfo",
    "build_index",
    "default_cache_dir",
    "ensure_index",
    "framework_sizes",
    "index_info",
    "index_path",
    "open_index",
]

INDEX_SCHEMA_VERSION: Final = 2

# FTS5 needs the identifier characters kept inside tokens: ``View.body`` and
# ``NSApplicationDelegate`` must not be split into pieces.
#
# The porter stemmer was tried here and rejected on measurement: it does close
# the inflection gap ("secret" reaches "secrets"), but it also merges terms into
# higher document frequencies, so selectivity filtering drops more of the query
# and both query sets scored worse — identifier recall@3 92.5% -> 90%, question
# recall@5 75% -> 58%.
_TOKENIZER: Final = "unicode61 remove_diacritics 2 tokenchars '_.:'"

BUILD_BATCH: Final = 4000

#: Two callers can reach a cold index at once (a warm-up thread and a first
#: search). Building writes a fixed temporary file, so only one build may run.
_BUILD_LOCK: Final = threading.Lock()

_FRAMEWORK_CACHE: Final[dict[tuple[str, int, int], dict[str, int]]] = {}
_FRAMEWORK_LOCK: Final = threading.Lock()


def default_cache_dir() -> Path:
    """Return the per-user cache directory for the derived index."""
    override = os.environ.get("APPLE_DOCS_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Caches" / "apple-docs-mcp"


def index_path(cache_dir: Path) -> Path:
    """Return the index file for a cache directory."""
    return cache_dir / f"index-v{INDEX_SCHEMA_VERSION}.sqlite"


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """State of the derived index relative to the corpus it was built from."""

    path: Path
    documents: int
    built_at: float
    source_path: str
    source_size: int
    source_mtime: float
    fresh: bool
    rebuilt: bool = False

    @property
    def usable(self) -> bool:
        return self.path.is_file() and self.documents > 0


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("select key, value from index_meta").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(key): str(value) for key, value in rows}


def index_info(db_path: Path | None, cache_dir: Path) -> IndexInfo | None:
    """Describe the index, or ``None`` when it has not been built yet."""
    path = index_path(cache_dir)
    if not path.is_file():
        return None

    try:
        connection = open_index(path)
    except sqlite3.DatabaseError:
        return None

    try:
        meta = _read_meta(connection)
    finally:
        connection.close()

    if not meta:
        return None

    documents = int(meta.get("documents", "0") or 0)
    source_size = int(meta.get("source_size", "0") or 0)
    source_mtime = float(meta.get("source_mtime", "0") or 0)
    built_at = float(meta.get("built_at", "0") or 0)
    source_path = meta.get("source_path", "")

    fresh = False
    if db_path is not None and db_path.is_file():
        stat = db_path.stat()
        fresh = (
            meta.get("schema_version") == str(INDEX_SCHEMA_VERSION)
            and source_path == str(db_path)
            and source_size == stat.st_size
            and abs(source_mtime - stat.st_mtime) < 1e-6
            and documents > 0
        )

    return IndexInfo(
        path=path,
        documents=documents,
        built_at=built_at,
        source_path=source_path,
        source_size=source_size,
        source_mtime=source_mtime,
        fresh=fresh,
    )


def framework_sizes(index_file: Path, connection: sqlite3.Connection) -> dict[str, int]:
    """Page counts per framework, cached per index file.

    A query that "how do I…" answers needs a prior on which framework a
    developer means: Foundation and UIKit hold thousands of pages, WalletOrders
    holds a few, and without that prior a symbol called ``Location`` in an
    unrelated framework outranks CoreLocation.
    """
    stat = index_file.stat()
    key = (str(index_file), stat.st_size, stat.st_mtime_ns)
    with _FRAMEWORK_LOCK:
        cached = _FRAMEWORK_CACHE.get(key)
    if cached is not None:
        return cached

    rows = connection.execute(
        "select framework, count(*) from docs group by framework"
    ).fetchall()
    sizes = {str(name): int(count) for name, count in rows if str(name)}
    with _FRAMEWORK_LOCK:
        _FRAMEWORK_CACHE.clear()
        _FRAMEWORK_CACHE[key] = sizes
    return sizes


def open_index(path: Path) -> sqlite3.Connection:
    """Open the derived index read-only with a memory-mapped page cache."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("pragma query_only = on")
    connection.execute("pragma mmap_size = 268435456")
    connection.execute("pragma cache_size = -65536")
    return connection


def build_index(db_path: Path, cache_dir: Path) -> IndexInfo:
    """Build the index from scratch and swap it in atomically."""
    if not db_path.is_file():
        raise DocumentationDBMissingError(
            f"The documentation corpus was not found at {db_path}. "
            "Download Developer Documentation from Xcode's Components settings."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = index_path(cache_dir)
    temporary = target.with_suffix(".building")
    temporary.unlink(missing_ok=True)

    stat = db_path.stat()
    documents = 0
    source = connect(db_path)
    derived = sqlite3.connect(temporary)
    try:
        derived.executescript(
            f"""
            pragma journal_mode = off;
            pragma synchronous = off;
            create table docs (
                id integer primary key,
                uri text not null,
                title text not null,
                framework text not null,
                kind text not null
            );
            create virtual table docs_fts using fts5(
                uri, title, framework, content,
                content='', tokenize="{_TOKENIZER}"
            );
            """
        )
        # Term statistics, so a query can be built from its most selective words
        # instead of OR-ing everything and scoring tens of thousands of pages.
        derived.executescript(
            """
            create virtual table docs_vocab using fts5vocab(docs_fts, 'row');
            """
        )

        batch: list[tuple[int, str, str, str, str, str]] = []
        next_id = 1
        for page in _iter_pages(db_path):
            batch.append((next_id, *page))
            next_id += 1
            if len(batch) >= BUILD_BATCH:
                _insert_batch(derived, batch)
                batch = []
        if batch:
            _insert_batch(derived, batch)
        documents = next_id - 1

        derived.executescript(
            "create table index_meta (key text primary key, value text not null);"
        )
        derived.executemany(
            "insert into index_meta values (?, ?)",
            [
                ("schema_version", str(INDEX_SCHEMA_VERSION)),
                ("source_path", str(db_path)),
                ("source_size", str(stat.st_size)),
                ("source_mtime", repr(stat.st_mtime)),
                ("documents", str(documents)),
                ("built_at", repr(time.time())),
            ],
        )
        derived.commit()
    finally:
        derived.close()
        source.close()

    if documents == 0:
        temporary.unlink(missing_ok=True)
        raise DocumentationDBMissingError(
            f"The documentation corpus at {db_path} holds no pages."
        )

    os.replace(temporary, target)
    _remove_stale_indexes(target)
    return IndexInfo(
        path=target,
        documents=documents,
        built_at=time.time(),
        source_path=str(db_path),
        source_size=stat.st_size,
        source_mtime=stat.st_mtime,
        fresh=True,
        rebuilt=True,
    )


def _remove_stale_indexes(current: Path) -> None:
    """Delete indexes built for an older schema; they are dead weight."""
    for candidate in current.parent.glob("index-v*.sqlite"):
        if candidate != current:
            candidate.unlink(missing_ok=True)


def _insert_batch(
    derived: sqlite3.Connection, batch: list[tuple[int, str, str, str, str, str]]
) -> None:
    derived.executemany(
        "insert into docs values (?, ?, ?, ?, ?)",
        [(row[0], row[1], row[2], row[3], row[4]) for row in batch],
    )
    derived.executemany(
        "insert into docs_fts(rowid, uri, title, framework, content) values (?, ?, ?, ?, ?)",
        [(row[0], row[1], row[2], row[3], row[5]) for row in batch],
    )


def _iter_pages(db_path: Path) -> Iterator[tuple[str, str, str, str, str]]:
    """Stream ``(uri, title, framework, kind, content)`` for every page."""
    connection = connect(db_path)
    try:
        cursor = connection.execute(
            """
            select json_extract(document,'$.uri'),
                   coalesce(json_extract(document,'$.title'), ''),
                   coalesce(json_extract(document,'$.framework'), ''),
                   coalesce(json_extract(document,'$.kind'), ''),
                   coalesce(json_extract(document,'$.content'), '')
            from documents
            where asset_id is not null
            """
        )
        for row in cursor:
            if not isinstance(row[0], str):
                continue
            yield (
                row[0],
                str(row[1] or ""),
                str(row[2] or ""),
                str(row[3] or ""),
                str(row[4] or ""),
            )
    finally:
        connection.close()


def ensure_index(
    db_path: Path | None, cache_dir: Path, *, force: bool = False
) -> IndexInfo:
    """Return a usable index, building or rebuilding it when necessary."""
    if db_path is None:
        raise DocumentationDBMissingError(
            "Apple Developer Documentation is not installed. Download it from "
            "Xcode's Settings > Components > Developer Documentation."
        )

    with _BUILD_LOCK:
        if not force:
            existing = index_info(db_path, cache_dir)
            if existing is not None and existing.fresh and existing.usable:
                return existing
        return build_index(db_path, cache_dir)
