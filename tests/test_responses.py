from __future__ import annotations

import json
from pathlib import Path

from apple_docs_mcp.bridge import NotApprovedError
from apple_docs_mcp.corpus import DocumentationDBMissingError, DocumentRecord
from apple_docs_mcp.index import IndexInfo
from apple_docs_mcp.responses import (
    document_payload,
    error_payload,
    frameworks_payload,
    index_payload,
    search_payload,
    status_payload,
)
from apple_docs_mcp.search import Hit
from apple_docs_mcp.service import SearchOutcome, StatusReport


def _hit(uri: str = "/documentation/SwiftData/ModelContainer", **overrides: object) -> Hit:
    values = {
        "uri": uri,
        "title": "ModelContainer",
        "framework": "SwiftData",
        "kind": "symbol",
        "score": 12.5,
        "declaration": "@MainActor class ModelContainer",
        "availability": ["iOS 17+"],
        "snippet": "…manages the storage and object graph…",
        "source": "local",
    }
    values.update(overrides)
    return Hit(**values)


def _index(documents: int = 263513, fresh: bool = True) -> IndexInfo:
    return IndexInfo(
        path=Path("/tmp/index-v1.sqlite"),
        documents=documents,
        built_at=1.0,
        source_path="/corpus/index.sql",
        source_size=10,
        source_mtime=1.0,
        fresh=fresh,
    )


def test_search_payload_carries_ranking_metadata_and_declaration() -> None:
    payload = json.loads(search_payload(SearchOutcome([_hit()], "offline", _index())))

    assert payload["mode"] == "offline"
    assert payload["count"] == 1
    assert payload["indexDocuments"] == 263513
    document = payload["documents"][0]
    assert document["declaration"] == "@MainActor class ModelContainer"
    assert document["availability"] == ["iOS 17+"]
    assert document["source"] == "local"
    assert "contents" not in document


def test_search_payload_treats_an_empty_result_as_data() -> None:
    payload = json.loads(search_payload(SearchOutcome([], "offline", _index())))

    assert payload["documents"] == []
    assert payload["count"] == 0
    assert "error" not in payload


def test_search_payload_includes_full_text_when_provided() -> None:
    outcome = SearchOutcome([_hit()], "offline", _index())
    payload = json.loads(search_payload(outcome, {"/documentation/SwiftData/ModelContainer": "full"}))

    assert payload["documents"][0]["contents"] == "full"


def test_search_payload_reports_a_bridge_failure_without_losing_results() -> None:
    outcome = SearchOutcome([_hit()], "hybrid", _index(), bridge_error="bridge timed out")
    payload = json.loads(search_payload(outcome))

    assert payload["bridgeError"] == "bridge timed out"
    assert payload["count"] == 1


def test_search_payload_without_an_index_reports_zero_documents() -> None:
    payload = json.loads(search_payload(SearchOutcome([_hit()], "semantic", None)))

    assert payload["indexDocuments"] == 0


def test_document_payload_truncates_on_request() -> None:
    record = DocumentRecord(
        uri="/documentation/SwiftUI/View",
        title="View",
        framework="SwiftUI",
        kind="symbol",
        role="symbol",
        parent_uri="/documentation/SwiftUI",
        content="x" * 500,
        symbol=json.dumps({"kind": "Protocol", "preciseIdentifier": "s:7SwiftUI4ViewP"}),
        platforms=json.dumps([{"platform": "iOS", "introduced": 13, "deprecated": False}]),
    )
    payload = json.loads(document_payload(record, max_chars=100))

    assert payload["truncated"] is True
    assert len(payload["contents"]) == 100
    assert payload["symbolKind"] == "Protocol"
    assert payload["preciseIdentifier"] == "s:7SwiftUI4ViewP"
    assert payload["availability"] == ["iOS 13+"]


def test_document_payload_keeps_everything_without_a_limit() -> None:
    record = DocumentRecord(
        uri="/documentation/A/B",
        title="B",
        framework="A",
        kind="article",
        role="",
        parent_uri="",
        content="prose",
        symbol="",
        platforms="",
    )
    payload = json.loads(document_payload(record))

    assert payload["truncated"] is False
    assert payload["contents"] == "prose"
    assert payload["declaration"] is None


def test_frameworks_payload_lists_names_and_counts() -> None:
    payload = json.loads(frameworks_payload([("SwiftUI", 10944), ("UIKit", 14211)]))

    assert payload["count"] == 2
    assert payload["frameworks"][1] == {"name": "UIKit", "documents": 14211}


def test_error_payload_names_the_remedy_for_a_known_error() -> None:
    payload = json.loads(error_payload(NotApprovedError("Xcode isn't approved")))

    assert payload["error"] == "NotApprovedError"
    assert "xcrun mcp-server open" in payload["remedy"]


def test_error_payload_names_the_remedy_for_a_missing_corpus() -> None:
    payload = json.loads(error_payload(DocumentationDBMissingError("no corpus")))

    assert "Components" in payload["remedy"]


def test_error_payload_falls_back_for_unknown_errors() -> None:
    payload = json.loads(error_payload(RuntimeError("odd")))

    assert payload["remedy"].startswith("Unexpected failure")


def test_status_payload_reports_index_and_bridge_state() -> None:
    report = StatusReport(
        asset_path=Path("/asset/index.sql"),
        index=_index(documents=10),
        bridge_command=("xcrun", "mcpbridge"),
        bridge_documents=20,
        bridge_error=None,
    )
    payload = json.loads(status_payload(report))

    assert payload["searchable"] is True
    assert payload["assetInstalled"] is True
    assert payload["bridgeCommand"] == "xcrun mcpbridge"
    assert payload["bridgeAvailable"] is True
    assert payload["index"]["documents"] == 10


def test_status_payload_handles_a_missing_index_and_a_failing_bridge() -> None:
    report = StatusReport(
        asset_path=None,
        index=None,
        bridge_command=("xcrun", "mcpbridge"),
        bridge_documents=None,
        bridge_error="bridge not approved",
    )
    payload = json.loads(status_payload(report))

    assert payload["searchable"] is False
    assert payload["index"] is None
    assert payload["bridgeError"] == "bridge not approved"


def test_index_payload_reports_the_build(tmp_path: Path) -> None:
    built = tmp_path / "index-v1.sqlite"
    built.write_bytes(b"x" * 32)

    payload = json.loads(index_payload(built, 263513, True, 7.234))

    assert payload["documents"] == 263513
    assert payload["sizeBytes"] == 32
    assert payload["seconds"] == 7.23
