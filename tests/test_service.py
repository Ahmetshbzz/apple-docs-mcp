from __future__ import annotations

from pathlib import Path

import pytest

from apple_docs_mcp.bridge import BridgeTimeoutError, Document
from apple_docs_mcp.corpus import DocumentationDBMissingError
from apple_docs_mcp.service import (
    Settings,
    build,
    document,
    frameworks,
    playbook,
    reference,
    search,
    status,
    texts_for,
)
from apple_docs_mcp.skill import SkillMissingError


@pytest.fixture
def settings(asset_with_documents: Path, tmp_path: Path) -> Settings:
    return Settings(asset_root=asset_with_documents, cache_dir=tmp_path / "cache")


def test_search_runs_offline_and_builds_the_index(settings: Settings) -> None:
    outcome = search("inheritance in a data model", settings=settings)

    assert outcome.mode == "offline"
    assert outcome.hits
    assert outcome.index is not None and outcome.index.documents == 3


def test_search_reuses_a_fresh_index(settings: Settings) -> None:
    search("inheritance", settings=settings)
    second = search("container", settings=settings)

    assert second.index is not None and second.index.rebuilt is False


def test_search_passes_filters_through(settings: Settings) -> None:
    outcome = search("category", frameworks=["AVFAudio"], kinds=["symbol"], settings=settings)

    assert outcome.hits
    assert {hit.framework for hit in outcome.hits} == {"AVFAudio"}
    assert {hit.kind for hit in outcome.hits} == {"symbol"}


def test_search_rejects_an_empty_query(settings: Settings) -> None:
    with pytest.raises(ValueError):
        search("   ", settings=settings)


def test_search_rejects_an_unknown_mode(settings: Settings) -> None:
    with pytest.raises(ValueError):
        search("container", mode="guess", settings=settings)


def test_search_rejects_a_non_positive_limit(settings: Settings) -> None:
    with pytest.raises(ValueError):
        search("container", limit=0, settings=settings)


def test_search_reports_a_missing_corpus(tmp_path: Path) -> None:
    missing = Settings(asset_root=tmp_path / "nothing", cache_dir=tmp_path / "cache")

    with pytest.raises(DocumentationDBMissingError):
        search("container", settings=missing)


def test_hybrid_mode_fuses_the_bridge_into_the_local_ranking(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apple_docs_mcp.service.bridge_search",
        lambda *args, **kwargs: [
            Document(
                title="Category",
                uri="/documentation/AVFAudio/AVAudioSession/Category-swift.struct",
                contents="bridge excerpt",
                score=0.9,
                kind="symbol",
            )
        ],
    )

    outcome = search("AVAudioSession category", mode="hybrid", settings=settings)

    assert outcome.hits
    assert any(hit.source == "hybrid" for hit in outcome.hits)
    assert outcome.bridge_error is None


def test_hybrid_mode_degrades_to_offline_when_the_bridge_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise BridgeTimeoutError("Bridge did not answer within 30s")

    monkeypatch.setattr("apple_docs_mcp.service.bridge_search", explode)

    outcome = search("inheritance", mode="hybrid", settings=settings)

    assert outcome.hits, "offline results must survive a bridge failure"
    assert outcome.bridge_error is not None


def test_semantic_mode_propagates_a_bridge_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise BridgeTimeoutError("Bridge did not answer within 30s")

    monkeypatch.setattr("apple_docs_mcp.service.bridge_search", explode)

    with pytest.raises(BridgeTimeoutError):
        search("inheritance", mode="semantic", settings=settings)


def test_semantic_mode_enriches_bridge_hits_from_the_corpus(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apple_docs_mcp.service.bridge_search",
        lambda *args, **kwargs: [
            Document(
                title="ModelContainer",
                uri="/documentation/SwiftData/ModelContainer",
                contents="short excerpt",
                score=0.8,
                kind="symbol",
            )
        ],
    )

    outcome = search("ModelContainer", mode="semantic", settings=settings)

    assert outcome.hits[0].declaration == "@MainActor class ModelContainer"


def test_document_returns_a_whole_page(settings: Settings) -> None:
    record = document("/documentation/AVFAudio/AVAudioSession/Category-swift.struct", settings=settings)

    assert record is not None
    assert record.declaration == "struct Category"


def test_document_returns_none_for_an_unknown_uri(settings: Settings) -> None:
    assert document("/documentation/Absent", settings=settings) is None


def test_texts_for_loads_page_text_for_hits(settings: Settings) -> None:
    outcome = search("inheritance", settings=settings)
    texts = texts_for(outcome.hits, settings=settings)

    assert set(texts) == {hit.uri for hit in outcome.hits}
    assert "inheritance" in texts[outcome.hits[0].uri]


def test_texts_for_returns_nothing_without_hits(settings: Settings) -> None:
    assert texts_for([], settings=settings) == {}


def test_frameworks_counts_pages_from_the_index(settings: Settings) -> None:
    search("inheritance", settings=settings)
    counted = dict(frameworks(settings=settings))

    assert counted == {"AVFAudio": 1, "SwiftData": 2}


def test_frameworks_works_before_the_index_exists(settings: Settings) -> None:
    names = dict(frameworks(settings=settings))

    assert names == {"AVFAudio": 0, "SwiftData": 0}


def test_build_reports_a_rebuild(settings: Settings) -> None:
    info = build(force=True, settings=settings)

    assert info.rebuilt is True
    assert info.documents == 3


def test_status_reports_corpus_and_index_without_probing_the_bridge(settings: Settings) -> None:
    search("inheritance", settings=settings)
    report = status(probe_bridge=False, settings=settings)

    assert report.asset_path is not None
    assert report.index is not None and report.index.fresh
    assert report.searchable is True
    assert report.bridge_documents is None
    assert report.bridge_error is None


def test_playbook_reads_the_configured_skill_directory(skill_directory: Path, tmp_path: Path) -> None:
    settings = Settings(asset_root=tmp_path, cache_dir=tmp_path, skill_root=skill_directory)
    book = playbook(settings=settings)

    assert book.name == "swift"
    assert book.body.startswith("# Swift playbook")
    assert book.references == ("swiftui",)


def test_reference_returns_one_topic(skill_directory: Path, tmp_path: Path) -> None:
    settings = Settings(asset_root=tmp_path, cache_dir=tmp_path, skill_root=skill_directory)

    assert reference("swiftui", settings=settings) == "state and data flow"


def test_playbook_reports_a_missing_skill_directory(tmp_path: Path) -> None:
    settings = Settings(asset_root=tmp_path, cache_dir=tmp_path, skill_root=tmp_path / "absent")

    with pytest.raises(SkillMissingError):
        playbook(settings=settings)


def test_status_without_a_corpus_reports_not_searchable(tmp_path: Path) -> None:
    report = status(probe_bridge=False, settings=Settings(asset_root=tmp_path / "none", cache_dir=tmp_path))

    assert report.asset_path is None
    assert report.searchable is False
