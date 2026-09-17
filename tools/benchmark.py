#!/usr/bin/env python3
"""Measure this server against Xcode's DocumentationSearch on the same queries.

The README's comparison table comes from this script; run it on your own machine
before believing any of those numbers here.

    uv run python tools/benchmark.py [--queries N]

Both paths answer from the same local corpus. The bridge path needs Xcode
running with an approved workspace; when it is unavailable the script reports
that and still measures the offline path.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

sys.path.insert(0, "src")

from apple_docs_mcp.bridge import BridgeError, search_docs  # noqa: E402
from apple_docs_mcp.service import search  # noqa: E402

QUERIES = [
    "SwiftData model inheritance",
    "AVAudioSession category",
    "observe changes in a SwiftUI view model",
    "URLSession async download with progress",
    "Metal compute pipeline state",
    "how do I make a haptic feedback",
]

LIMIT = 5


def measure_offline(query: str) -> tuple[float, list[str]]:
    started = time.perf_counter()
    outcome = search(query, limit=LIMIT)
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, [hit.title for hit in outcome.hits]


def measure_bridge(query: str) -> tuple[float, list[str], list[int]]:
    started = time.perf_counter()
    documents = search_docs(query)
    elapsed = (time.perf_counter() - started) * 1000
    sizes = [len(document.contents) for document in documents]
    return elapsed, [document.title for document in documents], sizes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=len(QUERIES))
    arguments = parser.parse_args()
    queries = QUERIES[: arguments.queries]

    offline_times: list[float] = []
    bridge_times: list[float] = []
    bridge_sizes: list[int] = []
    bridge_available = True
    first_call_ms: float | None = None

    for query in queries:
        offline_ms, offline_titles = measure_offline(query)
        if first_call_ms is None:
            first_call_ms = offline_ms
        else:
            offline_times.append(offline_ms)
        print(f"\n[{query}]\n  offline {offline_ms:7.0f} ms")
        for title in offline_titles[:3]:
            print(f"    {title[:80]}")

        if not bridge_available:
            continue

        try:
            bridge_ms, bridge_titles, sizes = measure_bridge(query)
        except BridgeError as error:
            print(f"  bridge unavailable: {str(error).splitlines()[0]}")
            bridge_available = False
            continue

        bridge_times.append(bridge_ms)
        bridge_sizes.extend(sizes)
        print(f"  bridge  {bridge_ms:7.0f} ms, {len(sizes)} documents")
        for title in bridge_titles[:3]:
            print(f"    {title[:80]}")

    print("\n--- summary ---")
    print(
        "The first search in a fresh process reads index pages the OS has not "
        "cached yet; report it separately instead of averaging it away."
    )
    if first_call_ms is not None:
        print(f"offline first call: {first_call_ms:.0f} ms (cold page cache)")
    if offline_times:
        print(
            f"offline steady:     mean {statistics.mean(offline_times):.0f} ms, "
            f"median {statistics.median(offline_times):.0f} ms, n={len(offline_times)}"
        )
    if bridge_times and offline_times:
        print(
            f"bridge:             mean {statistics.mean(bridge_times):.0f} ms, "
            f"median {statistics.median(bridge_times):.0f} ms, n={len(bridge_times)}"
        )
        speedup = statistics.mean(bridge_times) / statistics.mean(offline_times)
        print(f"speedup (steady):   {speedup:.1f}x")
        if bridge_sizes:
            small = sum(1 for size in bridge_sizes if size < 100)
            print(
                f"bridge text per document: min {min(bridge_sizes)}, "
                f"median {int(statistics.median(bridge_sizes))}, max {max(bridge_sizes)}; "
                f"{small}/{len(bridge_sizes)} under 100 characters"
            )
    else:
        print("bridge: not measured (Xcode not answering)")

    print("\nindex:", json.dumps({"documents": search("SwiftUI View", limit=1).index.documents}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
