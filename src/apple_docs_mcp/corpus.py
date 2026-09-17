"""Read the documentation corpus from Xcode's on-disk SQLite store.

Xcode's Developer Documentation asset keeps every page as a JSON document in a
SQLite database (``documentation-db/index.sql``). The prose is therefore
readable in place, with no Xcode process involved and no bridge approval: the
corpus is a file, and a file needs neither a running IDE nor a 24-hour grant.

Each stored document carries the page title, the framework it belongs to, the
document kind, the full prose with its code listings, the symbol identity, and
the availability windows — everything a caller needs to answer an API question
without guessing from memory.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from apple_docs_mcp.connections import shared

__all__ = [
    "ASSET_ROOT",
    "DB_RELATIVE_PATH",
    "PAGE_CACHE_CHARACTERS",
    "DocumentRecord",
    "DocumentationDBMissingError",
    "count_documents",
    "extract_declaration",
    "find_documentation_db",
    "get_document",
    "get_documents",
    "iter_documents",
    "list_framework_names",
]

ASSET_ROOT: Final = Path(
    "/System/Library/AssetsV2/com_apple_MobileAsset_AppleDeveloperDocumentation"
)
DB_RELATIVE_PATH: Final = Path("documentation-db") / "index.sql"
DB_BUNDLE_GLOB: Final = "*.asset/AssetData/" + DB_RELATIVE_PATH.as_posix()

#: Page text is cached up to this many characters (~a few thousand pages).
PAGE_CACHE_CHARACTERS: Final = 4_000_000

_SYMBOL_KIND: Final = "symbol"
_DECLARATION_PATTERN: Final = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

_SELECT_COLUMNS: Final = """
    json_extract(document,'$.uri'),
    json_extract(document,'$.title'),
    json_extract(document,'$.framework'),
    json_extract(document,'$.kind'),
    json_extract(document,'$.role'),
    json_extract(document,'$.parentUri'),
    json_extract(document,'$.content'),
    json_extract(document,'$.symbol'),
    json_extract(document,'$.platforms')
"""


class DocumentationDBMissingError(RuntimeError):
    """The documentation corpus database is not installed or not readable."""


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One documentation page as stored on disk."""

    uri: str
    title: str
    framework: str
    kind: str
    role: str
    parent_uri: str
    content: str
    symbol: str
    platforms: str

    @property
    def declaration(self) -> str | None:
        """The declaration code fence of a symbol page, when it has one."""
        if self.kind != _SYMBOL_KIND:
            return None
        return extract_declaration(self.content)

    @property
    def availability(self) -> list[str]:
        """Availability windows rendered as ``["iOS 17+", ...]``."""
        return format_availability(self.platforms)

    @property
    def symbol_kind(self) -> str | None:
        kind = _load_json_object(self.symbol).get("kind")
        return kind if isinstance(kind, str) else None

    @property
    def precise_identifier(self) -> str | None:
        identifier = _load_json_object(self.symbol).get("preciseIdentifier")
        return identifier if isinstance(identifier, str) else None


def extract_declaration(content: str) -> str | None:
    """Return the first fenced code block in ``content``, stripped."""
    match = _DECLARATION_PATTERN.search(content)
    if match is None:
        return None
    declaration = match.group(1).strip()
    return declaration or None


def format_availability(platforms: str) -> list[str]:
    """Render stored platform windows as readable strings."""
    rendered: list[str] = []
    for entry in _load_json_object(platforms).get("__list__", []):
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        if not isinstance(platform, str):
            continue
        introduced = entry.get("introduced")
        if introduced is None:
            continue
        label = f"{platform} {introduced}+"
        if entry.get("deprecated") is True:
            label = f"{label} (deprecated)"
        rendered.append(label)
    return rendered


def _load_json_object(raw: str) -> dict[str, Any]:
    """Parse a stored JSON column, tolerating ``None`` and malformed values.

    ``platforms`` is a JSON array, so arrays are parked under ``__list__`` and
    object keys are returned as-is.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"__list__": parsed}
    return {}


def find_documentation_db(search_root: Path = ASSET_ROOT) -> Path | None:
    """Return the documentation corpus database, or ``None`` when absent.

    The installed layout nests the payload under a content-addressed bundle::

        <root>/<uuid>.asset/AssetData/documentation-db/index.sql

    A flat root that holds the database directly is also accepted, which keeps
    synthetic fixtures and future layout changes working.
    """
    if not search_root.is_dir():
        return None

    direct = search_root / DB_RELATIVE_PATH
    if direct.is_file():
        return direct

    for candidate in sorted(search_root.glob(DB_BUNDLE_GLOB)):
        if candidate.is_file():
            return candidate
    return None


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the corpus read-only, immutable, and without touching journal files.

    The corpus lives on a system-managed volume that may be replaced underneath
    us, so the connection must never attempt a write or a recovery pass.
    """
    if not db_path.is_file():
        raise DocumentationDBMissingError(
            f"The documentation corpus was not found at {db_path}. "
            "Download Developer Documentation from Xcode's Components settings."
        )
    try:
        connection = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        _require_documents_table(connection, db_path)
    except sqlite3.DatabaseError as error:
        raise DocumentationDBMissingError(
            f"Could not read the documentation corpus at {db_path}: {error}"
        ) from error
    return connection


