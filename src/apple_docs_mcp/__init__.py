"""Offline Apple Developer Documentation access through Xcode's MCP bridge."""

from __future__ import annotations

__all__ = [
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeoutError",
    "Document",
    "NotApprovedError",
    "XcodeUnavailableError",
    "search_docs",
]

from apple_docs_mcp.bridge import (
    BridgeError,
    BridgeProtocolError,
    BridgeTimeoutError,
    Document,
    NotApprovedError,
    XcodeUnavailableError,
    search_docs,
)
