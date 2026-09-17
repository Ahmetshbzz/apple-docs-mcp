from __future__ import annotations

from pathlib import Path

import pytest

from apple_docs_mcp.bridge import Document
from apple_docs_mcp.connections import shared
from apple_docs_mcp.index import open_index
from apple_docs_mcp.search import (
    Hit,
    bridge_hits,
    build_match_query,
    enrich_from_corpus,
    fuse_rrf,
    identifier_in,
    make_snippet,
    search_offline,
    select_terms,
    term_document_frequencies,
    tokenize_query,
)


def test_tokenize_query_drops_stopwords_and_keeps_identifiers() -> None:
    assert tokenize_query("how do I use AVAudioSession.Category") == [
        "AVAudioSession.Category",
    ]


def test_tokenize_query_keeps_terms_when_everything_is_a_stopword() -> None:
    assert tokenize_query("what is it") == ["what", "is", "it"]


def test_build_match_query_quotes_every_term() -> None:
    assert build_match_query("lazy loading") == '"lazy" OR "loading"'


def test_build_match_query_rejects_an_empty_query() -> None:
    with pytest.raises(ValueError):
        build_match_query("   ")


def test_build_match_query_can_be_narrowed_to_selected_terms() -> None:
    assert build_match_query("anything", ["SwiftUI", "View"]) == '"SwiftUI" OR "View"'


def test_select_terms_drops_terms_that_cannot_narrow_the_corpus() -> None:
    frequencies = {"model": 40_000, "inheritance": 120, "swiftdata": 800}

    assert select_terms(["model", "inheritance", "swiftdata"], frequencies, 263_513) == [
        "inheritance",
        "swiftdata",
    ]


def test_select_terms_keeps_the_rarest_when_every_term_is_common() -> None:
    frequencies = {"view": 30_000, "model": 40_000, "data": 50_000}

    assert select_terms(["view", "model", "data"], frequencies, 263_513) == ["view", "model"]


def test_select_terms_leaves_a_single_term_alone() -> None:
    assert select_terms(["view"], {"view": 30_000}, 263_513) == ["view"]


def test_select_terms_drops_unknown_terms_when_a_known_one_exists() -> None:
    """A term the index has never seen can only make a strict pass match nothing."""
    frequencies = {"zzzznotpresent": 0, "swiftui": 10_000}

    assert select_terms(["zzzznotpresent", "swiftui"], frequencies, 263_513) == ["swiftui"]


def test_select_terms_keeps_unknown_terms_when_nothing_is_known() -> None:
    frequencies = {"zzzz": 0, "yyyy": 0}

    assert select_terms(["zzzz", "yyyy"], frequencies, 263_513) == ["zzzz", "yyyy"]


def test_select_terms_caps_the_number_of_terms() -> None:
    frequencies = {f"term{index}": index + 1 for index in range(10)}
    selected = select_terms(list(frequencies), frequencies, 263_513, max_terms=3)

    assert selected == ["term0", "term1", "term2"]


def test_term_document_frequencies_come_from_the_index(indexed_asset: tuple[Path, Path]) -> None:
    _, index_file = indexed_asset
    connection = shared(index_file, open_index)

    frequencies = term_document_frequencies(connection, ["inheritance", "zzzznotpresent"])

    assert frequencies == {"inheritance": 1}


def test_search_offline_still_answers_when_no_page_carries_every_term(
    indexed_asset: tuple[Path, Path],
) -> None:
    """A strict pass can come back empty; the loose pass must still answer."""
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "inheritance container", total_documents=3)

    assert hits, "the OR pass must fill in when the AND pass matches nothing"
    assert {hit.uri for hit in hits} == {
        "/documentation/SwiftData/Adopting-inheritance-in-SwiftData",
        "/documentation/SwiftData/ModelContainer",
    }


def test_identifier_in_finds_dotted_and_camel_case_names() -> None:
    assert identifier_in("how does AVAudioSession.Category work") == "AVAudioSession.Category"
    assert identifier_in("where is ModelContainer defined") == "ModelContainer"
    assert identifier_in("how do i observe changes") is None


def test_make_snippet_centres_on_the_matching_term() -> None:
    content = ("padding " * 60) + "inheritance chain " + ("trailing " * 60)
    snippet = make_snippet(content, ["inheritance"], width=80)

    assert "inheritance chain" in snippet
    assert snippet.startswith("…")