def _require_documents_table(connection: sqlite3.Connection, db_path: Path) -> None:
    row = connection.execute(
        "select count(*) from sqlite_master where type = 'table' and name = 'documents'"
    ).fetchone()
    if not row or not row[0]:
        raise DocumentationDBMissingError(
            f"{db_path} is not a documentation corpus: no documents table."
        )


def _record(row: sqlite3.Row | tuple[Any, ...]) -> DocumentRecord:
    values = ["" if value is None else value for value in row]
    return DocumentRecord(
        uri=str(values[0]),
        title=str(values[1]),
        framework=str(values[2]),
        kind=str(values[3]),
        role=str(values[4]),
        parent_uri=str(values[5]),
        content=str(values[6]) if values[6] is not None else "",
        symbol=str(values[7]) if values[7] is not None else "",
        platforms=str(values[8]) if values[8] is not None else "",
    )


def iter_documents(db_path: Path) -> Iterator[DocumentRecord]:
    """Stream every page in the corpus without materialising it in memory."""
    connection = connect(db_path)
    try:
        cursor = connection.execute(
            f"select {_SELECT_COLUMNS} from documents where asset_id is not null"
        )
        for row in cursor:
            if not isinstance(row[0], str):
                continue
            yield _record(row)
    finally:
        connection.close()


def count_documents(db_path: Path) -> int:
    """Return the number of pages in the corpus."""
    connection = connect(db_path)
    try:
        row = connection.execute("select count(*) from documents").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def list_framework_names(db_path: Path) -> list[str]:
    """Return every framework named by the corpus.

    Used when the derived index does not exist yet, so listing frameworks never
    costs a build.
    """
    connection = connect(db_path)
    try:
        rows = connection.execute(
            "select distinct json_extract(document,'$.framework') from documents "
            "where asset_id is not null"
        ).fetchall()
    finally:
        connection.close()
    return sorted({str(row[0]) for row in rows if row[0]})


def get_document(db_path: Path, uri: str) -> DocumentRecord | None:
    """Return one page by its documentation URI."""
    return get_documents(db_path, [uri]).get(uri)


def get_documents(db_path: Path, uris: list[str]) -> dict[str, DocumentRecord]:
    """Return several pages in one round trip, keyed by URI.

    A search returns a handful of pages; loading them through a single
    connection keeps the call off the per-page connection cost. The corpus is a
    1.2 GB file, so pages that a session has already read are kept in a small
    bounded cache — repeated questions about the same APIs would otherwise pay
    for random reads again.
    """
    if not uris:
        return {}

    found: dict[str, DocumentRecord] = {}
    missing: list[str] = []
    for uri in uris:
        cached = _PAGE_CACHE.get((str(db_path), uri))
        if cached is None:
            missing.append(uri)
        else:
            found[uri] = cached

    if missing:
        connection = shared(db_path, connect)
        placeholders = ",".join("?" for _ in missing)
        rows = connection.execute(
            f"select {_SELECT_COLUMNS} from documents where asset_id in ({placeholders})",
            missing,
        ).fetchall()

        for row in rows:
            if not isinstance(row[0], str):
                continue
            record = _record(row)
            found[record.uri] = record
            _remember(db_path, record)
    return found


class _PageCache:
    """A bounded FIFO cache of recently read pages, capped by total characters."""

    __slots__ = ("_entries", "_characters", "limit")

    def __init__(self, limit: int = PAGE_CACHE_CHARACTERS) -> None:
        self.limit = limit
        self._entries: dict[tuple[str, str], DocumentRecord] = {}
        self._characters = 0

    def get(self, key: tuple[str, str]) -> DocumentRecord | None:
        return self._entries.get(key)

    def put(self, key: tuple[str, str], record: DocumentRecord) -> None:
        size = len(record.content)
        if size > self.limit:
            return
        while self._characters + size > self.limit and self._entries:
            oldest = next(iter(self._entries))
            self._characters -= len(self._entries.pop(oldest).content)
        self._entries[key] = record
        self._characters += size

    def clear(self) -> None:
        self._entries.clear()
        self._characters = 0


_PAGE_CACHE: Final = _PageCache()


def _remember(db_path: Path, record: DocumentRecord) -> None:
    _PAGE_CACHE.put((str(db_path), record.uri), record)
