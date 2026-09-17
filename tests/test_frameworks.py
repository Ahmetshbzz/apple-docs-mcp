from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_docs_mcp.frameworks import (
    ASSET_ROOT,
    DocumentationAssetMissingError,
    list_frameworks,
)


def _write_index(root: Path, payload: dict[str, object]) -> Path:
    cache = root / "documentation-cache" / "index"
    cache.mkdir(parents=True, exist_ok=True)
    index = cache / "index.json"
    index.write_text(json.dumps(payload), encoding="utf-8")
    return index


def test_list_frameworks_returns_sorted_unique_names(tmp_path: Path) -> None:
    root = tmp_path / "AppleDeveloperDocumentation"
    _write_index(
        root,
        {
            "interfaceLanguages": {
                "data": [
                    {"path": "/documentation/swiftui/view", "title": "View", "type": "symbol"},
                    {"path": "/documentation/SwiftData/ModelContainer", "title": "ModelContainer", "type": "symbol"},
                    {"path": "/documentation/swiftui/navigationstack", "title": "NavigationStack", "type": "symbol"},
                ]
            }
        },
    )

    frameworks = list_frameworks(root)

    assert frameworks == ["SwiftData", "swiftui"]


def test_list_frameworks_skips_nodes_without_path(tmp_path: Path) -> None:
    root = tmp_path / "AppleDeveloperDocumentation"
    _write_index(
        root,
        {
            "interfaceLanguages": {
                "data": [
                    {"title": "App Frameworks", "type": "groupMarker"},
                    {
                        "children": [
                            {"path": "/documentation/foundation/data", "title": "Data", "type": "symbol"}
                        ]
                    },
                ]
            }
        },
    )

    assert list_frameworks(root) == ["foundation"]


def test_list_frameworks_raises_when_index_is_absent(tmp_path: Path) -> None:
    root = tmp_path / "AppleDeveloperDocumentation"
    root.mkdir()

    with pytest.raises(DocumentationAssetMissingError):
        list_frameworks(root)


def test_asset_root_constant_is_the_documented_location() -> None:
    assert Path(
        "/System/Library/AssetsV2/com_apple_MobileAsset_AppleDeveloperDocumentation"
    ) == ASSET_ROOT