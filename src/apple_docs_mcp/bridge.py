"""Client for Xcode's MCP bridge, serving Apple Developer Documentation search.

Xcode 27 exposes a local MCP service (``mcpbridge``) whose ``DocumentationSearch``
tool answers from the on-disk documentation asset. That asset's prose is not
readable in place, so this bridge is the only local source of full document text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "BRIDGE_PATH",
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeoutError",
    "Document",
    "NotApprovedError",
    "XcodeUnavailableError",
    "map_error_frame",
    "parse_search_result",
    "search_docs",
]

BRIDGE_PATH: Final = "/Applications/Xcode.app/Contents/Developer/usr/bin/mcpbridge"

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
INITIALIZE_TIMEOUT_SECONDS: Final = 10.0

UNAPPROVED_MARKER: Final = "isn't approved to use Xcode's tools"
WORKSPACE_GUIDANCE: Final = "XcodeOpenWorkspace"


class BridgeError(RuntimeError):
    """Base class for bridge failures."""


class XcodeUnavailableError(BridgeError):
    """Xcode or its MCP bridge is not available on this machine."""


class NotApprovedError(BridgeError):
    """The calling process is not approved to use Xcode's tools."""


class BridgeTimeoutError(BridgeError):
    """The bridge did not answer within the allotted budget."""


class BridgeProtocolError(BridgeError):
    """The bridge returned a frame this client cannot interpret."""


@dataclass(frozen=True)
class Document:
    """One documentation entry returned by ``DocumentationSearch``."""

    title: str
    uri: str
    contents: str
    score: float
    kind: str


def _extract_documents(payload: Any) -> list[Document] | None:
    if not isinstance(payload, dict):
        return None
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        return None

    documents: list[Document] = []
    for entry in raw_documents:
        if not isinstance(entry, dict):
            continue
        documents.append(
            Document(
                title=str(entry.get("title", "")),
                uri=str(entry.get("uri", "")),
                contents=str(entry.get("contents", "")),
                score=float(entry.get("score", 0.0)),
                kind=str(entry.get("kind", "")),
            )
        )
    return documents


def map_error_frame(frame: dict[str, Any]) -> BridgeError | None:
    """Translate an error frame into a typed error, or ``None`` when it succeeded."""
    result = frame.get("result")
    if not isinstance(result, dict) or not result.get("isError"):
        return None

    texts = [
        str(block.get("text", ""))
        for block in result.get("content", [])
        if isinstance(block, dict)
    ]
    message = "\n".join(text for text in texts if text) or "Xcode returned an error"

    if UNAPPROVED_MARKER in message:
        return NotApprovedError(
            f"{message}\n\nApprove this process in Xcode by opening a workspace: "
            f"`xcrun mcp-server open <path-to-project>`, then accept the dialog. "
            f"Approval is granted per process and expires after 24 hours."
        )
    return BridgeProtocolError(message)


def parse_search_result(frame: dict[str, Any]) -> list[Document]:
    """Return documents from a ``DocumentationSearch`` response frame."""
    error = map_error_frame(frame)
    if error is not None:
        raise error

    result = frame.get("result")
    if not isinstance(result, dict):
        raise BridgeProtocolError(f"Response has no result object: {frame!r}")

    structured = result.get("structuredContent")
    documents = _extract_documents(structured)
    if documents is not None:
        return documents

    for block in result.get("content", []):
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            documents = _extract_documents(json.loads(text))
        except json.JSONDecodeError:
            continue
        if documents is not None:
            return documents

    raise BridgeProtocolError(f"Response carries no documents: {frame!r}")


class _BridgeSession:
    """A live ``mcpbridge`` subprocess speaking newline-delimited JSON-RPC."""

    def __init__(self, bridge_path: str, timeout: float) -> None:
        self._timeout = timeout
        self._stderr_lines: list[str] = []
        try:
            self._process = subprocess.Popen(
                [bridge_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise XcodeUnavailableError(f"Could not start Xcode's MCP bridge: {error}") from error
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_lines.append(line.rstrip())

    def send(self, message: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise BridgeProtocolError("Bridge stdin is closed")
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def read_response(self, request_id: int, timeout: float) -> dict[str, Any]:
        assert self._process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                detail = "; ".join(self._stderr_lines[-3:]) or "no stderr output"
                raise XcodeUnavailableError(
                    f"Xcode's MCP bridge exited early (code {self._process.returncode}): {detail}"
                )
            line = self._process.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and frame.get("id") == request_id:
                return frame
        raise BridgeTimeoutError(f"Bridge did not answer within {timeout:.0f}s")

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


def search_docs(
    query: str,
    frameworks: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    bridge_path: str | None = None,
) -> list[Document]:
    """Search the local Apple Developer Documentation index.

    Raises a typed :class:`BridgeError` subclass on every failure; never returns
    an empty list to signal an error.
    """
    if not query.strip():
        raise ValueError("query must not be empty")

    path = bridge_path or BRIDGE_PATH
    if not shutil.which(path) and not __import__("os").path.exists(path):
        raise XcodeUnavailableError(
            f"Xcode's MCP bridge was not found at {path}. Install Xcode or run `xcode-select`."
        )

    session = _BridgeSession(path, timeout)
    try:
        session.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "apple-docs-mcp", "version": "0.1.0"},
                },
            }
        )
        session.read_response(1, INITIALIZE_TIMEOUT_SECONDS)
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        arguments: dict[str, Any] = {"query": query}
        if frameworks:
            arguments["frameworks"] = list(frameworks)

        session.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "DocumentationSearch", "arguments": arguments},
            }
        )
        frame = session.read_response(2, timeout)
        return parse_search_result(frame)
    finally:
        session.close()
