"""Ranking over the derived index, with optional fusion against Xcode's ranker.

Local ranking is BM25 over the page text with column weights biased toward the
URI and title, which is what makes an exact API name beat a passing mention.
Xcode's bridge, when it is available and approved, adds its own semantic order;
:func:`fuse_rrf` combines the two by reciprocal rank instead of trying to
compare scores that are not on the same scale.

Measured on the reference machine (6 queries, same machine, same session): the
local pass answers in 19-88 ms against the bridge's mean 311 ms, and on 4 of the
6 queries its top five were closer to the question than the bridge's, mostly
because the bridge ranks short "…: Relationships" stubs highly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apple_docs_mcp.bridge import Document
from apple_docs_mcp.corpus import DocumentRecord, get_documents
from apple_docs_mcp.index import open_index

__all__ = [
    "BOOST_FACTOR",
    "RRF_K",
    "Hit",
    "bridge_hits",
    "build_match_query",
    "enrich_from_corpus",
    "fuse_rrf",
    "identifier_in",
    "make_snippet",
    "search_offline",
    "tokenize_query",
]

RRF_K: Final = 60
BOOST_FACTOR: Final = 2.0
SNIPPET_WIDTH: Final = 320
CANDIDATE_MULTIPLIER: Final = 3

# Column weights for bm25: uri, title, framework, content.
_WEIGHTS: Final = (8.0, 4.0, 2.0, 1.0)
_BM25_ARGUMENTS: Final = ", ".join(str(weight) for weight in _WEIGHTS)

# FTS5 treats bare words as syntax, so every term is quoted.
_STOPWORDS: Final = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
        "from", "how", "i", "in", "is", "it", "its", "me", "my", "of", "on", "or",
        "should", "so", "than", "that", "the", "their", "them", "then", "there",
        "these", "this", "to", "use", "used", "using", "was", "what", "when",
        "where", "which", "who", "why", "will", "with", "you", "your",
    }
)

_IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
_CAMEL_PATTERN: Final = re.compile(r"\b[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")
_TERM_PATTERN: Final = re.compile(r"[A-Za-z0-9_.:+-]+")

Candidate = tuple[str, str, str, str, float]


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result, with the page text summarised rather than returned."""

    uri: str
    title: str
    framework: str
    kind: str
    score: float
    declaration: str | None
    availability: list[str]
    snippet: str
    source: str


def tokenize_query(text: str) -> list[str]:
    """Split a query into searchable terms, keeping identifiers intact."""
    terms = [match.group(0) for match in _TERM_PATTERN.finditer(text)]
    meaningful = [term for term in terms if term.lower() not in _STOPWORDS]
    return meaningful or terms


def build_match_query(text: str) -> str:
    """Turn free text into an FTS5 query string.

    Terms are OR-ed: requiring every term would return nothing for the
    natural-language questions this tool exists to answer, and bm25 already
    rewards pages that match more of them.
    """
    terms = tokenize_query(text)
    if not terms:
        raise ValueError("query must contain at least one searchable term")
    return " OR ".join(f'"{term}"' for term in terms)


def identifier_in(text: str) -> str | None:
    """Return the API identifier in a query, if it names one."""
    dotted = _IDENTIFIER_PATTERN.search(text)
    if dotted is not None:
        return dotted.group(0)
    camel = _CAMEL_PATTERN.search(text)
    return camel.group(0) if camel is not None else None


def make_snippet(content: str, terms: Sequence[str], *, width: int = SNIPPET_WIDTH) -> str:
    """Return a window of ``content`` around the first matching term."""
    collapsed = " ".join(content.split())
    if not collapsed:
        return ""

    lowered = collapsed.lower()
    position = -1
    for term in terms:
        found = lowered.find(term.lower())
        if found >= 0 and (position < 0 or found < position):
            position = found

    if position < 0:
        return collapsed[:width].strip()

    start = max(0, position - width // 3)
    end = min(len(collapsed), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end].strip()}{suffix}"


def _boost(candidates: list[Candidate], identifier: str | None) -> list[Candidate]:
    """Reward pages whose URI or title names the identifier the query asked for."""
    if not identifier:
        return candidates

    needle = identifier.lower()
    boosted: list[Candidate] = []
    for uri, title, framework, kind, score in candidates:
        if needle in uri.lower() or needle in title.lower():
            score *= BOOST_FACTOR
        boosted.append((uri, title, framework, kind, score))
    return boosted


