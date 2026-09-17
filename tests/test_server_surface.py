"""Exercise the MCP surface over the real stdio transport.

These tests speak JSON-RPC to a spawned server pointed at synthetic fixtures, so
they check the part unit tests cannot: tool schemas, handler wiring, prompts,
and resources as a client actually sees them. No Xcode and no real corpus.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import build_asset, document

pytestmark = pytest.mark.usefixtures("server_session")

SESSION_TIMEOUT = 60.0


class ServerSession:
    """One spawned server process plus the request/response plumbing."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()

        assert self._process.stdout is not None
        deadline = time.monotonic() + SESSION_TIMEOUT
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if not line:
                time.sleep(0.02)
                continue
            frame = json.loads(line)
            if frame.get("id") == request_id:
                return frame
        raise AssertionError(f"no response to {method}")

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, dict | str]:
        """Call a tool and return ``(is_error, payload)``.

        This server reports failures as a structured payload carrying the remedy
        rather than as an ``isError`` result, so the payload is parsed either way
        and a failure is recognised by its ``error`` key.
        """
        frame = self.request("tools/call", {"name": name, "arguments": arguments})
        result = frame["result"]
        text = result["content"][0]["text"]
        try:
            return bool(result.get("isError")), json.loads(text)
        except json.JSONDecodeError:
            return True, text


@pytest.fixture(scope="module")
def server_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServerSession]:
    root = tmp_path_factory.mktemp("surface")
    asset_root = root / "asset"
    build_asset(
        asset_root,
        [
            document(
                "/documentation/SwiftData/ModelContainer",
                "ModelContainer",
                "SwiftData",
                "ModelContainer\nClass of SwiftData\nManages the storage and object graph.\n\n"
                "```\n@MainActor class ModelContainer\n```\n",
                symbol={"preciseIdentifier": "s:8SwiftData14ModelContainerC", "kind": "Class"},
                platforms=[{"introduced": 17, "platform": "iOS", "deprecated": False}],
            ),
            document(
                "/documentation/SwiftData/Adopting-inheritance",
                "Adopting inheritance in SwiftData",
                "SwiftData",
                "Article\nDetermine whether inheritance is right for your data model.",
                kind="article",
            ),
        ],
    )

    skill_root = root / "skill"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: swift\ndescription: Apple platform playbook.\n---\n\n# Swift playbook\n\n"
        "Never invent an API.\n",
        encoding="utf-8",
    )
    (skill_root / "references" / "swiftui.md").write_text("state and data flow", encoding="utf-8")

    environment = {
        **os.environ,
        "APPLE_DOCS_ASSET_ROOT": str(asset_root),
        "APPLE_DOCS_CACHE_DIR": str(root / "cache"),
        "APPLE_DOCS_SWIFT_SKILL": str(skill_root),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }

    process = subprocess.Popen(
        [sys.executable, "-c", "from apple_docs_mcp.server import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
    )
    session = ServerSession(process)
    session.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    yield session
    process.terminate()
    process.wait(timeout=10)


def test_tools_are_advertised(server_session: ServerSession) -> None:
    names = {tool["name"] for tool in server_session.request("tools/list")["result"]["tools"]}

    assert names == {
        "search_docs",
        "get_document",
        "list_frameworks",
        "doc_status",
        "swift_playbook",
        "build_index",
    }


def test_search_docs_answers_offline(server_session: ServerSession) -> None:
    failed, payload = server_session.call_tool("search_docs", {"query": "inheritance", "limit": 5})

    assert not failed
    assert isinstance(payload, dict)
    assert payload["mode"] == "offline"
    assert payload["documents"][0]["uri"] == "/documentation/SwiftData/Adopting-inheritance"


def test_search_docs_carries_declaration_and_availability(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("search_docs", {"query": "ModelContainer"})

    assert isinstance(payload, dict)
    container = next(doc for doc in payload["documents"] if doc["uri"].endswith("ModelContainer"))
    assert container["declaration"] == "@MainActor class ModelContainer"
    assert container["availability"] == ["iOS 17+"]


def test_get_document_returns_the_page(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool(
        "get_document", {"uri": "/documentation/SwiftData/ModelContainer"}
    )

    assert isinstance(payload, dict)
    assert "Manages the storage and object graph" in payload["contents"]


def test_get_document_reports_an_unknown_uri_as_an_error(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("get_document", {"uri": "/documentation/Absent"})

    assert isinstance(payload, dict)
    assert "No documentation page" in payload["message"]
    assert payload["remedy"]


def test_search_docs_rejects_an_unknown_mode(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("search_docs", {"query": "inheritance", "mode": "vibes"})

    assert isinstance(payload, dict)
    assert payload["error"] == "ValueError"
    assert "unknown mode" in payload["message"]


def test_list_frameworks_needs_no_build(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("list_frameworks", {})

    assert isinstance(payload, dict)
    assert {entry["name"] for entry in payload["frameworks"]} == {"SwiftData"}


def test_doc_status_reports_the_corpus(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("doc_status", {})

    assert isinstance(payload, dict)
    assert payload["assetInstalled"] is True
    assert payload["searchable"] is True


def test_build_index_reports_the_build(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("build_index", {})

    assert isinstance(payload, dict)
    assert payload["documents"] == 2


def test_swift_playbook_serves_the_skill(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("swift_playbook", {})

    assert isinstance(payload, dict)
    assert payload["name"] == "swift"
    assert payload["text"].startswith("# Swift playbook")
    assert payload["references"] == ["swiftui"]


def test_swift_playbook_serves_one_reference(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("swift_playbook", {"topic": "swiftui"})

    assert isinstance(payload, dict)
    assert payload["text"] == "state and data flow"


def test_swift_playbook_refuses_a_path_traversal(server_session: ServerSession) -> None:
    _, payload = server_session.call_tool("swift_playbook", {"topic": "../SKILL"})

    assert isinstance(payload, dict)
    assert "not a reference topic name" in payload["message"]


def test_playbook_is_available_as_a_resource(server_session: ServerSession) -> None:
    frame = server_session.request("resources/read", {"uri": "swift://playbook"})

    assert frame["result"]["contents"][0]["text"].startswith("# Swift playbook")


def test_reference_is_available_as_a_resource_template(server_session: ServerSession) -> None:
    frame = server_session.request("resources/read", {"uri": "swift://reference/swiftui"})

    assert frame["result"]["contents"][0]["text"] == "state and data flow"


def test_playbook_is_available_as_a_prompt(server_session: ServerSession) -> None:
    frame = server_session.request("prompts/get", {"name": "swift-playbook"})

    text = frame["result"]["messages"][0]["content"]["text"]
    assert "# Swift playbook" in text
    assert "search_docs" in text
