from __future__ import annotations

from apple_docs_mcp.bridge import Document, NotApprovedError, XcodeUnavailableError
from apple_docs_mcp.cli import EXIT_CODES, ExitCode, format_documents, main


def _doc(title: str, uri: str, contents: str) -> Document:
    return Document(title=title, uri=uri, contents=contents, score=0.7, kind="article")


def test_exit_codes_map_each_failure_distinctly() -> None:
    assert ExitCode.SUCCESS == 0
    assert ExitCode.USAGE == 1
    assert ExitCode.NOT_APPROVED == 2
    assert ExitCode.XCODE_UNAVAILABLE == 3
    assert ExitCode.TIMEOUT == 4
    assert ExitCode.PROTOCOL == 5
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)


def test_format_documents_includes_uri_contents_and_score() -> None:
    output = format_documents([_doc("ModelContainer", "/documentation/SwiftData/ModelContainer", "Creates a container.")])

    assert "/documentation/SwiftData/ModelContainer" in output
    assert "Creates a container." in output
    assert "0.70" in output


def test_format_documents_reports_empty_result_explicitly() -> None:
    assert "No documents" in format_documents([])


def test_main_returns_success_and_prints_documents(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "apple_docs_mcp.cli.search_docs",
        lambda query, frameworks=None, timeout=30.0: [_doc("View", "/documentation/SwiftUI/View", "A view.")],
    )

    code = main(["search", "swiftui view"])

    assert code == ExitCode.SUCCESS
    assert "/documentation/SwiftUI/View" in capsys.readouterr().out


def test_main_maps_not_approved_to_its_exit_code(capsys, monkeypatch) -> None:
    def refuse(query, frameworks=None, timeout=30.0):
        raise NotApprovedError("approve this process")

    monkeypatch.setattr("apple_docs_mcp.cli.search_docs", refuse)

    code = main(["search", "anything"])

    assert code == ExitCode.NOT_APPROVED
    assert "approve this process" in capsys.readouterr().err


def test_main_maps_missing_xcode_to_its_exit_code(capsys, monkeypatch) -> None:
    def unavailable(query, frameworks=None, timeout=30.0):
        raise XcodeUnavailableError("Xcode is not installed")

    monkeypatch.setattr("apple_docs_mcp.cli.search_docs", unavailable)

    code = main(["search", "anything"])

    assert code == ExitCode.XCODE_UNAVAILABLE
    assert "Xcode is not installed" in capsys.readouterr().err


def test_main_requires_a_query(capsys) -> None:
    code = main(["search"])

    assert code == ExitCode.USAGE
    assert "query" in capsys.readouterr().err.lower()


def test_main_json_mode_emits_parsable_payload(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "apple_docs_mcp.cli.search_docs",
        lambda query, frameworks=None, timeout=30.0: [_doc("View", "/documentation/SwiftUI/View", "A view.")],
    )

    code = main(["search", "swiftui view", "--json"])

    assert code == ExitCode.SUCCESS
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["documents"][0]["uri"] == "/documentation/SwiftUI/View"


def test_main_frameworks_lists_names(capsys, monkeypatch) -> None:
    monkeypatch.setattr("apple_docs_mcp.cli.list_frameworks", lambda: ["SwiftData", "SwiftUI"])

    code = main(["frameworks"])

    assert code == ExitCode.SUCCESS
    assert "SwiftData" in capsys.readouterr().out
