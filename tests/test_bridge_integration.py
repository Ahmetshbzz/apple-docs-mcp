"""Integration tests against the real Xcode MCP bridge.

These exercise the live bridge and are skipped when Xcode is unavailable. They
are the only tests that prove the end-to-end path works; the unit tests prove
parsing and error mapping.
"""

from __future__ import annotations

import pytest

from apple_docs_mcp.bridge import (
    BRIDGE_PATH,
    NotApprovedError,
    XcodeUnavailableError,
    search_docs,
)

pytestmark = pytest.mark.integration


def _bridge_present() -> bool:
    import os

    return os.path.exists(BRIDGE_PATH)


@pytest.mark.skipif(not _bridge_present(), reason="Xcode's MCP bridge is not installed")
def test_search_returns_full_documents_for_a_query() -> None:
    try:
        documents = search_docs("SwiftData model inheritance", frameworks=["SwiftData"])
    except (NotApprovedError, XcodeUnavailableError) as error:
        pytest.skip(f"bridge not usable in this environment: {error}")

    assert documents, "search returned no documents"
    assert any(document.contents.strip() for document in documents)
    assert all(document.uri.startswith("/documentation/") for document in documents)


@pytest.mark.skipif(not _bridge_present(), reason="Xcode's MCP bridge is not installed")
def test_search_rejects_an_empty_query_without_touching_the_bridge() -> None:
    with pytest.raises(ValueError):
        search_docs("   ")