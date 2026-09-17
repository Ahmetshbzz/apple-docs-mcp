"""MCP server exposing Apple Developer Documentation to coding agents.

Thin transport over :mod:`apple_docs_mcp.responses`: this module validates
arguments and shapes tool definitions, and delegates every answer to the bridge
or the offline index. It never converts a failure into an empty success, because
an empty result is indistinguishable from "this API does not exist".
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from apple_docs_mcp.bridge import (
    BRIDGE_PATH,
    BridgeError,
    Document,
    search_docs,
)
from apple_docs_mcp.frameworks import (
    DocumentationAssetMissingError,
    find_asset_root,
    list_frameworks,
)
from apple_docs_mcp.responses import (
    error_payload,
    frameworks_payload,
    search_payload,
    status_payload,
)

__all__ = ["PROBE_QUERY", "create_server", "main"]

PROBE_QUERY = "SwiftUI View"
PROBE_TIMEOUT_SECONDS = 20.0
DEFAULT_TIMEOUT_SECONDS = 30.0

_HANDLED_ERRORS = (
    BridgeError,
    DocumentationAssetMissingError,
    ValueError,
)


def create_server() -> MCPServer:
    """Build the MCP server with its three documentation tools."""
    server: MCPServer = MCPServer(
        name="apple-docs",
        instructions=(
            "Apple Developer Documentation, served from Xcode's on-disk index. "
            "Call search_docs before writing or reviewing Apple-platform code when an "
            "API signature, availability window, or behavioral detail matters. "
            "Call doc_status first when a search fails."
        ),
    )

    @server.tool(
        name="search_docs",
        description=(
            "Search Apple Developer Documentation by meaning, using Xcode's local "
            "documentation index. Returns full document text including code examples, "
            "with each result's uri and match score. Use this before answering from "
            "memory about Swift or Apple platform APIs."
        ),
    )
    async def search_docs_tool(
        query: Annotated[str, Field(description="Natural-language or API search query.")],
        frameworks: Annotated[
            list[str] | None,
            Field(description="Restrict the search to these frameworks, e.g. ['SwiftData']."),
        ] = None,
        timeout: Annotated[
            float,
            Field(description="Seconds to wait for the bridge before failing."),
        ] = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        if not query.strip():
            return error_payload(ValueError("a non-empty query is required"))
        try:
            documents: list[Document] = await asyncio.to_thread(
                search_docs, query, frameworks, timeout
            )
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        return search_payload(documents)

    @server.tool(
        name="list_frameworks",
        description=(
            "List every framework in Xcode's local documentation index. Runs offline "
            "and needs no Xcode approval."
        ),
    )
    async def list_frameworks_tool() -> str:
        try:
            names: list[str] = await asyncio.to_thread(list_frameworks)
        except DocumentationAssetMissingError as error:
            return error_payload(error)
        return frameworks_payload(names)

    @server.tool(
        name="doc_status",
        description=(
            "Report whether documentation search is usable right now: asset presence, "
            "bridge path, and whether this process is approved to use Xcode's tools."
        ),
    )
    async def doc_status_tool() -> str:
        asset_path = await asyncio.to_thread(find_asset_root)
        try:
            documents: list[Document] | None = await asyncio.to_thread(
                search_docs, PROBE_QUERY, None, PROBE_TIMEOUT_SECONDS
            )
            error: BaseException | None = None
        except _HANDLED_ERRORS as probe_error:
            documents = None
            error = probe_error
        payload = status_payload(asset_path, documents, error)
        return _with_bridge_path(payload)

    return server


def _with_bridge_path(payload: str) -> str:
    """Add the bridge path to a status payload without re-serialising twice."""
    import json

    body: dict[str, Any] = json.loads(payload)
    body["bridgePath"] = BRIDGE_PATH
    return json.dumps(body, ensure_ascii=False)


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()