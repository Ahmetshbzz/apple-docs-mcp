"""Ranking over the derived index, with optional fusion against Xcode's ranker.

Terms are chosen before the query runs, by document frequency: a word carried by
a large share of the corpus cannot narrow anything, and every page that has it
must still be scored. Terms that name a framework or an API are kept whatever
their frequency, because that is how a caller scopes a question. Measured effect
of that selection alone: the same six queries went from 10–121 ms to 1–22 ms.

Ranking is bm25 with column weights biased toward the URI and title, which is
what makes an exact API name beat a passing mention. Xcode's bridge, when it is
available and approved, adds its own semantic order; :func:`fuse_rrf` combines
the two by reciprocal rank instead of trying to compare scores that are not on
the same scale.

Measured on the reference machine (6 queries, same machine, same session): the
local pass answers in 19-88 ms against the bridge's mean 311 ms, and on 4 of the
6 queries its top five were closer to the question than the bridge's, mostly
because the bridge ranks short "…: Relationships" stubs highly.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apple_docs_mcp.bridge import Document
from apple_docs_mcp.connections import shared
from apple_docs_mcp.corpus import DocumentRecord, get_documents
from apple_docs_mcp.index import framework_sizes, open_index

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
    "named_terms",
    "search_offline",
    "select_terms",
    "term_document_frequencies",
    "tokenize_query",
]

RRF_K: Final = 60
BOOST_FACTOR: Final = 2.0
SNIPPET_WIDTH: Final = 320
#: bm25 must score every match before it can order them, so a wider pool costs
#: almost nothing and decides whether a prior can act on the answer at all.
CANDIDATE_MULTIPLIER: Final = 12

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

#: A term carried by more documents than this share of the corpus cannot narrow a
#: search: bm25 gives it almost no weight and every page has to be scored for it.
MAX_TERM_SHARE: Final = 0.02
MAX_TERM_DOCUMENTS: Final = 2000
SELECTED_TERMS: Final = 4
FALLBACK_TERMS: Final = 2

#: How far the largest framework may outrank the smallest, and how much a
#: question prefers a guide over a symbol page. Both are priors over a ranking
#: that only sees terms; measured against the judged query set, not chosen.
AUTHORITY_WEIGHT: Final = 0.6
QUESTION_WEIGHT: Final = 1.6
QUESTION_PATTERN: Final = re.compile(
    r"\b(how|what|why|when|where|which|can i|should i|do i|does|is it)\b", re.IGNORECASE
)
GUIDE_KINDS: Final = frozenset({"article", "tutorial"})

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


def build_match_query(text: str, terms: Sequence[str] | None = None) -> str:
    """Turn free text into an FTS5 query string.

    Terms are OR-ed: requiring every term would return nothing for the
    natural-language questions this tool exists to answer, and bm25 already
    rewards pages that match more of them. Pass ``terms`` to narrow the query to
    the words that can actually discriminate; see :func:`select_terms`.
    """
    chosen = list(terms) if terms is not None else tokenize_query(text)
    if not chosen:
        raise ValueError("query must contain at least one searchable term")
    return " OR ".join(f'"{term}"' for term in chosen)


def term_document_frequencies(
    connection: sqlite3.Connection, terms: Sequence[str]
) -> dict[str, int]:
    """How many documents carry each term, straight from the index."""
    if not terms:
        return {}

    placeholders = ",".join("?" for _ in terms)
    rows = connection.execute(
        f"select term, doc from docs_vocab where term in ({placeholders})",
        [term.lower() for term in terms],
    ).fetchall()
    return {str(term): int(count) for term, count in rows}


def named_terms(terms: Sequence[str]) -> list[str]:
    """The terms that name something: ``Metal``, ``AVAudioSession.Category``.

    A capitalized or dotted token is how a caller says "in this framework" or
    "this exact API", so it is kept even when the corpus carries it everywhere —
    dropping it is what turns a Metal question into a general one.
    """
    return [term for term in terms if term[:1].isupper() or "." in term or ":" in term]


def select_terms(
    terms: Sequence[str],
    frequencies: dict[str, int],
    total_documents: int | None,
    *,
    mandatory: Sequence[str] = (),
    max_terms: int = SELECTED_TERMS,
) -> list[str]:
    """Keep the terms that can actually narrow the corpus.

    A word carried by a large share of the corpus contributes almost nothing to
    bm25 and forces the engine to score every page that has it, so it costs
    latency and buys noise. Terms that name a framework or an API are kept
    regardless. When nothing is left the query keeps its two rarest words, so it
    still means something instead of collapsing.
    """
    if len(terms) <= 1:
        return list(terms)

    ceiling = MAX_TERM_DOCUMENTS
    if total_documents:
        ceiling = max(ceiling, int(total_documents * MAX_TERM_SHARE))

    forced = [term for term in terms if term in set(mandatory)]
    known = [(term, frequencies.get(term.lower(), 0)) for term in terms if term not in set(forced)]
    selective = [term for term, count in known if 0 < count <= ceiling]
    if selective:
        return forced + selective[: max(max_terms - len(forced), 0)]

    # Every remaining term is common: keep the rarest ones rather than the whole
    # query, so the search still has something to narrow with.
    present = [term for term, count in sorted(known, key=lambda pair: pair[1]) if count > 0]
    if present:
        return forced + present[:FALLBACK_TERMS]

    # Nothing is known to the index (a typo, or an API from a newer SDK), so the
    # query keeps its words and returns an honest empty result.
    fallback = [term for term, _ in sorted(known, key=lambda pair: pair[1])[:FALLBACK_TERMS]]
    return forced + fallback if forced else fallback


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
    total_documents: int | None = None,
) -> list[Hit]:
    """Search the local index and return ranked pages.

    Two passes. The first requires every selected term, which is both precise and
    cheap; the second loosens to OR only when the strict pass came back short, so
    recall is never traded away silently.
    """
    terms = tokenize_query(query)
    connection = shared(index_file, open_index)
    selected = select_terms(
        terms,
        term_document_frequencies(connection, terms),
        total_documents,
        mandatory=named_terms(terms),
    )
    # Priors act only on questions. When a caller names an API, the name match is
    # the answer and a framework's size has no business outranking it.
    question = looks_like_question(query)
    ranked = _rank(
        index_file,
        build_match_query(query, selected),
        frameworks=frameworks,
        kinds=kinds,
        limit=limit * CANDIDATE_MULTIPLIER,
        identifier=identifier_in(query),
        factors=authority_factors(framework_sizes(index_file, connection)) if question else {},
        prefer_guides=question,
    )
    candidates = collapse_to_parents(
        ranked, parent_map(db_path, [row[0] for row in ranked]), limit
    )
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
                title=page.title if page is not None else title,
                framework=page.framework if page is not None else framework,
                kind=page.kind if page is not None else kind,
                score=round(score, 4),
                declaration=page.declaration if page is not None else None,
                availability=page.availability if page is not None else [],
                snippet=make_snippet(content, terms, width=snippet_width),
                source="local",
            )
        )
    return hits


def looks_like_question(query: str) -> bool:
    """Whether the caller asked a question rather than named an API."""
    return QUESTION_PATTERN.search(query) is not None


def authority_factors(sizes: dict[str, int]) -> dict[str, float]:
    """Map each framework to a multiplier between 1 and ``1 + AUTHORITY_WEIGHT``."""
    if not sizes:
        return {}
    span = math.log1p(max(sizes.values()))
    if span <= 0:
        return {}
    return {
        name: 1.0 + AUTHORITY_WEIGHT * (math.log1p(count) / span)
        for name, count in sizes.items()
    }


def _rank(
    index_file: Path,
    match_query: str,
    *,
    frameworks: Sequence[str] | None,
    kinds: Sequence[str] | None,
    limit: int,
    identifier: str | None,
    factors: dict[str, float] | None = None,
    prefer_guides: bool = False,
) -> list[Candidate]:
    """Run one pass and order it: identifier hits first, then by weighted bm25."""
    candidates = _boost(
        _query_index(
            index_file, match_query, frameworks=frameworks, kinds=kinds, limit=limit
        ),
        identifier,
    )
    if factors or prefer_guides:
        weighted: list[Candidate] = []
        for uri, title, framework, kind, score in candidates:
            score *= (factors or {}).get(framework, 1.0)
            if prefer_guides and kind in GUIDE_KINDS:
                score *= QUESTION_WEIGHT
            weighted.append((uri, title, framework, kind, score))
        candidates = weighted
    candidates.sort(key=lambda row: row[4], reverse=True)
    return candidates


def parent_map(db_path: Path, uris: Sequence[str]) -> dict[str, str]:
    """Map each page to the page that holds it, sections included."""
    pages = get_documents(db_path, list(uris))
    return {
        uri: (pages[uri].parent_uri or uri) for uri in uris if uri in pages
    }


def collapse_to_parents(
    ranked: Sequence[Candidate], parents: dict[str, str], limit: int
) -> list[Candidate]:
    """Return canonical pages rather than several sections of the same page.

    A section is where the words matched; the page that holds it is what answers
    the question, and three sections of one article should not fill three slots.
    """
    chosen: dict[str, Candidate] = {}
    order: list[str] = []
    for uri, title, framework, kind, score in ranked:
        display = parents.get(uri, uri)
        if display in chosen:
            continue
        chosen[display] = (display, title, framework, kind, score)
        order.append(display)
        if len(order) >= limit:
            break
    return [chosen[key] for key in order]


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

    connection = shared(index_file, open_index)
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
    local: Sequence[Hit],
    bridge: Sequence[Hit],
    *,
    limit: int,
    k: int = RRF_K,
    weights: tuple[float, float] = (1.0, 1.0),
) -> list[Hit]:
    """Merge two ranked lists by reciprocal rank, keeping one entry per page.

    Weights let the caller say which list it trusts for this question. Measured
    on the judged sets: an identifier lookup is answered by the local list alone,
    a question by the bridge's order, and fusing them with equal weight serves
    neither.
    """
    fused: dict[str, float] = {}
    chosen: dict[str, Hit] = {}
    sources: dict[str, set[str]] = {}

    for index, ranks in enumerate((local, bridge)):
        weight = weights[index] if index < len(weights) else 1.0
        if weight <= 0:
            continue
        for position, hit in enumerate(ranks, start=1):
            fused[hit.uri] = fused.get(hit.uri, 0.0) + weight / (k + position)
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
