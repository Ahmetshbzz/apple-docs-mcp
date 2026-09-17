from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

DOCUMENT_SCHEMA = """
CREATE TABLE documents (asset_id TEXT PRIMARY KEY, document BLOB) STRICT;
"""

CACHE_SCHEMA = """
CREATE TABLE metadata (row_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value BLOB NOT NULL, UNIQUE(key) ON CONFLICT REPLACE);
CREATE TABLE refs (row_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, data_id INTEGER NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, UNIQUE(uuid) ON CONFLICT REPLACE);
CREATE TABLE data (row_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, data BLOB, is_compressed INTEGER NOT NULL);
"""


def document(
    uri: str,
    title: str,
    framework: str,
    content: str,
    *,
    kind: str = "symbol",
    role: str = "",
    parent_uri: str = "",
    symbol: dict[str, Any] | None = None,
    platforms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one synthetic documentation page in the shape Xcode stores."""
    return {
        "uri": uri,
        "title": title,
        "framework": framework,
        "kind": kind,
        "role": role,
        "parentUri": parent_uri,
        "content": content,
        "content_hash": "",
        "symbol": json.dumps(symbol) if symbol is not None else None,
        "platforms": json.dumps(platforms) if platforms is not None else None,
        "modules": None,
        "external_id": None,
        "fileName": None,
        "roleHeading": None,
    }


def build_asset(root: Path, documents: list[dict[str, Any]]) -> Path:
    """Create a synthetic documentation asset and return its index.sql path."""
    data = root / "fixture.asset" / "AssetData"
    db_dir = data / "documentation-db"
    db_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = data / "documentation-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "index" / "index.json").parent.mkdir(parents=True, exist_ok=True)
    (cache_dir / "index" / "index.json").write_text(
        json.dumps({"interfaceLanguages": {"data": []}}), encoding="utf-8"
    )

    path = db_dir / "index.sql"
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(DOCUMENT_SCHEMA)
    connection.executemany(
        "insert into documents values (?, ?)",
        [(doc["uri"], json.dumps(doc).encode("utf-8")) for doc in documents],
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def asset_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_documents() -> list[dict[str, Any]]:
    return [
        document(
            "/documentation/SwiftData/ModelContainer",
            "ModelContainer",
            "SwiftData",
            "ModelContainer\nClass of SwiftData\nAn object that manages the storage and "
            "object graph for your data model.\n\n```\n@MainActor class ModelContainer\n```\n\n"
            "Overview\nCreate a container with a schema and configuration.",
            role="symbol",
            symbol={"preciseIdentifier": "s:8SwiftData14ModelContainerC", "kind": "Class"},
            platforms=[{"introduced": 17, "platform": "iOS", "deprecated": False}],
        ),
        document(
            "/documentation/SwiftData/Adopting-inheritance-in-SwiftData",
            "Adopting inheritance in SwiftData",
            "SwiftData",
            "Adopting inheritance in SwiftData\nArticle\nDetermine whether inheritance is "
            "right for your data model, and how to implement it.",
            kind="article",
        ),
        document(
            "/documentation/AVFAudio/AVAudioSession/Category-swift.struct",
            "AVAudioSession.Category",
            "AVFAudio",
            "AVAudioSession.Category\nStructure of AVFAudio\nA category that describes the "
            "audio behavior of your app.\n\n```\nstruct Category\n```\n\n"
            "Overview\nSelect a category such as playback or record.",
            role="symbol",
            platforms=[{"introduced": 6, "platform": "iOS", "deprecated": False}],
        ),
    ]


@pytest.fixture
def asset_with_documents(asset_root: Path, sample_documents: list[dict[str, Any]]) -> Path:
    build_asset(asset_root, sample_documents)
    return asset_root


SKILL_TEXT = """---
name: swift
description: Use when building or reviewing Apple-platform Swift code.
---

# Swift playbook

Never invent an API.

- `swiftui.md` — state and data flow.
"""


@pytest.fixture
def skill_directory(tmp_path: Path) -> Path:
    """A synthetic Swift skill directory, in the shape the real one has."""
    root = tmp_path / "claude-skills" / "swift"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    (root / "references" / "swiftui.md").write_text("state and data flow", encoding="utf-8")
    return root


@pytest.fixture
def indexed_asset(asset_with_documents: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic asset plus its derived index. Returns ``(db_path, index_file)``."""
    from apple_docs_mcp.corpus import find_documentation_db
    from apple_docs_mcp.index import build_index

    db_path = find_documentation_db(asset_with_documents)
    assert db_path is not None
    info = build_index(db_path, tmp_path / "cache")
    return db_path, info.path
