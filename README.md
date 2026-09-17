# apple-docs-mcp

An MCP server and CLI that answer Apple Developer Documentation questions from
the documentation corpus Xcode already has on disk — no network, no Xcode
process, no approval dialog.

Coding agents recall Apple APIs from training data that lags the installed SDK.
This server hands them the real text: complete pages with their code listings,
the declaration, and the availability windows.

## Measured against Xcode's own `DocumentationSearch`

Xcode 27 exposes an MCP tool, `DocumentationSearch`, that answers from the same
corpus through `mcpbridge`. It is a good tool, and this project uses it — but
only when its semantic ranking is wanted. The two paths measured on this machine
(6 queries, same session, 2026-09-17):

| | Xcode `DocumentationSearch` | this server, `mode: offline` |
|---|---|---|
| Requires | Xcode running, workspace open, per-process approval, 24 h expiry | nothing |
| Latency, mean of 6 queries | 264–357 ms across runs | **4–43 ms**, 10 ms in the MCP server path |
| Latency, single page fetch | not available | 2 ms |
| Corpus reachable | the 20 documents the tool returns | 263,513 pages, searchable |
| Text per result | fragment, 46–5,305 chars (median 437) | the whole page (up to 375,908 chars) |
| Fields per result | title, uri, contents, score, kind | plus declaration, availability, framework, kind, USR |
| Index build | n/a | 263,513 pages in 5.3 s, 128 MB |

Reproduce all of it on your own machine — `uv run python tools/benchmark.py`. The
offline figures move with the OS page cache: 4 ms warm, 43 ms when the first
query lands on uncached index pages, 10 ms mean over six queries in a running
MCP server.

Judged query by query on top three results, the offline ranking was better on
four of those six queries, a wash on one, and worse on one ("how do I make a
haptic feedback" — the bridge found the conceptual article, the offline pass
found the API). The reason is visible in the numbers: **39 of the 118 documents
the bridge returned across those queries are under 100 characters**, because it
ranks short "…: Relationships" stubs highly, and a stub cannot answer a
question. That is also why `mode: semantic` and `mode: hybrid` exist.

Two things this project does **not** claim. It does not reproduce Xcode's
semantic ranker offline: the stored page vectors are 512-dimensional but live in
a space `NLContextualEmbedding` does not reproduce (measured cosine ≈ 0.01–0.03
for the *same* page text). And it is not a better search engine than Xcode's —
it is the same corpus with different plumbing.

## Install

