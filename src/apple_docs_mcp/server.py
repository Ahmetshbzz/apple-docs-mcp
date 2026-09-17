"""MCP server exposing Apple Developer Documentation to coding agents.

Answers come from Xcode's on-disk documentation corpus through the shared
service layer, so no Xcode process, bridge approval, or network call is needed
for search. Xcode's own semantic ranker stays available behind ``mode``: the
corpus is authoritative for text, the bridge for meaning.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Annotated, Final

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from apple_docs_mcp.bridge import BridgeError
from apple_docs_mcp.corpus import DocumentationDBMissingError
from apple_docs_mcp.responses import (
    document_payload,
    error_payload,
    frameworks_payload,
    index_payload,
    playbook_payload,
    search_payload,
    status_payload,
)
from apple_docs_mcp.service import (
    Settings,
    build,
    document,
    frameworks,
    playbook,
    prewarm,
    reference,
    search,
    status,
    texts_for,
)
from apple_docs_mcp.skill import SkillMissingError

__all__ = ["create_server", "main"]

DEFAULT_LIMIT: Final = 10

_HANDLED_ERRORS = (
    BridgeError,
    DocumentationDBMissingError,
    SkillMissingError,
    ValueError,
)


def create_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server with its four documentation tools."""
    server: MCPServer = MCPServer(
        name="apple-docs",
        instructions=(
            "Apple Developer Documentation, served from Xcode's on-disk corpus. "
            "Search runs offline and needs no Xcode process or approval. Call "
            "search_docs before writing or reviewing Apple-platform code when an API "
            "signature, availability window, or behavioral detail matters, then "
            "get_document for the whole page. Call doc_status first when a search "
            "fails."
        ),
    )

    @server.tool(
        name="search_docs",
        description=(
            "Search Apple Developer Documentation by meaning over the full local "
            "corpus (263k pages, offline, milliseconds). Returns ranked pages with "
            "their declaration, availability windows, and a text snippet. Use this "
            "before answering from memory about Swift or Apple platform APIs. Set "
            "include_full_text=true to get the whole page text for each hit."
        ),
    )
    async def search_docs_tool(
        query: Annotated[str, Field(description="Natural-language or API search query.")],
        frameworks: Annotated[
            list[str] | None,
            Field(description="Restrict the search to these frameworks, e.g. ['SwiftData']."),
        ] = None,
        kinds: Annotated[
            list[str] | None,
            Field(description="Restrict to document kinds: symbol, article, tutorial."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum number of results to return.")
        ] = DEFAULT_LIMIT,
        mode: Annotated[
            str,
            Field(description="offline (default, no Xcode), semantic (Xcode), or hybrid."),
        ] = "offline",
        include_full_text: Annotated[
            bool, Field(description="Include each hit's full page text, not just a snippet.")
        ] = False,
    ) -> str:
        try:
            outcome = await asyncio.to_thread(
                search,
                query,
                frameworks=frameworks,
                kinds=kinds,
                limit=limit,
                mode=mode,
                settings=settings,
            )
            texts = (
                await asyncio.to_thread(texts_for, outcome.hits, settings=settings)
                if include_full_text
                else None
            )
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        return search_payload(outcome, texts)

    @server.tool(
        name="get_document",
        description=(
            "Return one whole documentation page by URI, including prose, code "
            "examples, declaration, and availability. Offline."
        ),
    )
    async def get_document_tool(
        uri: Annotated[str, Field(description="Page URI, e.g. /documentation/SwiftUI/View.")],
        max_chars: Annotated[
            int, Field(description="Truncate contents to this many characters; 0 returns all.")
        ] = 0,
    ) -> str:
        try:
            record = await asyncio.to_thread(document, uri, settings=settings)
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        if record is None:
            return error_payload(
                ValueError(f"No documentation page is stored at {uri!r}. Check the URI.")
            )
        return document_payload(record, max_chars=max_chars or None)

    @server.tool(
        name="list_frameworks",
        description=(
            "List every framework in the local corpus with its page count. Runs "
            "offline and needs no Xcode approval."
        ),
    )
    async def list_frameworks_tool() -> str:
        try:
            names = await asyncio.to_thread(frameworks, settings=settings)
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        return frameworks_payload(names)

    @server.tool(
        name="doc_status",
        description=(
            "Report whether documentation search is usable right now: corpus presence, "
            "index state, and whether Xcode's bridge answers."
        ),
    )
    async def doc_status_tool() -> str:
        report = await asyncio.to_thread(status, settings=settings)
        return status_payload(report)

    @server.tool(
        name="swift_playbook",
        description=(
            "The Swift/Apple-platform engineering playbook: what to do, what never "
            "to do, and which reference file answers an architecture question. Pair "
            "it with search_docs for signatures and availability, which the playbook "
            "deliberately does not carry. Pass a topic to read one reference file "
            "(topics are listed in the playbook's reference map)."
        ),
    )
    async def swift_playbook_tool(
        topic: Annotated[
            str | None,
            Field(description="Reference topic, e.g. 'swiftui'. Omit for the playbook itself."),
        ] = None,
    ) -> str:
        try:
            book = await asyncio.to_thread(playbook, settings=settings)
            text = (
                await asyncio.to_thread(reference, topic, settings=settings)
                if topic
                else book.body
            )
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        return playbook_payload(book, text, topic=topic)

    @server.resource(
        "swift://playbook",
        name="swift-playbook",
        title="Swift engineering playbook",
        mime_type="text/markdown",
    )
    async def swift_playbook_resource() -> str:
        book = await asyncio.to_thread(playbook, settings=settings)
        return book.body

    @server.resource(
        "swift://reference/{topic}",
        name="swift-reference",
        title="Swift playbook reference",
        mime_type="text/markdown",
    )
    async def swift_reference_resource(topic: str) -> str:
        return await asyncio.to_thread(reference, topic, settings=settings)

    @server.prompt(
        name="swift-playbook",
        title="Swift engineering playbook",
        description="Prime a session with the Swift/Apple-platform rules before writing code.",
    )
    async def swift_playbook_prompt() -> str:
        book = await asyncio.to_thread(playbook, settings=settings)
        return (
            f"{book.body}\n\n"
            "Use the apple-docs search_docs and get_document tools for signatures, "
            "availability, and code examples — do not answer those from memory."
        )

    @server.tool(
        name="build_index",
        description=(
            "Build or rebuild the local search index from the corpus. Needed once per "
            "corpus version; search does this automatically on first use."
        ),
    )
    async def build_index_tool(
        force: Annotated[bool, Field(description="Rebuild even when the index is fresh.")] = False,
    ) -> str:
        try:
            started = time.perf_counter()
            info = await asyncio.to_thread(build, force=force, settings=settings)
        except _HANDLED_ERRORS as error:
            return error_payload(error)
        return index_payload(info.path, info.documents, info.rebuilt, time.perf_counter() - started)

    return server


def main() -> None:
    # The first search otherwise pays for index pages the OS has not cached yet.
    # The warm-up is best effort: if it fails, the search that follows reports
    # the real error, and an unexpected failure here stays visible on stderr.
    threading.Thread(target=prewarm, daemon=True).start()
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