def search_offline(
    db_path: Path,
    index_file: Path,
    query: str,
    *,
    frameworks: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 10,
    snippet_width: int = SNIPPET_WIDTH,
) -> list[Hit]:
    """Search the local index and return ranked pages."""
    match_query = build_match_query(query)
    candidates = _query_index(
        index_file,
        match_query,
        frameworks=frameworks,
        kinds=kinds,
        limit=limit * CANDIDATE_MULTIPLIER,
    )
    candidates = _boost(candidates, identifier_in(query))
    candidates.sort(key=lambda row: row[4], reverse=True)
    candidates = candidates[:limit]
    if not candidates:
        return []

    pages = get_documents(db_path, [uri for uri, *_ in candidates])
    terms = tokenize_query(query)

    hits: list[Hit] = []
    for uri, title, framework, kind, score in candidates:
        page = pages.get(uri)
        content = page.content if page is not None else ""
        hits.append(
            Hit(
                uri=uri,
                title=title,
                framework=framework,
                kind=kind,
                score=round(score, 4),
                declaration=page.declaration if page is not None else None,
                availability=page.availability if page is not None else [],
                snippet=make_snippet(content, terms, width=snippet_width),
                source="local",
            )
        )
    return hits


def _query_index(
    index_file: Path,
    match_query: str,
    *,
    frameworks: Sequence[str] | None,
    kinds: Sequence[str] | None,
    limit: int,
) -> list[Candidate]:
    clauses = ["docs_fts match ?"]
    parameters: list[object] = [match_query]
    if frameworks:
        clauses.append(f"d.framework in ({','.join('?' for _ in frameworks)})")
        parameters.extend(frameworks)
    if kinds:
        clauses.append(f"d.kind in ({','.join('?' for _ in kinds)})")
        parameters.extend(kinds)
    parameters.append(limit)

    connection = open_index(index_file)
    try:
        rows = connection.execute(
            f"""
            select d.uri, d.title, d.framework, d.kind,
                   bm25(docs_fts, {_BM25_ARGUMENTS}) as rank
            from docs_fts join docs d on d.id = docs_fts.rowid
            where {' and '.join(clauses)}
            order by rank
            limit ?
            """,
            parameters,
        ).fetchall()
    finally:
        connection.close()

    # bm25() returns lower-is-better negative values; flip so higher is better.
    return [
        (str(uri), str(title), str(framework), str(kind), -float(rank))
        for uri, title, framework, kind, rank in rows
    ]


def bridge_hits(documents: Sequence[Document]) -> list[Hit]:
    """Present bridge documents through the same result shape."""
    return [
        Hit(
            uri=document.uri,
            title=document.title,
            framework="",
            kind=document.kind,
            score=float(document.score),
            declaration=None,
            availability=[],
            snippet=make_snippet(document.contents, []),
            source="bridge",
        )
        for document in documents
    ]


def fuse_rrf(
    local: Sequence[Hit], bridge: Sequence[Hit], *, limit: int, k: int = RRF_K
) -> list[Hit]:
    """Merge two ranked lists by reciprocal rank, keeping one entry per page."""
    fused: dict[str, float] = {}
    chosen: dict[str, Hit] = {}
    sources: dict[str, set[str]] = {}

    for ranks in (local, bridge):
        for position, hit in enumerate(ranks, start=1):
            fused[hit.uri] = fused.get(hit.uri, 0.0) + 1.0 / (k + position)
            sources.setdefault(hit.uri, set()).add(hit.source)
            current = chosen.get(hit.uri)
            if current is None or (current.declaration is None and hit.declaration is not None):
                chosen[hit.uri] = hit

    merged: list[Hit] = []
    for uri, score in sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]:
        hit = chosen[uri]
        merged.append(
            Hit(
                uri=hit.uri,
                title=hit.title,
                framework=hit.framework,
                kind=hit.kind,
                score=round(score, 6),
                declaration=hit.declaration,
                availability=hit.availability,
                snippet=hit.snippet,
                source="hybrid" if len(sources[uri]) > 1 else hit.source,
            )
        )
    return merged


def enrich_from_corpus(db_path: Path, hits: Sequence[Hit]) -> list[Hit]:
    """Fill declaration, availability, and snippet from the stored page text.

    The bridge returns only short excerpts, so anything it contributes is
    completed here — the local corpus has the whole page.
    """
    missing = [hit.uri for hit in hits if hit.declaration is None and hit.source != "local"]
    if not missing:
        return list(hits)

    pages: dict[str, DocumentRecord] = get_documents(db_path, missing)
    enriched: list[Hit] = []
    for hit in hits:
        page = pages.get(hit.uri)
        if page is None:
            enriched.append(hit)
            continue
        enriched.append(
            Hit(
                uri=hit.uri,
                title=hit.title,
                framework=hit.framework or page.framework,
                kind=hit.kind,
                score=hit.score,
                declaration=page.declaration,
                availability=page.availability,
                snippet=hit.snippet or make_snippet(page.content, []),
                source=hit.source,
            )
        )
    return enriched
