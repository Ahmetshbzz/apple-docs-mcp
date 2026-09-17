from __future__ import annotations

import json
from pathlib import Path

from apple_docs_mcp.frameworks import find_asset_root


def test_find_asset_root_returns_none_when_no_asset_installed(tmp_path: Path) -> None:
    assert find_asset_root(tmp_path) is None


def test_find_asset_root_locates_documentation_asset(tmp_path: Path) -> None:
    index_dir = tmp_path / "documentation-cache" / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "index.json").write_text(json.dumps({"interfaceLanguages": {"data": []}}), encoding="utf-8")

    assert find_asset_root(tmp_path) == tmp_path


def test_find_asset_root_returns_none_for_a_non_directory(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert find_asset_root(missing) is None


def test_find_asset_root_descends_into_the_versioned_asset_bundle(tmp_path: Path) -> None:
    """The installed layout is <root>/<uuid>.asset/AssetData/documentation-cache/index."""
    data = tmp_path / "9fdbc6a2ba5e0388d3b6775794ebb1939f527020.asset" / "AssetData"
    index_dir = data / "documentation-cache" / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "index.json").write_text(json.dumps({"interfaceLanguages": {"data": []}}), encoding="utf-8")

    assert find_asset_root(tmp_path) == tmp_path


def test_list_frameworks_works_on_the_versioned_asset_bundle(tmp_path: Path) -> None:
    from apple_docs_mcp.frameworks import list_frameworks

    data = tmp_path / "abc123.asset" / "AssetData"
    index_dir = data / "documentation-cache" / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "index.json").write_text(
        json.dumps(
            {
                "interfaceLanguages": {
                    "data": [{"path": "/documentation/SwiftData/Schema", "title": "Schema", "type": "symbol"}]
                }
            }
        ),
        encoding="utf-8",
    )

    assert list_frameworks(tmp_path) == ["SwiftData"]
