# apple-docs-mcp

An MCP server and CLI that search Apple Developer Documentation from Xcode's
on-disk documentation index — fully offline, no network calls.

Coding agents recall Apple APIs from training data that lags the installed SDK.
This server gives them the real text: signatures, availability, and code
examples, straight from the documentation that Xcode already downloaded.

## Why this exists

Xcode installs a local copy of the Apple Developer Documentation asset. It looks
like a source an agent could read directly, but the prose is not readable in
place:

- `index/index.json` (104 MB) holds navigation nodes — `path`, `title`, `type` —
  and no prose. The keys `abstract`, `declarationFragments`, and
  `primaryContentSections` appear zero times.
- `cache.db` stores 1269 compressed blobs whose header (`9b21f31f`) decodes under
  none of LZ4, LZFSE, zlib, LZMA, or LZBITMAP via Apple's `libcompression`.
- `documentation-db/index.sql` (1.2 GB) is a vector store for Xcode's own
  semantic search, not text.

Xcode 27 exposes an MCP service with a `DocumentationSearch` tool that answers
from this asset. Measured behavior: 19–20 documents per query with full
contents and code examples, ~0.4 s median latency, and **zero bytes** of network
traffic. This project wraps that tool so any MCP client can use it, and adds an
offline framework index that needs no approval at all.

## Install

Requires macOS, Xcode installed, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Ahmetshbzz/apple-docs-mcp
cd apple-docs-mcp
uv sync
```

Install the CLI on your `PATH`:

```bash
uv tool install --editable .
```

## Register with an MCP client

Claude Code:

```bash
claude mcp add apple-docs --scope user -- \
  uv run --quiet --project /path/to/apple-docs-mcp apple-docs-mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.apple-docs]
command = "uv"
args = ["run", "--quiet", "--project", "/path/to/apple-docs-mcp", "apple-docs-mcp"]
startup_timeout_sec = 60
```

## Tools

| Tool | Purpose |
|---|---|
| `search_docs` | Search documentation by meaning; returns full text, code examples, `uri`, and score per result |
| `list_frameworks` | List every framework in the local index (~395). Offline, no approval needed |
| `doc_status` | Report whether search is usable right now, and why not if it is not |

## CLI

```bash
apple-docs search "SwiftData model inheritance" --framework SwiftData
apple-docs search "AVAudioSession category" --json
apple-docs frameworks
apple-docs status
```

Exit codes are distinct per failure so scripts can branch:
`0` success, `1` usage, `2` not approved, `3` Xcode unavailable, `4` timeout,
`5` protocol, `6` asset missing.

## The approval dependency

This is the one thing to understand before using it.

`search_docs` drives `mcpbridge`, so **Xcode must be running with an approved
workspace**:

```bash
xcrun mcp-server open <path-to-project>   # accept the dialog in Xcode
```

- Approval is granted **per process**, not per user. A different interpreter
  needs its own approval.
- Approval **expires after 24 hours**.
- Approval requires an open workspace; the documentation search itself is global,
  so any project works.

Every failure returns a typed error with a remedy. The tool never returns an
empty list to signal a problem, because an empty result is indistinguishable
from "this API does not exist" — the exact confusion it exists to prevent.

`list_frameworks` reads `index.json` directly and needs none of this.

## Development

```bash
uv run pytest tests/                 # 37 tests
uv run pytest tests/ -m integration  # live bridge tests
uv run ruff check src tests
```

Unit tests need no Xcode. Integration tests skip themselves when the bridge is
absent or unapproved.

### Architecture

One implementation, two interfaces. Business logic is not duplicated.

```
src/apple_docs_mcp/
├── bridge.py      # mcpbridge JSON-RPC client; owns the protocol and typed errors
├── frameworks.py  # offline index.json reader
├── responses.py   # shared JSON payload contract
├── cli.py         # argparse adapter
└── server.py      # MCP adapter
```

## Verified environment

Measured 2026-09-17, not assumed:

| Thing | Value |
|---|---|
| Xcode | 27.0 (27A266a) |
| Swift | 6.4 (swiftlang-6.4.0.34.1) |
| Simulator runtime | iOS 27.0 (24A434) |
| Documentation asset | Build 10M13950, ~1.6 GB, ~395 frameworks |

## License

MIT — see [LICENSE](LICENSE).
