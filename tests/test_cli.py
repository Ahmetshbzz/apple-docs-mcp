from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_docs_mcp.cli import ExitCode, main
from apple_docs_mcp.service import StatusReport


@pytest.fixture
def cli_environment(asset_with_documents: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a synthetic corpus and a throwaway cache."""
    monkeypatch.setenv("APPLE_DOCS_ASSET_ROOT", str(asset_with_documents))
    monkeypatch.setenv("APPLE_DOCS_CACHE_DIR", str(tmp_path / "cache"))
    return asset_with_documents


def test_search_prints_ranked_pages(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["search", "inheritance in a data model"])

    printed = capsys.readouterr().out
    assert exit_code == ExitCode.SUCCESS
    assert "Adopting inheritance in SwiftData" in printed
    assert "/documentation/SwiftData/Adopting-inheritance-in-SwiftData" in printed


def test_search_reports_the_declaration_of_a_symbol_page(
    cli_environment: Path, capsys: pytest.CaptureFixture
) -> None:
    main(["search", "ModelContainer"])

    assert "@MainActor class ModelContainer" in capsys.readouterr().out


def test_search_emits_json(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["search", "inheritance", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == ExitCode.SUCCESS
    assert payload["mode"] == "offline"
    assert payload["documents"][0]["uri"].startswith("/documentation/")


def test_search_can_include_full_text(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    main(["search", "inheritance", "--json", "--full"])

    payload = json.loads(capsys.readouterr().out)
    assert "inheritance" in payload["documents"][0]["contents"]


def test_search_honours_the_framework_filter(
    cli_environment: Path, capsys: pytest.CaptureFixture
) -> None:
    main(["search", "category", "--framework", "AVFAudio", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert {document["framework"] for document in payload["documents"]} == {"AVFAudio"}


def test_search_without_a_query_is_a_usage_error(
    cli_environment: Path, capsys: pytest.CaptureFixture
) -> None:
    exit_code = main(["search"])

    assert exit_code == ExitCode.USAGE
    assert "non-empty query" in capsys.readouterr().err


def test_search_reports_a_missing_corpus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("APPLE_DOCS_ASSET_ROOT", str(tmp_path / "nothing"))
    monkeypatch.setenv("APPLE_DOCS_CACHE_DIR", str(tmp_path / "cache"))

    exit_code = main(["search", "container"])

    assert exit_code == ExitCode.ASSET_MISSING
    assert "Developer Documentation" in capsys.readouterr().err


def test_get_prints_a_whole_page(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["get", "/documentation/SwiftData/ModelContainer"])

    printed = capsys.readouterr().out
    assert exit_code == ExitCode.SUCCESS
    assert "@MainActor class ModelContainer" in printed
    assert "manages the storage and object graph" in printed


def test_get_emits_json(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    main(["get", "/documentation/SwiftData/ModelContainer", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["availability"] == ["iOS 17+"]
    assert payload["truncated"] is False


def test_get_can_truncate(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    main(["get", "/documentation/SwiftData/ModelContainer", "--json", "--max-chars", "20"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["truncated"] is True
    assert len(payload["contents"]) == 20


def test_get_reports_an_unknown_uri(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["get", "/documentation/Absent"])

    assert exit_code == ExitCode.NOT_FOUND
    assert "no documentation page" in capsys.readouterr().err


def test_frameworks_lists_names(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["frameworks"])

    printed = capsys.readouterr().out
    assert exit_code == ExitCode.SUCCESS
    assert "AVFAudio" in printed
    assert "SwiftData" in printed


def test_frameworks_emits_counts_in_json(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    main(["index"])
    capsys.readouterr()

    main(["frameworks", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert {entry["name"] for entry in payload["frameworks"]} == {"AVFAudio", "SwiftData"}


def test_index_builds_and_reports(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = main(["index", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == ExitCode.SUCCESS
    assert payload["documents"] == 3
    assert Path(payload["path"]).is_file()


def test_index_rebuild_is_reported(cli_environment: Path, capsys: pytest.CaptureFixture) -> None:
    main(["index"])
    capsys.readouterr()

    main(["index", "--rebuild", "--json"])

    assert json.loads(capsys.readouterr().out)["rebuilt"] is True


def test_status_prints_state(cli_environment: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    report = StatusReport(
        asset_path=cli_environment,
        index=None,
        bridge_command=("xcrun", "mcpbridge"),
        bridge_documents=20,
        bridge_error=None,
    )
    monkeypatch.setattr("apple_docs_mcp.cli.run_status", lambda: report)

    exit_code = main(["status"])

    printed = capsys.readouterr().out
    assert exit_code == ExitCode.SUCCESS
    assert "corpus:" in printed
    assert "index: not built" in printed
    assert "bridge: available" in printed


def test_status_is_unavailable_without_any_source(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    report = StatusReport(
        asset_path=None,
        index=None,
        bridge_command=("xcrun", "mcpbridge"),
        bridge_documents=None,
        bridge_error="not approved",
    )
    monkeypatch.setattr("apple_docs_mcp.cli.run_status", lambda: report)

    exit_code = main(["status", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == ExitCode.XCODE_UNAVAILABLE
    assert payload["searchable"] is False
    assert payload["bridgeError"] == "not approved"
