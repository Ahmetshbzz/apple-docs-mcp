from __future__ import annotations

import json

import pytest

from apple_docs_mcp.bridge import (
    BridgeProtocolError,
    BridgeTimeoutError,
    Document,
    NotApprovedError,
    XcodeUnavailableError,
)
from apple_docs_mcp.frameworks import DocumentationAssetMissingError
from apple_docs_mcp.responses import (
    error_payload,
    frameworks_payload,
    search_payload,
    status_payload,
)


def _doc(uri: str = "/documentation/SwiftUI/View") -> Document:
    return Document(title="View", uri=uri, contents="A view.", score=0.9, kind="symbol")


def test_search_payload_wraps_documents() -> None:
    body = json.loads(search_payload([_doc()]))

    assert body["documents"][0]["uri"] == "/documentation/SwiftUI/View"
    assert body["documents"][0]["contents"] == "A view."


def test_search_payload_reports_an_empty_result_as_data_not_error() -> None:
    body = json.loads(search_payload([]))

    assert body["documents"] == []


def test_frameworks_payload_lists_names() -> None:
    body = json.loads(frameworks_payload(["SwiftData", "SwiftUI"]))

    assert body["frameworks"] == ["SwiftData", "SwiftUI"]


@pytest.mark.parametrize(
    ("error", "expected_name"),
    [
        (NotApprovedError("x"), "NotApprovedError"),
        (XcodeUnavailableError("x"), "XcodeUnavailableError"),
        (BridgeTimeoutError("x"), "BridgeTimeoutError"),
        (BridgeProtocolError("x"), "BridgeProtocolError"),
        (DocumentationAssetMissingError("x"), "DocumentationAssetMissingError"),
    ],
)
def test_error_payload_names_every_failure_class(error: BaseException, expected_name: str) -> None:
    body = json.loads(error_payload(error))

    assert body["error"] == expected_name
    assert body["message"] == "x"
    assert body["remedy"].strip()


def test_error_payload_gives_an_approval_remedy_for_unapproved_agents() -> None:
    body = json.loads(error_payload(NotApprovedError("not approved")))

    assert "approv" in body["remedy"].lower()
    assert "24 hours" in body["remedy"]


def test_error_payload_gives_a_download_remedy_for_a_missing_asset() -> None:
    body = json.loads(error_payload(DocumentationAssetMissingError("absent")))

    assert "Components" in body["remedy"]


def test_status_payload_reports_a_searchable_bridge() -> None:
    body = json.loads(status_payload(asset_path=None, documents=[_doc()], error=None))

    assert body["searchable"] is True
    assert body["approved"] is True
    assert body["probeDocumentCount"] == 1


def test_status_payload_reports_an_unapproved_bridge() -> None:
    body = json.loads(status_payload(asset_path=None, documents=None, error=NotApprovedError("nope")))

    assert body["searchable"] is False
    assert body["approved"] is False
    assert body["detail"]