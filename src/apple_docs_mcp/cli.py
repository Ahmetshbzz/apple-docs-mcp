"""Command-line interface over the documentation bridge and the offline index."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from enum import IntEnum
from typing import Final

from apple_docs_mcp.bridge import (
    BRIDGE_PATH,
    BridgeProtocolError,
    BridgeTimeoutError,
    Document,
    NotApprovedError,
    XcodeUnavailableError,
    search_docs,
)
from apple_docs_mcp.frameworks import (
    DocumentationAssetMissingError,
    find_asset_root,
    list_frameworks,
)
from apple_docs_mcp.responses import search_payload

__all__ = ["EXIT_CODES", "ExitCode", "format_documents", "main"]

PROBE_QUERY: Final = "SwiftUI View"
PROBE_TIMEOUT_SECONDS: Final = 20.0


class ExitCode(IntEnum):
    """Process exit codes; each failure mode is distinguishable."""

    SUCCESS = 0
    USAGE = 1
    NOT_APPROVED = 2
    XCODE_UNAVAILABLE = 3
    TIMEOUT = 4
    PROTOCOL = 5
    ASSET_MISSING = 6


EXIT_CODES: Final[dict[str, int]] = {
    "success": ExitCode.SUCCESS,
    "usage": ExitCode.USAGE,
    "not_approved": ExitCode.NOT_APPROVED,
    "xcode_unavailable": ExitCode.XCODE_UNAVAILABLE,
    "timeout": ExitCode.TIMEOUT,
    "protocol": ExitCode.PROTOCOL,
    "asset_missing": ExitCode.ASSET_MISSING,
}

_EXCEPTION_EXIT_CODES: Final[tuple[tuple[type[BaseException], ExitCode], ...]] = (
    (NotApprovedError, ExitCode.NOT_APPROVED),
    (XcodeUnavailableError, ExitCode.XCODE_UNAVAILABLE),
    (BridgeTimeoutError, ExitCode.TIMEOUT),
    (BridgeProtocolError, ExitCode.PROTOCOL),
    (DocumentationAssetMissingError, ExitCode.ASSET_MISSING),
)


def format_documents(documents: list[Document]) -> str:
    """Render documents for a terminal reader."""
    if not documents:
        return "No documents matched this query."

    blocks: list[str] = []
    for document in documents:
        header = f"[{document.score:.2f}] {document.kind}: {document.title}"
        blocks.append(f"{header}\n{document.uri}\n\n{document.contents.strip()}")
    return "\n\n" + ("\n\n" + "-" * 72 + "\n\n").join(blocks)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-docs",
        description="Search Apple Developer Documentation from Xcode's local index.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search the documentation index")
    search.add_argument("query", nargs="?", help="Natural-language or API query")
    search.add_argument("--framework", action="append", dest="frameworks", default=None)
    search.add_argument("--timeout", type=float, default=30.0)
    search.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("frameworks", help="List frameworks in the local index")
    subparsers.add_parser("status", help="Report bridge and asset availability")
    return parser


def _report_status() -> int:
    asset = find_asset_root()
    print(f"documentation asset: {asset if asset else 'not installed'}")
    print(f"bridge path: {BRIDGE_PATH}")
    try:
        documents = search_docs(PROBE_QUERY, timeout=PROBE_TIMEOUT_SECONDS)
    except NotApprovedError:
        print("bridge status: not approved (open a workspace in Xcode to approve)")
        return ExitCode.NOT_APPROVED
    except XcodeUnavailableError as error:
        print(f"bridge status: unavailable ({error})")
        return ExitCode.XCODE_UNAVAILABLE
    print(f"bridge status: available ({len(documents)} documents for a probe query)")
    return ExitCode.SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "frameworks":
            for framework in list_frameworks():
                print(framework)
            return ExitCode.SUCCESS

        if args.command == "status":
            return _report_status()

        if not args.query or not args.query.strip():
            print("error: a non-empty query is required", file=sys.stderr)
            return ExitCode.USAGE

        documents = search_docs(args.query, frameworks=args.frameworks, timeout=args.timeout)
        if args.as_json:
            print(search_payload(documents))
        else:
            print(format_documents(documents))
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


if __name__ == "__main__":
    raise SystemExit(main())