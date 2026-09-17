"""JSON payload builders shared by the MCP server and the CLI.

Kept separate from transport so the response contract is testable without a
live corpus, and so both interfaces produce identical payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from apple_docs_mcp.bridge import (
    BridgeProtocolError,
    BridgeTimeoutError,
    NotApprovedError,
    XcodeUnavailableError,
)
from apple_docs_mcp.corpus import DocumentationDBMissingError, DocumentRecord
from apple_docs_mcp.search import Hit
from apple_docs_mcp.service import SearchOutcome, StatusReport
from apple_docs_mcp.skill import Playbook, SkillMissingError

__all__ = [
    "document_payload",
    "error_payload",
    "frameworks_payload",
    "playbook_payload",
    "search_payload",
    "status_payload",
]

_REMEDIES: Final[dict[type[BaseException], str]] = {
    NotApprovedError: (
        "Approval is granted per process and expires after 24 hours. Open a workspace "
        "in Xcode (or run `xcrun mcp-server open <path-to-project>`) and accept the "
        "approval dialog for this interpreter, then retry. Offline search needs none "
        "of this."
    ),
    XcodeUnavailableError: (
        "Xcode is not answering. Offline search does not need it; only `semantic` "
        "and `hybrid` modes do."
    ),
    BridgeTimeoutError: (
        "The bridge did not answer in time. Retry with a larger `timeout`; the first "
        "call after Xcode launches can be slow."
    ),
    BridgeProtocolError: (
        "Xcode returned an unexpected frame. Report the message verbatim; it is not a "
        "documentation miss."
    ),
    DocumentationDBMissingError: (
        "Download Developer Documentation from Xcode: Settings > Components > "
        "Developer Documentation."
    ),
    SkillMissingError: (
        "The Swift playbook is a separate repository. Point APPLE_DOCS_SWIFT_SKILL "
        "at the skill directory that holds SKILL.md, or ask for a documentation "
        "search instead."
    ),
}


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _hit_payload(hit: Hit, *, full_text: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": hit.title,
        "uri": hit.uri,
        "framework": hit.framework,
        "kind": hit.kind,
        "score": hit.score,
        "source": hit.source,
        "declaration": hit.declaration,
        "availability": hit.availability,
        "snippet": hit.snippet,
    }
    if full_text is not None:
        payload["contents"] = full_text
    return payload


def search_payload(outcome: SearchOutcome, texts: dict[str, str] | None = None) -> str:
    """Serialise search results. An empty list is data, not a failure."""
    documents = [
        _hit_payload(hit, full_text=(texts or {}).get(hit.uri)) for hit in outcome.hits
    ]
    payload: dict[str, object] = {
        "documents": documents,
        "mode": outcome.mode,
        "count": len(documents),
        "indexDocuments": outcome.index.documents if outcome.index else 0,
    }
    if outcome.bridge_error:
        payload["bridgeError"] = outcome.bridge_error
    return _dump(payload)


def document_payload(record: DocumentRecord, *, max_chars: int | None = None) -> str:
    """Serialise one whole page."""
    content = record.content
    truncated = False
    if max_chars is not None and max_chars > 0 and len(content) > max_chars:
        content = content[:max_chars]
        truncated = True

    return _dump(
        {
            "title": record.title,
            "uri": record.uri,
            "framework": record.framework,
            "kind": record.kind,
            "role": record.role,
            "parentUri": record.parent_uri,
            "declaration": record.declaration,
            "availability": record.availability,
            "symbolKind": record.symbol_kind,
            "preciseIdentifier": record.precise_identifier,
            "contents": content,
            "truncated": truncated,
        }
    )


def frameworks_payload(frameworks: list[tuple[str, int]]) -> str:
    return _dump(
        {
            "frameworks": [{"name": name, "documents": count} for name, count in frameworks],
            "count": len(frameworks),
        }
    )


def playbook_payload(playbook: Playbook, text: str, *, topic: str | None = None) -> str:
    """Serialise the Swift playbook or one of its reference files."""
    return _dump(
        {
            "name": playbook.name,
            "description": playbook.description,
            "topic": topic,
            "references": list(playbook.references),
            "root": str(playbook.root),
            "text": text,
        }
    )


def error_payload(error: BaseException) -> str:
    """Serialise a failure with the remedy that matches its class."""
    remedy = _REMEDIES.get(type(error), "Unexpected failure; report it verbatim.")
    return _dump({"error": type(error).__name__, "message": str(error), "remedy": remedy})


def status_payload(report: StatusReport) -> str:
    """Report whether documentation search is usable right now, and from where."""
    payload: dict[str, object] = {
        "assetInstalled": report.asset_path is not None,
        "assetPath": str(report.asset_path) if report.asset_path else None,
        "index": _index_payload(report),
        "bridgeCommand": " ".join(report.bridge_command),
        "bridgeAvailable": report.bridge_documents is not None,
        "searchable": report.searchable,
    }
    if report.bridge_error:
        payload["bridgeError"] = report.bridge_error
    return _dump(payload)


def _index_payload(report: StatusReport) -> dict[str, object] | None:
    if report.index is None:
        return None
    return {
        "path": str(report.index.path),
        "documents": report.index.documents,
        "fresh": report.index.fresh,
        "builtAt": report.index.built_at,
    }


def index_payload(path: Path, documents: int, rebuilt: bool, seconds: float) -> str:
    """Report an index build."""
    return _dump(
        {
            "path": str(path),
            "documents": documents,
            "rebuilt": rebuilt,
            "seconds": round(seconds, 2),
            "sizeBytes": path.stat().st_size if path.is_file() else 0,
        }
    )