def test_make_snippet_falls_back_to_the_head_of_the_page() -> None:
    assert make_snippet("no match here", ["absent"], width=8) == "no match"


def test_make_snippet_handles_an_empty_page() -> None:
    assert make_snippet("", ["anything"]) == ""


def test_search_offline_finds_a_page_by_its_prose(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "inheritance in a data model")

    assert hits, "expected at least one hit"
    assert hits[0].uri == "/documentation/SwiftData/Adopting-inheritance-in-SwiftData"
    assert hits[0].source == "local"
    assert "inheritance" in hits[0].snippet


def test_search_offline_surfaces_declaration_and_availability(
    indexed_asset: tuple[Path, Path],
) -> None:
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "ModelContainer schema")

    container = next(hit for hit in hits if hit.uri.endswith("/ModelContainer"))
    assert container.declaration == "@MainActor class ModelContainer"
    assert container.availability == ["iOS 17+"]


def test_search_offline_restricts_to_a_framework(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "category", frameworks=["AVFAudio"])

    assert hits
    assert {hit.framework for hit in hits} == {"AVFAudio"}


def test_search_offline_restricts_to_a_kind(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "inheritance", kinds=["article"])

    assert hits
    assert {hit.kind for hit in hits} == {"article"}


def test_search_offline_respects_the_limit(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    assert len(search_offline(db_path, index_file, "inheritance OR container OR category", limit=1)) == 1


def test_search_offline_ranks_an_exact_identifier_first(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    hits = search_offline(db_path, index_file, "AVAudioSession.Category")

    assert hits[0].uri == "/documentation/AVFAudio/AVAudioSession/Category-swift.struct"


def test_search_offline_returns_nothing_for_an_absent_term(indexed_asset: tuple[Path, Path]) -> None:
    db_path, index_file = indexed_asset
    assert search_offline(db_path, index_file, "zzzznotpresent") == []


def test_bridge_hits_map_documents_into_hits() -> None:
    hits = bridge_hits(
        [Document(title="View", uri="/documentation/SwiftUI/View", contents="hello", score=0.7, kind="symbol")]
    )

    assert hits[0].source == "bridge"
    assert hits[0].score == pytest.approx(0.7)
    assert hits[0].declaration is None


def test_fuse_rrf_prefers_pages_ranked_well_in_both_lists() -> None:
    local = [
        Hit("/a", "A", "F", "symbol", 10.0, None, [], "", "local"),
        Hit("/b", "B", "F", "symbol", 9.0, None, [], "", "local"),
    ]
    bridge = [
        Hit("/b", "B", "F", "symbol", 0.72, None, [], "", "bridge"),
        Hit("/c", "C", "F", "symbol", 0.70, None, [], "", "bridge"),
    ]

    fused = fuse_rrf(local, bridge, limit=3)

    assert fused[0].uri == "/b"
    assert fused[0].source == "hybrid"
    assert {hit.uri for hit in fused} == {"/a", "/b", "/c"}


def test_fuse_rrf_respects_the_limit() -> None:
    local = [Hit(f"/{index}", f"T{index}", "F", "symbol", 1.0, None, [], "", "local") for index in range(5)]
    assert len(fuse_rrf(local, [], limit=2)) == 2


def test_enrich_from_corpus_completes_bridge_hits(indexed_asset: tuple[Path, Path]) -> None:
    db_path, _ = indexed_asset
    bridge_only = [
        Hit(
            uri="/documentation/SwiftData/ModelContainer",
            title="ModelContainer",
            framework="",
            kind="symbol",
            score=0.7,
            declaration=None,
            availability=[],
            snippet="",
            source="bridge",
        )
    ]

    enriched = enrich_from_corpus(db_path, bridge_only)

    assert enriched[0].declaration == "@MainActor class ModelContainer"
    assert enriched[0].availability == ["iOS 17+"]
    assert enriched[0].framework == "SwiftData"


def test_enrich_from_corpus_leaves_complete_hits_alone(indexed_asset: tuple[Path, Path]) -> None:
    db_path, _ = indexed_asset
    local = [Hit("/documentation/X", "X", "F", "symbol", 1.0, None, [], "s", "local")]
    assert enrich_from_corpus(db_path, local) == local
