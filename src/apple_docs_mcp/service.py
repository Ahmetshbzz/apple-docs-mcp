"""Documentation service shared by the MCP tools and the CLI.

Both interfaces answer from this module, so a fix or a ranking change lands in
one place. The service owns three decisions:

* **which source answers** — the local corpus index by default, Xcode's bridge
  when its semantic order is wanted, both fused when the caller asks;
* **when the index is built** — lazily on first search, never during a status
  probe, so a diagnostic call stays fast and side-effect free;
* **how failures are reported** — typed errors that name the remedy, never an
  empty list, because an empty result must mean "no such page".
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from apple_docs_mcp import corpus
from apple_docs_mcp.bridge import (
    BRIDGE_COMMAND,
    BridgeError,
    Document,
)
from apple_docs_mcp.bridge import (
    search_docs as bridge_search,
)
from apple_docs_mcp.corpus import DocumentationDBMissingError, DocumentRecord
from apple_docs_mcp.index import (
    IndexInfo,
    default_cache_dir,
    ensure_index,
    index_info,
)
from apple_docs_mcp.search import Hit, bridge_hits, enrich_from_corpus, fuse_rrf, search_offline
from apple_docs_mcp.skill import Playbook, load_playbook, read_reference

__all__ = [
    "MODES",
    "SearchOutcome",
    "Settings",
    "StatusReport",
    "build",
    "document",
    "frameworks",
    "playbook",
    "reference",
    "search",
    "status",
    "texts_for",
]

MODES: Final = ("offline", "semantic", "hybrid")
BRIDGE_CANDIDATES: Final = 20


def default_asset_root() -> Path:
    """The corpus root, overridable for non-standard installs and tests."""
    override = os.environ.get("APPLE_DOCS_ASSET_ROOT")
    return Path(override) if override else corpus.ASSET_ROOT


def default_skill_root() -> Path:
    """Where the Swift playbook lives; empty-looking paths fail with a remedy."""
    override = os.environ.get("APPLE_DOCS_SWIFT_SKILL")
    if override:
        return Path(override)
    return Path.home() / "Desktop" / "tools" / "claude-skills" / "swift"


def default_bridge_command() -> tuple[str, ...]:
    """The bridge launcher, overridable when Xcode lives somewhere unusual."""
    override = os.environ.get("APPLE_DOCS_BRIDGE_COMMAND")
    if override:
        parts = shlex.split(override)
        if parts:
            return tuple(parts)
    return BRIDGE_COMMAND


@dataclass(frozen=True, slots=True)
class Settings:
    """Where the corpus, the derived index, and the bridge live."""

    asset_root: Path = field(default_factory=default_asset_root)
    cache_dir: Path = field(default_factory=default_cache_dir)
    bridge_command: tuple[str, ...] = field(default_factory=default_bridge_command)
    skill_root: Path = field(default_factory=default_skill_root)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Search results plus where they came from."""

    hits: list[Hit]
    mode: str
    index: IndexInfo | None
    bridge_error: str | None = None


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Everything a caller needs to explain why search does or does not work."""

    asset_path: Path | None
    index: IndexInfo | None
    bridge_command: tuple[str, ...]
    bridge_documents: int | None
    bridge_error: str | None

    @property
    def searchable(self) -> bool:
        return bool(self.index and self.index.usable) or self.bridge_documents is not None


def _settings(settings: Settings | None) -> Settings:
    """Resolve settings at call time so environment overrides always apply."""
    return settings if settings is not None else Settings()


def _corpus_path(settings: Settings) -> Path | None:
    return corpus.find_documentation_db(settings.asset_root)


def _ready(settings: Settings, *, force: bool = False) -> tuple[Path, IndexInfo]:
    db_path = _corpus_path(settings)
    if db_path is None:
        raise DocumentationDBMissingError(
            "Apple Developer Documentation is not installed. Download it from "
            "Xcode's Settings > Components > Developer Documentation."
        )
    return db_path, ensure_index(db_path, settings.cache_dir, force=force)


def _bridge_documents(
    query: str, frameworks: list[str] | None, settings: Settings
) -> list[Document]:
    return bridge_search(query, frameworks, command=settings.bridge_command)


def search(
    query: str,
    *,
    frameworks: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int = 10,
    mode: str = "offline",
    settings: Settings | None = None,
) -> SearchOutcome:
    """Answer a documentation question from the local corpus, the bridge, or both."""
    if not query.strip():
        raise ValueError("a non-empty query is required")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    resolved = _settings(settings)
    bridge_error: str | None = None
    bridge: list[Hit] = []
    index_state: IndexInfo | None = None

    if mode in ("semantic", "hybrid"):
        try:
            bridge = bridge_hits(_bridge_documents(query, frameworks, resolved))
        except BridgeError as error:
            if mode == "semantic":
                raise
            bridge_error = str(error).splitlines()[0]

    if mode == "semantic" and bridge:
        db_path = _corpus_path(resolved)
        if db_path is not None:
            bridge = enrich_from_corpus(db_path, bridge)
        return SearchOutcome(hits=bridge[:limit], mode=mode, index=index_state)
    if mode == "semantic" and not bridge:
        return SearchOutcome(hits=[], mode=mode, index=index_state, bridge_error=bridge_error)

    db_path, index_state = _ready(resolved)
    local = search_offline(
        db_path,
        index_state.path,
        query,
        frameworks=frameworks,
        kinds=kinds,
        limit=limit if mode == "offline" else BRIDGE_CANDIDATES,
    )

    if mode == "offline":
        return SearchOutcome(hits=local, mode=mode, index=index_state)

    fused = fuse_rrf(local, bridge, limit=limit)
    return SearchOutcome(
        hits=enrich_from_corpus(db_path, fused),
        mode=mode,
        index=index_state,
        bridge_error=bridge_error,
    )


def playbook(*, settings: Settings | None = None) -> Playbook:
    """Return the Swift engineering playbook authored beside this project."""
    return load_playbook(_settings(settings).skill_root)


def reference(topic: str, *, settings: Settings | None = None) -> str:
    """Return one playbook reference file by topic."""
    return read_reference(topic, _settings(settings).skill_root)


def texts_for(hits: list[Hit], *, settings: Settings | None = None) -> dict[str, str]:
    """Return whole page text for the given hits, keyed by URI."""
    resolved = _settings(settings)
    db_path = _corpus_path(resolved)
    if db_path is None or not hits:
        return {}
    pages = corpus.get_documents(db_path, [hit.uri for hit in hits])
    return {uri: record.content for uri, record in pages.items()}


def build(*, force: bool = False, settings: Settings | None = None) -> IndexInfo:
    """Build or rebuild the derived index, returning its state."""
    resolved = _settings(settings)
    _, state = _ready(resolved, force=force)
    return state


def document(uri: str, *, settings: Settings | None = None) -> DocumentRecord | None:
    """Return a whole page from the local corpus."""
    resolved = _settings(settings)
    db_path = _corpus_path(resolved)
    if db_path is None:
        raise DocumentationDBMissingError(
            "Apple Developer Documentation is not installed. Download it from "
            "Xcode's Settings > Components > Developer Documentation."
        )
    return corpus.get_document(db_path, uri)


def frameworks(*, settings: Settings | None = None) -> list[tuple[str, int]]:
    """List frameworks with page counts, from the index when it exists."""
    resolved = _settings(settings)
    db_path = _corpus_path(resolved)
    if db_path is None:
        raise DocumentationDBMissingError(
            "Apple Developer Documentation is not installed. Download it from "
            "Xcode's Settings > Components > Developer Documentation."
        )

    state = index_info(db_path, resolved.cache_dir)
    if state is not None and state.usable:
        from apple_docs_mcp.index import open_index

        connection = open_index(state.path)
        try:
            rows = connection.execute(
                "select framework, count(*) from docs group by framework order by 2 desc, 1"
            ).fetchall()
        finally:
            connection.close()
        return [(str(name), int(count)) for name, count in rows if str(name)]

    return [(name, 0) for name in corpus.list_framework_names(db_path)]


def status(*, probe_bridge: bool = True, settings: Settings | None = None) -> StatusReport:
    """Report corpus, index, and bridge state without building anything."""
    resolved = _settings(settings)
    db_path = _corpus_path(resolved)
    state = index_info(db_path, resolved.cache_dir) if db_path is not None else None

    bridge_documents: int | None = None
    bridge_error: str | None = None
    if probe_bridge:
        try:
            bridge_documents = len(_bridge_documents("SwiftUI View", None, resolved))
        except BridgeError as error:
            bridge_error = str(error).splitlines()[0]

    return StatusReport(
        asset_path=db_path,
        index=state,
        bridge_command=resolved.bridge_command,
        bridge_documents=bridge_documents,
        bridge_error=bridge_error,
    )
