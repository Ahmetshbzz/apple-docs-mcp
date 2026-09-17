"""Offline framework inventory from Xcode's local documentation asset.

The asset's ``index.json`` is the only part of the documentation bundle whose
text is readable in place. It carries navigation nodes (``path``, ``title``,
``type``) but no prose, which makes it useful for discovery and useless for
answering API questions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

__all__ = [
    "ASSET_ROOT",
    "DocumentationAssetMissingError",
    "find_asset_root",
    "find_index_path",
    "list_frameworks",
]

ASSET_ROOT: Final = Path(
    "/System/Library/AssetsV2/com_apple_MobileAsset_AppleDeveloperDocumentation"
)

FRAMEWORK_PATTERN: Final = re.compile(r"/documentation/([A-Za-z0-9_.-]+)")
INDEX_RELATIVE_PATH: Final = Path("documentation-cache") / "index" / "index.json"
ASSET_BUNDLE_GLOB: Final = "*.asset/AssetData/" + INDEX_RELATIVE_PATH.as_posix()


class DocumentationAssetMissingError(RuntimeError):
    """The local Apple Developer Documentation asset is not installed."""


def find_index_path(search_root: Path = ASSET_ROOT) -> Path | None:
    """Return the documentation ``index.json``, or ``None`` when not installed.

    The installed layout nests the payload under a content-addressed bundle::

        <root>/<uuid>.asset/AssetData/documentation-cache/index/index.json

    A flat root that holds the index directly is also accepted, which keeps
    synthetic fixtures and future layout changes working.
    """
    if not search_root.is_dir():
        return None

    direct = search_root / INDEX_RELATIVE_PATH
    if direct.is_file():
        return direct

    for candidate in sorted(search_root.glob(ASSET_BUNDLE_GLOB)):
        if candidate.is_file():
            return candidate
    return None


def find_asset_root(search_root: Path = ASSET_ROOT) -> Path | None:
    """Return the root that holds the documentation index, or ``None``."""
    if find_index_path(search_root) is None:
        return None
    return search_root


def _iter_paths(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        path = node.get("path")
        if isinstance(path, str):
            yield path
        for value in node.values():
            yield from _iter_paths(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_paths(item)


def list_frameworks(asset_root: Path | None = None) -> list[str]:
    """Return the sorted framework names present in the local index."""
    root = asset_root if asset_root is not None else ASSET_ROOT
    index_path = find_index_path(root)
    if index_path is None:
        raise DocumentationAssetMissingError(
            "Apple Developer Documentation asset is not installed. "
            "Download Developer Documentation from Xcode's Components settings."
        )

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentationAssetMissingError(
            f"Could not read documentation index: {error}"
        ) from error

    return sorted(
        {match for path in _iter_paths(payload) for match in FRAMEWORK_PATTERN.findall(path)}
    )