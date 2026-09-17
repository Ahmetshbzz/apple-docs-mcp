from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from conftest import build_asset, document

from apple_docs_mcp.corpus import find_documentation_db
from apple_docs_mcp.index import (
    INDEX_SCHEMA_VERSION,
    build_index,
    ensure_index,
    index_info,
    index_path,
    open_index,
)


def test_build_index_records_every_document(asset_with_documents: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    info = build_index(find_documentation_db(asset_with_documents), cache)

    assert info.documents == 3
    assert info.path.is_file()
    assert info.path.name == f"index-v{INDEX_SCHEMA_VERSION}.sqlite"


def test_built_index_is_searchable(asset_with_documents: Path, tmp_path: Path) -> None:
    info = build_index(find_documentation_db(asset_with_documents), tmp_path / "cache")
    connection = open_index(info.path)
    try:
        hits = connection.execute(
            "select count(*) from docs_fts where docs_fts match ?", ("inheritance",)
        ).fetchone()
    finally:
        connection.close()

    assert hits is not None and hits[0] == 1


def test_index_is_fresh_after_building(asset_with_documents: Path, tmp_path: Path) -> None:
    db_path = find_documentation_db(asset_with_documents)
    cache = tmp_path / "cache"
    build_index(db_path, cache)

    info = index_info(db_path, cache)
    assert info is not None
    assert info.fresh is True


def test_ensure_index_reuses_a_fresh_index(asset_with_documents: Path, tmp_path: Path) -> None:
    db_path = find_documentation_db(asset_with_documents)
    cache = tmp_path / "cache"
    first = ensure_index(db_path, cache)
    second = ensure_index(db_path, cache)

    assert first.path == second.path
    assert second.rebuilt is False


def test_index_is_rebuilt_when_the_corpus_changes(asset_with_documents: Path, tmp_path: Path) -> None:
    db_path = find_documentation_db(asset_with_documents)
    assert db_path is not None
    cache = tmp_path / "cache"
    ensure_index(db_path, cache)

    build_asset(
        asset_with_documents,
        [
            document("/documentation/A/B", "B", "A", "B\nfresh content"),
            document("/documentation/A/C", "C", "A", "C\nmore content"),
        ],
    )
    later = time.time() + 5
    db_path.touch()
    db_path.chmod(0o644)
    import os

    os.utime(db_path, (later, later))

    rebuilt = ensure_index(db_path, cache)
    assert rebuilt.rebuilt is True
    assert rebuilt.documents == 2


def test_index_is_rebuilt_for_an_unknown_schema_version(
    asset_with_documents: Path, tmp_path: Path
) -> None:
    db_path = find_documentation_db(asset_with_documents)
    cache = tmp_path / "cache"
    built = build_index(db_path, cache)

    writable = sqlite3.connect(built.path)
    writable.execute("update index_meta set value = ? where key = 'schema_version'", ("0",))
    writable.commit()
    writable.close()

    assert index_info(db_path, cache).fresh is False
    assert ensure_index(db_path, cache).rebuilt is True


def test_force_rebuilds_an_otherwise_fresh_index(asset_with_documents: Path, tmp_path: Path) -> None:
    db_path = find_documentation_db(asset_with_documents)
    cache = tmp_path / "cache"
    ensure_index(db_path, cache)

    assert ensure_index(db_path, cache, force=True).rebuilt is True


def test_index_meta_records_the_corpus_identity(asset_with_documents: Path, tmp_path: Path) -> None:
    db_path = find_documentation_db(asset_with_documents)
    info = build_index(db_path, tmp_path / "cache")

    assert info.source_path == str(db_path)
    assert info.source_size == db_path.stat().st_size
    assert info.built_at > 0


def test_building_removes_indexes_from_older_schema_versions(
    asset_with_documents: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "index-v1.sqlite"
    stale.write_bytes(b"old")

    info = build_index(find_documentation_db(asset_with_documents), cache)

    assert not stale.exists()
    assert info.path.is_file()


def test_index_path_is_stable_for_a_cache_directory(tmp_path: Path) -> None:
    assert index_path(tmp_path).parent == tmp_path
    assert index_path(tmp_path).name == f"index-v{INDEX_SCHEMA_VERSION}.sqlite"
