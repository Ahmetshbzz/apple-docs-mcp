"""JSON payload builders shared by the MCP server and the CLI.

Kept separate from transport so the response contract is testable without a
live bridge, and so both interfaces produce identical payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from apple_docs_mcp.bridge import (
    BridgeProtocolError,
    BridgeTimeoutError,
    Document,
    NotApprovedError,
    XcodeUnavailableError,
)
from apple_docs_mcp.frameworks import DocumentationAssetMissingError

__all__ = [
    "error_payload",
    "frameworks_payload",
    "search_payload",
    "status_payload",
]

_REMEDIES: Final[dict[type[BaseException], str]] = {
    NotApprovedError: (
        "Approval is granted per process and expires after 24 hours. Open a workspace "
        "in Xcode (or run `xcrun mcp-server open <path-to-project>`) and accept the "
        "approval dialog for this interpreter, then retry."
    ),
    XcodeUnavailableError: (
        "Ensure Xcode is installed, that `xcode-select -p` points at it, and that "
        "Xcode is running."
    ),
    BridgeTimeoutError: (
        "The bridge did not answer in time. Retry with a larger `timeout`; the first "
        "call after Xcode launches can be slow."
    ),
    BridgeProtocolError: (
        "Xcode returned an unexpected frame. Report the message verbatim; it is not a "
        "documentation miss."
    ),
    DocumentationAssetMissingError: (
        "Download Developer Documentation from Xcode: Settings > Components > "
        "Developer Documentation."
    ),
}


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def search_payload(documents: list[Document]) -> str:
    """Serialise search results. An empty list is data, not a failure."""
    return _dump(
        {
            "documents": [
                {
                    "title": document.title,
                    "uri": document.uri,
                    "contents": document.contents,
                    "score": document.score,
                    "kind": document.kind,
                }
                for document in documents
            ]
        }
    )


def frameworks_payload(frameworks: list[str]) -> str:
    return _dump({"frameworks": frameworks})


def error_payload(error: BaseException) -> str:
    """Serialise a failure with the remedy that matches its class."""
    remedy = _REMEDIES.get(type(error), "Unexpected failure; report it verbatim.")
    return _dump({"error": type(error).__name__, "message": str(error), "remedy": remedy})


def _approval_state(error: BaseException | None) -> bool | None:
    """``True`` approved, ``False`` refused, ``None`` undetermined.

    Xcode unavailable means the question never reached Xcode, so approval is
    unknown rather than negative.
    """
    if error is None:
        return True
    if isinstance(error, NotApprovedError):
        return False
    if isinstance(error, XcodeUnavailableError):
        return None
    return True


def status_payload(
    asset_path: Path | None,
    documents: list[Document] | None,
    error: BaseException | None,
) -> str:
    """Report whether documentation search is usable right now."""
    payload: dict[str, object] = {
        "assetInstalled": asset_path is not None,
        "assetPath": str(asset_path) if asset_path else None,
        "searchable": error is None,
        "approved": _approval_state(error),
    }
    if error is None:
        payload["probeDocumentCount"] = len(documents or [])
    else:
        payload["detail"] = str(error).splitlines()[0]
        payload["remedy"] = _REMEDIES.get(
            type(error), "Unexpected failure; report it verbatim."
        )
    return _dump(payload)