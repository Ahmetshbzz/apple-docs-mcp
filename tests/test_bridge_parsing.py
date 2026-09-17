from __future__ import annotations

import json

import pytest

from apple_docs_mcp.bridge import (
    BridgeProtocolError,
    Document,
    map_error_frame,
    parse_search_result,
)


def test_parse_search_result_extracts_documents() -> None:
    frame = {
        "id": 2,
        "jsonrpc": "2.0",
        "result": {
            "content": [{"text": "...", "type": "text"}],
            "structuredContent": {
                "documents": [
                    {
                        "title": "Adopting inheritance in SwiftData",
                        "uri": "/documentation/SwiftData/Adopting-inheritance-in-SwiftData",
                        "contents": "Design for specialization\n\n...",
                        "score": 0.699,
                        "kind": "article",
                    },
                ]
            },
        },
    }

    documents = parse_search_result(frame)

    assert documents == [
        Document(
            title="Adopting inheritance in SwiftData",
            uri="/documentation/SwiftData/Adopting-inheritance-in-SwiftData",
            contents="Design for specialization\n\n...",
            score=0.699,
            kind="article",
        )
    ]


def test_parse_search_result_rejects_missing_structured_content() -> None:
    frame = {"id": 2, "jsonrpc": "2.0", "result": {"content": []}}

    with pytest.raises(BridgeProtocolError):
        parse_search_result(frame)


def test_map_error_frame_detects_unapproved_agent() -> None:
    frame = {
        "id": 2,
        "jsonrpc": "2.0",
        "result": {
            "content": [
                {
                    "text": "This agent isn't approved to use Xcode's tools yet. "
                    "Call XcodeOpenWorkspace or XcodeNewProject first: opening or "
                    "creating a project is what asks the user to approve this agent, "
                    "together with access to that project's folder.",
                    "type": "text",
                }
            ],
            "isError": True,
        },
    }

    error = map_error_frame(frame)

    assert error is not None
    assert type(error).__name__ == "NotApprovedError"
    assert "XcodeOpenWorkspace" in str(error)


def test_map_error_frame_returns_none_for_success() -> None:
    frame = {"id": 2, "jsonrpc": "2.0", "result": {"structuredContent": {"documents": []}}}

    assert map_error_frame(frame) is None


def test_parse_search_result_tolerates_json_encoded_text() -> None:
    payload = {"documents": [{"title": "T", "uri": "/documentation/X", "contents": "c", "score": 0.5, "kind": "symbol"}]}
    frame = {
        "id": 2,
        "jsonrpc": "2.0",
        "result": {"content": [{"text": json.dumps(payload), "type": "text"}]},
    }

    documents = parse_search_result(frame)

    assert len(documents) == 1
    assert documents[0].uri == "/documentation/X"
