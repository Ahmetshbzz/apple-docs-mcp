#!/usr/bin/env python3
"""Ranking quality, measured against ground truth instead of taste.

Two query sets:

* **identifier** — sampled from the corpus itself. The query is a page's own
  title and the expected answer is that page, so the question "does the engine
  find the API it was named after" has an objective answer.
* **natural language** — hand-written questions with the page that answers them,
  verified to exist in the corpus before anything is scored. This is the set the
  bridge's semantic ranker should win; reporting it is the point.

Both engines answer the same queries. Recall@k counts a hit when the expected
page appears in the first k results.

    uv run python tools/evaluate.py [--identifier 40]

The bridge runs only when Xcode answers; without it, the script reports the
offline numbers alone.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, "src")

from apple_docs_mcp.bridge import BridgeError, search_docs  # noqa: E402
from apple_docs_mcp.connections import shared  # noqa: E402
from apple_docs_mcp.corpus import find_documentation_db  # noqa: E402
from apple_docs_mcp.index import default_cache_dir, ensure_index, open_index  # noqa: E402
from apple_docs_mcp.service import search  # noqa: E402

# A question usually has several acceptable answers, and judging only the page
# the question writer had in mind would punish an equally good one. Each entry
# was judged against the pooled results of both engines (see ``pool``); the first
# URI is the page the question was written against.
NATURAL_LANGUAGE: list[tuple[str, tuple[str, ...]]] = [
    ("how do I download a file from the internet and show progress",
     ("/documentation/Foundation/downloading-files-from-websites",)),
    ("how do I observe changes in a SwiftUI view model",
     ("/documentation/SwiftUI/Managing-model-data-in-your-app",
      "/documentation/SwiftUI/Model-data",
      "/documentation/SwiftUI/Monitoring-model-data-changes-in-your-app",
      "/documentation/SwiftUI/ObservedObject")),
    ("how do I add inheritance to my SwiftData model",
     ("/documentation/SwiftData/Adopting-inheritance-in-SwiftData",
      "/documentation/SwiftData/Schema/Entity",
      "/documentation/CoreData/adopting-swiftdata-for-a-core-data-app")),
    ("how do I configure an audio session for playback",
     ("/documentation/AVFAudio/AVAudioSession",
      "/documentation/AVFoundation/configuring-your-app-for-media-playback",
      "/documentation/WatchKit/playing-background-audio")),
    ("how do I create a compute pipeline state in Metal",
     ("/documentation/Metal/MTLComputePipelineState",
      "/documentation/Metal/pipeline-state-creation",
      "/documentation/Metal/compute-passes",
      "/documentation/Metal/performing-calculations-on-a-gpu")),
    ("how do I play haptic feedback",
     ("/documentation/ApplePencil/playing-haptic-feedback-in-your-app",
      "/documentation/AppKit/sound-speech-and-haptics",
      "/documentation/WatchKit/WKInterfaceDevice",
      "/documentation/UIKit/UIFeedbackGenerator")),
    ("how do I schedule a background task to run later",
     ("/documentation/BackgroundTasks/BGTaskScheduler",
      "/documentation/BackgroundTasks/performing-long-running-tasks-on-ios-and-ipados",
      "/documentation/BackgroundTasks/choosing-background-strategies-for-your-app",
      "/documentation/UIKit/using-background-tasks-to-update-your-app",
      "/documentation/WatchKit/using-background-tasks")),
    ("how do I store a secret securely",
     ("/documentation/Security/using-the-keychain-to-manage-user-secrets",
      "/documentation/Security/storing-keys-in-the-keychain",
      "/documentation/Security/keychain-services")),
    ("how do I add a widget to my app",
     ("/documentation/WidgetKit/Creating-a-Widget-Extension",
      "/documentation/WidgetKit",
      "/documentation/SwiftUI/Widget")),
    ("how do I write a test with swift testing",
     ("/documentation/Testing",
      "/tutorials/Develop-in-Swift/Add-functionality-with-Swift-Testing")),
    ("how do I fetch data from a REST API with async await",
     ("/documentation/Foundation/URLSession",
      "/documentation/Foundation/URLSessionTask")),
    ("how do I get the user's location",
     ("/documentation/CoreLocation/CLLocationManager",
      "/documentation/CoreLocation/configuring-your-app-to-use-location-services",
      "/documentation/CoreLocation/CLLocation",
      "/documentation/CoreLocation")),
]

KS = (1, 3, 5)


@dataclass(frozen=True, slots=True)
class Score:
    """Recall at each k, plus timing, for one engine over one query set."""

    engine: str
    queries: int
    recall: dict[int, int]
    latencies: list[float]

    def rate(self, k: int) -> float:
        return self.recall[k] / self.queries if self.queries else 0.0

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0


def audit_ground_truth(
    queries: list[tuple[str, tuple[str, ...]]], k: int = 5
) -> None:
    """Check the judged sets for writer bias.

    The questions were written by the same person judging the pooled results, so
    the acceptable sets could quietly favour one engine. This prints where each
    accepted page actually came from: our pool, the bridge's, or both.
    """
    from apple_docs_mcp.bridge import search_docs as call_bridge

    ours_only = theirs_only = shared = unreachable = 0
    for query, acceptable in queries:
        mine = {normalise(hit.uri) for hit in search(query, limit=k, mode="offline").hits}
        try:
            theirs = {normalise(document.uri) for document in call_bridge(query)[:k]}
        except BridgeError:
            theirs = set()
        for entry in acceptable:
            wanted = normalise(entry)
            in_mine, in_theirs = wanted in mine, wanted in theirs
            if in_mine and in_theirs:
                shared += 1
            elif in_mine:
                ours_only += 1
            elif in_theirs:
                theirs_only += 1
            else:
                unreachable += 1

    print("\n=== ground-truth audit ===")
    print(f"accepted pages found by both engines : {shared}")
    print(f"accepted pages only our pool returned: {ours_only}")
    print(f"accepted pages only the bridge returned: {theirs_only}")
    print(f"accepted pages neither engine returned: {unreachable}")
    print(
        "  (the last row is the pooling blind spot: a page nobody returned was "
        "never judged, so both engines look better than they are)"
    )


def identifier_queries(limit: int, *, wrap: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """Sample symbol pages and ask for each one by its own title.

    With ``wrap``, the name arrives inside a sentence the way a person would type
    it, which is a harder and fairer test than the bare title.
    """
    db_path = find_documentation_db()
    if db_path is None:
        raise SystemExit("the documentation corpus is not installed")
    state = ensure_index(db_path, default_cache_dir())

    connection = shared(state.path, open_index)
    rows = connection.execute(
        """
        select uri, title from docs
        where kind = 'symbol' and uri not like '%#%' and title <> ''
          and title not like '%:%' and length(title) > 6
        order by uri
        """
    ).fetchall()

    total = len(rows)
    if total == 0:
        raise SystemExit("no symbol pages to sample")
    stride = max(total // limit, 1)
    sampled = rows[::stride][:limit]
    if wrap:
        return [
            (f"I want to use {title} in my app, how does it work", (str(uri),))
            for uri, title in sampled
        ]
    return [(str(title), (str(uri),)) for uri, title in sampled]


def normalise(uri: str) -> str:
    """Drop a section anchor: a section is answered by the page that holds it."""
    return uri.split("#", 1)[0]


def is_relevant(uri: str, acceptable: tuple[str, ...]) -> bool:
    candidate = normalise(uri)
    return any(candidate == normalise(entry) for entry in acceptable)


def evaluate_offline(queries: list[tuple[str, tuple[str, ...]]], k: int) -> Score:
    hits = {key: 0 for key in KS}
    latencies: list[float] = []
    for query, acceptable in queries:
        started = time.perf_counter()
        outcome = search(query, limit=k, mode="offline")
        latencies.append((time.perf_counter() - started) * 1000)
        uris = [hit.uri for hit in outcome.hits]
        for key in KS:
            if any(is_relevant(uri, acceptable) for uri in uris[:key]):
                hits[key] += 1
    return Score("offline", len(queries), hits, latencies)


def evaluate_bridge(queries: list[tuple[str, tuple[str, ...]]], k: int) -> Score | None:
    hits = {key: 0 for key in KS}
    latencies: list[float] = []
    for query, acceptable in queries:
        started = time.perf_counter()
        try:
            documents = search_docs(query)
        except BridgeError as error:
            print(f"  bridge stopped: {str(error).splitlines()[0]}", file=sys.stderr)
            return None
        latencies.append((time.perf_counter() - started) * 1000)
        uris = [document.uri for document in documents[:k]]
        for key in KS:
            if any(is_relevant(uri, acceptable) for uri in uris[:key]):
                hits[key] += 1
    return Score("bridge", len(queries), hits, latencies)


def evaluate_mode(name: str, queries: list[tuple[str, tuple[str, ...]]], k: int) -> Score | None:
    """Score one mode: the fused path, or the automatic choice between paths."""
    hits = {key: 0 for key in KS}
    latencies: list[float] = []
    for query, acceptable in queries:
        started = time.perf_counter()
        try:
            outcome = search(query, limit=k, mode=name)
        except BridgeError as error:
            print(f"  bridge stopped: {str(error).splitlines()[0]}", file=sys.stderr)
            return None
        latencies.append((time.perf_counter() - started) * 1000)
        uris = [hit.uri for hit in outcome.hits]
        for key in KS:
            if any(is_relevant(uri, acceptable) for uri in uris[:key]):
                hits[key] += 1
    return Score(name, len(queries), hits, latencies)


def report(name: str, scores: list[Score]) -> None:
    print(f"\n=== {name} (n={scores[0].queries}) ===")
    header = (
        f"{'engine':10} " + " ".join(f"@{k:<7}" for k in KS) + f"{'mean ms':>10}{'median ms':>11}"
    )
    print(header)
    for score in scores:
        cells = " ".join(f"{score.rate(k) * 100:6.1f}%" for k in KS)
        print(f"{score.engine:10} {cells} {score.mean_ms:9.1f} {score.median_ms:10.1f}")


def pool(queries: list[tuple[str, tuple[str, ...]]], db_path: Path, k: int = 5) -> None:
    """Print what both engines return, so relevance can be judged, not assumed.

    A single expected URI per question would punish an equally good page that the
    question writer did not have in mind. Pooling both engines' results and
    judging them is how ranked retrieval is evaluated without that bias.
    """
    from apple_docs_mcp.corpus import get_documents

    for query, acceptable in queries:
        print(f"\n### {query}\n    (written against {acceptable[0]})")
        offline = [hit.uri for hit in search(query, limit=k).hits]
        try:
            bridge = [document.uri for document in search_docs(query)[:k]]
        except BridgeError as error:
            print(f"    bridge unavailable: {error}")
            bridge = []
        pooled = list(dict.fromkeys(offline + bridge))
        pages = get_documents(db_path, pooled)
        for uri in pooled:
            page = pages.get(uri)
            title = page.title if page is not None else "(not in corpus)"
            marks = []
            if uri in offline:
                marks.append("offline")
            if uri in bridge:
                marks.append("bridge")
            print(f"    [{'+'.join(marks):15}] {uri}")
            print(f"                      {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identifier", type=int, default=40, help="identifier queries to sample")
    parser.add_argument("--pool", action="store_true", help="print pooled results for judging")
    arguments = parser.parse_args()

    if arguments.pool:
        db_path = find_documentation_db()
        if db_path is None:
            raise SystemExit("the documentation corpus is not installed")
        pool(validate_natural_language(db_path), db_path)
        return 0

    db_path = find_documentation_db()
    if db_path is None:
        raise SystemExit("the documentation corpus is not installed")

    identifiers = identifier_queries(arguments.identifier)
    print(f"identifier queries sampled: {len(identifiers)}")
    for query, expected in identifiers[:5]:
        print(f"  {query}  ->  {expected}")

    natural = validate_natural_language(db_path)
    print(f"natural-language queries: {len(natural)}")

    audit_ground_truth(natural)

    sets = [
        ("identifier (bare title)", identifiers),
        ("identifier (in a sentence)", identifier_queries(arguments.identifier, wrap=True)),
        ("natural language", natural),
    ]
    for name, queries in sets:
        scores = [evaluate_offline(queries, 5)]
        bridge = evaluate_bridge(queries, 5)
        if bridge is not None:
            scores.append(bridge)
            for mode in ("hybrid", "auto"):
                fused = evaluate_mode(mode, queries, 5)
                if fused is not None:
                    scores.append(fused)
        report(name, scores)

    return 0


def validate_natural_language(db_path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Shout about any judged answer that is not in the corpus, then keep the rest."""
    from apple_docs_mcp.corpus import get_document

    kept: list[tuple[str, tuple[str, ...]]] = []
    for query, acceptable in NATURAL_LANGUAGE:
        present = tuple(uri for uri in acceptable if get_document(db_path, uri) is not None)
        missing = [uri for uri in acceptable if uri not in present]
        if missing:
            print(f"  WARNING: not in the corpus: {', '.join(missing)}", file=sys.stderr)
        kept.append((query, present or acceptable))
    return kept


if __name__ == "__main__":
    raise SystemExit(main())
