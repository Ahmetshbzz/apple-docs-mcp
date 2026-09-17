from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apple_docs_mcp.corpus import (
    DocumentationDBMissingError,
    count_documents,
    find_documentation_db,
    get_document,
    iter_documents,
)


def test_find_documentation_db_returns_none_without_an_asset(tmp_path: Path) -> None:
    assert find_documentation_db(tmp_path) is None


def test_find_documentation_db_locates_the_versioned_bundle(asset_with_documents: Path) -> None:
    found = find_documentation_db(asset_with_documents)
    assert found is not None
    assert found.name == "index.sql"
    assert "documentation-db" in found.parts


def test_count_documents_reads_the_corpus_size(asset_with_documents: Path) -> None:
    assert count_documents(find_documentation_db(asset_with_documents)) == 3


def test_iter_documents_yields_every_page(asset_with_documents: Path) -> None:
    records = list(iter_documents(find_documentation_db(asset_with_documents)))
    assert len(records) == 3
    assert {record.framework for record in records} == {"SwiftData", "AVFAudio"}


def test_iter_documents_parses_declaration_and_availability(asset_with_documents: Path) -> None:
    records = {
        record.uri: record for record in iter_documents(find_documentation_db(asset_with_documents))
    }
    container = records["/documentation/SwiftData/ModelContainer"]
    assert container.declaration == "@MainActor class ModelContainer"
    assert container.title == "ModelContainer"
    assert container.kind == "symbol"
    assert container.availability == ["iOS 17+"]


def test_iter_documents_keeps_full_page_text(asset_with_documents: Path) -> None:
    records = {
        record.uri: record for record in iter_documents(find_documentation_db(asset_with_documents))
    }
    article = records["/documentation/SwiftData/Adopting-inheritance-in-SwiftData"]
    assert article.declaration is None
    assert "inheritance is right for your data model" in article.content


def test_get_document_returns_one_page(asset_with_documents: Path) -> None:
    record = get_document(
        find_documentation_db(asset_with_documents),
        "/documentation/AVFAudio/AVAudioSession/Category-swift.struct",
    )
    assert record is not None
    assert record.declaration == "struct Category"
    assert record.framework == "AVFAudio"


def test_get_document_returns_none_for_an_unknown_uri(asset_with_documents: Path) -> None:
    assert get_document(find_documentation_db(asset_with_documents), "/documentation/Nope") is None


def test_repeated_page_reads_are_served_from_the_cache(
    asset_with_documents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apple_docs_mcp import corpus

    db_path = find_documentation_db(asset_with_documents)
    assert db_path is not None
    corpus._PAGE_CACHE.clear()

    opens = 0
    original = corpus.connect

    def counting_connect(path: Path):
        nonlocal opens
        opens += 1
        return original(path)

    monkeypatch.setattr(corpus, "connect", counting_connect)
    uri = "/documentation/SwiftData/ModelContainer"

    first = get_document(db_path, uri)
    opens_after_first = opens
    second = get_document(db_path, uri)

    assert first == second
    assert opens_after_first == 1
    assert opens == opens_after_first, "a cached page must not reopen the corpus"
    corpus._PAGE_CACHE.clear()


def test_page_cache_evicts_the_oldest_entry_when_full(asset_with_documents: Path) -> None:
    from apple_docs_mcp.corpus import DocumentRecord, _PageCache

    cache = _PageCache(limit=10)

    def page(uri: str, size: int) -> DocumentRecord:
        return DocumentRecord(
            uri=uri,
            title="t",
            framework="f",
            kind="symbol",
            role="",
            parent_uri="",
            content="x" * size,
            symbol="",
            platforms="",
        )

    cache.put(("/db", "/a"), page("/a", 6))
    cache.put(("/db", "/b"), page("/b", 6))

    assert cache.get(("/db", "/a")) is None, "the oldest page must be evicted"
    assert cache.get(("/db", "/b")) is not None


def test_page_cache_skips_pages_larger_than_its_limit() -> None:
    from apple_docs_mcp.corpus import DocumentRecord, _PageCache

    cache = _PageCache(limit=4)
    oversized = DocumentRecord(
        uri="/big",
        title="t",
        framework="f",
        kind="symbol",
        role="",
        parent_uri="",
        content="x" * 500,
        symbol="",
        platforms="",
    )
    cache.put(("/db", "/big"), oversized)

    assert cache.get(("/db", "/big")) is None


def test_missing_database_raises_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(DocumentationDBMissingError):
        count_documents(tmp_path / "absent.sql")


def test_a_database_without_the_documents_table_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "index.sql"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE unrelated (id INTEGER);")
    connection.commit()
    connection.close()

    with pytest.raises(DocumentationDBMissingError):
        count_documents(path)


def test_article_code_blocks_are_not_treated_as_declarations() -> None:
    """Only symbol pages carry a declaration; an article's code fence is an example."""
    from apple_docs_mcp.corpus import DocumentRecord

    record = DocumentRecord(
        uri="/documentation/SwiftUI/Writing-code",
        title="Writing code",
        framework="SwiftUI",
        kind="article",
        role="",
        parent_uri="",
        content="Writing code\nArticle\n\n```\nstruct Demo { }\n```\n",
        symbol="",
        platforms="",
    )
    assert record.declaration is None


def test_symbol_and_platforms_raw_json_are_preserved(asset_with_documents: Path) -> None:
    records = {
        record.uri: record for record in iter_documents(find_documentation_db(asset_with_documents))
    }
    container = records["/documentation/SwiftData/ModelContainer"]
    assert json.loads(container.symbol)["kind"] == "Class"
    assert json.loads(container.platforms)[0]["platform"] == "iOS"