Requires macOS and Xcode with the Developer Documentation asset installed.
[uv](https://docs.astral.sh/uv/) for the toolchain.

```bash
git clone https://github.com/Ahmetshbzz/apple-docs-mcp
cd apple-docs-mcp
uv sync
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
| `search_docs` | Ranked pages with declaration, availability, and a snippet. Offline by default; `mode` picks the semantic or hybrid path |
| `get_document` | One whole page by URI: prose, code listings, declaration, availability |
| `list_frameworks` | Every framework with its page count (375 frameworks) |
| `doc_status` | Corpus, index, and bridge state, with the remedy when something is missing |
| `swift_playbook` | The Swift/Apple-platform engineering playbook and its reference files |
| `build_index` | Build or rebuild the derived index (search builds it automatically on first use) |

The playbook is also served as the `swift://playbook` resource, the
`swift://reference/{topic}` resource template, and the `swift-playbook` prompt.

### Modes

| Mode | What answers | Measured latency | Xcode needed |
|---|---|---|---|
| `offline` (default) | the local corpus index | 3–23 ms in the MCP server | no |
| `semantic` | Xcode's bridge | 185–739 ms | yes, approved |
| `hybrid` | both, fused by reciprocal rank | 313–434 ms | no (falls back to offline) |

`hybrid` labels each hit `local`, `bridge`, or `hybrid`, so a caller can see
where an answer came from. If the bridge fails in `hybrid`, the offline results
are returned with a `bridgeError` note instead of an error.

## CLI

```bash
apple-docs search "SwiftData model inheritance" --limit 5
apple-docs search "AVAudioSession category" --framework AVFAudio --json --full
apple-docs get /documentation/SwiftUI/View
apple-docs frameworks
apple-docs status
apple-docs index --rebuild
```

Exit codes are distinct per failure so scripts can branch:
`0` success, `1` usage, `2` not approved, `3` Xcode unavailable, `4` timeout,
`5` protocol, `6` asset missing, `7` page not found.

## Where the text comes from

Xcode's Developer Documentation asset keeps every page as a JSON document in a
SQLite database:

```
/System/Library/AssetsV2/com_apple_MobileAsset_AppleDeveloperDocumentation/
  <uuid>.asset/AssetData/documentation-db/index.sql
```

`documents` holds one JSON row per page — `uri`, `title`, `framework`, `kind`,
`content` (prose and code listings), `symbol` (identity and USR), `platforms`
(availability) — and `observations` holds precomputed 512-dimensional vectors.
The neighbouring `documentation-cache/` is a different thing: an HTTP-style blob
store (`cache.db` refs into `fs/<n>` segments) for images and cached payloads.

The server derives a full-text index from `documents` and leaves the corpus
alone: it is opened read-only and immutable, and never written to.

### The derived index

- Built once per corpus version, in `~/Library/Caches/apple-docs-mcp/`.
- 128 MB for 263,513 pages; 5.3 s to build; atomic swap, so an interrupted build
  never leaves a half-index.
- Contentless FTS5 — the index holds tokens, and page text is read back from the
  corpus for the handful of pages a query returns. That is why it is 128 MB and
  not 500 MB.
- Freshness is decided from the corpus file's size and mtime plus the index
  schema version.

## The Swift playbook

`swift_playbook` serves an authored engineering playbook — what to do, what never
to do, and which reference file answers an architecture question — while
`search_docs` supplies signatures and availability, which the playbook
deliberately does not carry. The playbook stays in its own repository and is read
in place; point `APPLE_DOCS_SWIFT_SKILL` at it. When it is absent, the tool says
where it looked instead of returning nothing.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `APPLE_DOCS_ASSET_ROOT` | the system asset root | Where the documentation asset lives |
| `APPLE_DOCS_CACHE_DIR` | `~/Library/Caches/apple-docs-mcp` | Where the derived index lives |
| `APPLE_DOCS_SWIFT_SKILL` | `~/Desktop/tools/claude-skills/swift` | Where the playbook lives |
| `APPLE_DOCS_BRIDGE_COMMAND` | `xcrun mcpbridge` | How to launch Xcode's bridge |

## Limitations

- Ranking is BM25 with column weights plus an identifier boost. It is not
  semantic; use `semantic` or `hybrid` when meaning matters more than terms.
- The corpus is written by Xcode, so a page can be a section rather than an
  article ("…: Relationships"). Such sections are short by construction.
- Xcode can purge the asset (`…xml.purged` appears next to it). If the corpus
  disappears, searches fail with a remedy rather than returning nothing.
- Attribute rows in `index.sql` carry `framework`, `title`, and `type` but no
  text; the text lives in `documents`. The server reads `documents`.

## Development

```bash
uv run pytest tests/ -q                    # 125 tests, no Xcode needed
uv run pytest tests/ -m integration        # live bridge tests
uv run python tools/benchmark.py           # the comparison table above
uv run ruff check src tests
```

Unit tests build synthetic corpus and skill fixtures, so they run anywhere. The
integration tests exercise the live bridge and skip themselves when it is
unavailable. The MCP surface itself is tested over a real stdio transport in
`tests/test_server_surface.py`.

## Verified environment

Measured 2026-09-17, not assumed:

| Thing | Value |
|---|---|
| Xcode | 27.0 (27A266a) |
| Swift | 6.4 (swiftlang-6.4.0.34.1) |
| Documentation corpus | 263,513 pages, 375 frameworks, ~375 MB of text |
| Derived index | 128 MB, built in 5.3 s |

## License

MIT — see [LICENSE](LICENSE).
