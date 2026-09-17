"""Command-line interface over the documentation service."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from enum import IntEnum
from typing import Final

from apple_docs_mcp.bridge import (
    BridgeProtocolError,
    BridgeTimeoutError,
    NotApprovedError,
    XcodeUnavailableError,
)
from apple_docs_mcp.corpus import DocumentationDBMissingError, DocumentRecord
from apple_docs_mcp.index import IndexInfo
from apple_docs_mcp.responses import (
    document_payload,
    frameworks_payload,
    index_payload,
    search_payload,
    status_payload,
)
from apple_docs_mcp.service import MODES, SearchOutcome, StatusReport, texts_for
from apple_docs_mcp.service import build as build_index
from apple_docs_mcp.service import document as get_page
from apple_docs_mcp.service import frameworks as list_frameworks
from apple_docs_mcp.service import search as run_search
from apple_docs_mcp.service import status as run_status

__all__ = ["ExitCode", "main"]

DEFAULT_LIMIT: Final = 10


class ExitCode(IntEnum):
    """Process exit codes; each failure mode is distinguishable."""

    SUCCESS = 0
    USAGE = 1
    NOT_APPROVED = 2
    XCODE_UNAVAILABLE = 3
    TIMEOUT = 4
    PROTOCOL = 5
    ASSET_MISSING = 6
    NOT_FOUND = 7


_EXCEPTION_EXIT_CODES: Final[tuple[tuple[type[BaseException], ExitCode], ...]] = (
    (NotApprovedError, ExitCode.NOT_APPROVED),
    (XcodeUnavailableError, ExitCode.XCODE_UNAVAILABLE),
    (BridgeTimeoutError, ExitCode.TIMEOUT),
    (BridgeProtocolError, ExitCode.PROTOCOL),
    (DocumentationDBMissingError, ExitCode.ASSET_MISSING),
)


def format_hits(outcome: SearchOutcome, texts: dict[str, str]) -> str:
    """Render search results for a terminal reader."""
    if not outcome.hits:
        return "No documents matched this query."

    blocks: list[str] = []
    for position, hit in enumerate(outcome.hits, start=1):
        lines = [f"{position}. [{hit.score:.2f}] {hit.framework or '-'} · {hit.kind} · {hit.title}"]
        lines.append(f"   {hit.uri} · source: {hit.source}")
        if hit.declaration:
            lines.append(f"   {hit.declaration.splitlines()[0]}")
        if hit.availability:
            lines.append(f"   available: {', '.join(hit.availability)}")
        full = texts.get(hit.uri)
        lines.append(full.strip() if full else hit.snippet)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_document(record: DocumentRecord) -> str:
    """Render one whole page for a terminal reader."""
    lines = [f"{record.title}", f"{record.uri}", f"{record.framework} · {record.kind}"]
    if record.declaration:
        lines.append(f"declaration: {record.declaration}")
    if record.availability:
        lines.append(f"available: {', '.join(record.availability)}")
    return "\n".join(lines) + "\n\n" + record.content.strip()


def format_status(report: StatusReport) -> str:
    """Render service state for a terminal reader."""
    lines = [
        f"corpus: {report.asset_path if report.asset_path else 'not installed'}",
        f"index: {_format_index(report.index)}",
        f"bridge command: {' '.join(report.bridge_command)}",
        f"bridge: {'available' if report.bridge_documents is not None else 'unavailable'}",
    ]
    if report.bridge_error:
        lines.append(f"bridge detail: {report.bridge_error}")
    return "\n".join(lines)


def _format_index(info: IndexInfo | None) -> str:
    if info is None:
        return "not built (search builds it on first use)"
    state = "fresh" if info.fresh else "stale"
    return f"{info.path} ({info.documents} pages, {state})"


def format_index(info: IndexInfo, seconds: float) -> str:
    return (
        f"index: {info.path}\n"
        f"pages: {info.documents}\n"
        f"rebuilt: {'yes' if info.rebuilt else 'no'}\n"
        f"seconds: {seconds:.1f}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-docs",
        description="Search Apple Developer Documentation from Xcode's local corpus.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search the documentation corpus")
    search.add_argument("query", nargs="?", help="Natural-language or API query")
    search.add_argument("--framework", action="append", dest="frameworks", default=None)
    search.add_argument("--kind", action="append", dest="kinds", default=None)
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    search.add_argument("--mode", choices=MODES, default="offline")
    search.add_argument("--full", action="store_true", dest="full_text")
    search.add_argument("--json", action="store_true", dest="as_json")

    page = subparsers.add_parser("get", help="Print one whole documentation page")
    page.add_argument("uri", help="Page URI, e.g. /documentation/SwiftUI/View")
    page.add_argument("--max-chars", type=int, default=0)
    page.add_argument("--json", action="store_true", dest="as_json")

    frameworks = subparsers.add_parser("frameworks", help="List frameworks in the corpus")
    frameworks.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="Report corpus, index, and bridge state")
    status.add_argument("--json", action="store_true", dest="as_json")

    index = subparsers.add_parser("index", help="Build or rebuild the local search index")
    index.add_argument("--rebuild", action="store_true")
    index.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _report_status(as_json: bool) -> int:
    report = run_status()
    print(status_payload(report) if as_json else format_status(report))
    return ExitCode.SUCCESS if report.searchable else ExitCode.XCODE_UNAVAILABLE


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "frameworks":
            names = list_frameworks()
            print(frameworks_payload(names) if args.as_json else _format_frameworks(names))
            return ExitCode.SUCCESS

        if args.command == "status":
            return _report_status(args.as_json)

        if args.command == "index":
            started = time.perf_counter()
            info = build_index(force=args.rebuild)
            seconds = time.perf_counter() - started
            if args.as_json:
                print(index_payload(info.path, info.documents, info.rebuilt, seconds))
            else:
                print(format_index(info, seconds))
            return ExitCode.SUCCESS

        if args.command == "get":
            record = get_page(args.uri)
            if record is None:
                print(f"error: no documentation page is stored at {args.uri!r}", file=sys.stderr)
                return ExitCode.NOT_FOUND
            print(
                document_payload(record, max_chars=args.max_chars or None)
                if args.as_json
                else format_document(record)
            )
            return ExitCode.SUCCESS

        if not args.query or not args.query.strip():
            print("error: a non-empty query is required", file=sys.stderr)
            return ExitCode.USAGE

        outcome = run_search(
            args.query,
            frameworks=args.frameworks,
            kinds=args.kinds,
            limit=args.limit,
            mode=args.mode,
        )
        texts = texts_for(outcome.hits) if args.full_text else {}
        print(search_payload(outcome, texts) if args.as_json else format_hits(outcome, texts))
        return ExitCode.SUCCESS
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return ExitCode.USAGE
    except BaseException as error:  # noqa: BLE001 - mapped to a typed exit code below
        for exception_type, exit_code in _EXCEPTION_EXIT_CODES:
            if isinstance(error, exception_type):
                print(f"error: {error}", file=sys.stderr)
                return exit_code
        raise


def _format_frameworks(names: list[tuple[str, int]]) -> str:
    return "\n".join(
        f"{name}\t{count}" if count else name for name, count in names
    )


if __name__ == "__main__":
    raise SystemExit(main())
