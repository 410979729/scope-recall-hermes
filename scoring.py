"""Recall scoring helpers for lexical, vector, temporal, metadata, and relation-aware ranking.

Scoring must stay explainable so benchmarks and operator traces can justify why a memory was returned."""

from __future__ import annotations

from typing import Any

from .aliases import canonicalize_alias
from .gating import normalized_token_set, query_tokens


_QUERY_STOPWORDS = {
    "what",
    "which",
    "when",
    "where",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "should",
    "could",
    "would",
    "can",
    "our",
    "your",
    "their",
    "my",
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "i",
    "we",
    "you",
}

TARGET_PRIORITY_BONUS = {
    "user": 0.08,
    "memory": 0.06,
    "project": 0.055,
    "ops": 0.055,
    "general": -0.04,
}


def _canonical_tokens(text: str) -> set[str]:
    canonical: set[str] = set()
    for token in normalized_token_set(query_tokens(text)):
        normalized = canonicalize_alias(token)
        if not normalized:
            continue
        canonical.add(normalized)
    return canonical



def lexical_score(*, query: str, content: str, summary: str, source: str, target: str) -> float:
    haystack = f"{summary}\n{content}".lower()
    normalized_query = query.lower()
    query_token_set = _canonical_tokens(query)
    doc_token_set = _canonical_tokens(haystack)

    overlap = 0.0
    informative_query = {token for token in query_token_set if token not in _QUERY_STOPWORDS}
    if informative_query:
        overlap = len(informative_query & doc_token_set) / max(len(informative_query), 1)
    elif query_token_set:
        overlap = len(query_token_set & doc_token_set) / max(len(query_token_set), 1)

    phrase_bonus = 0.35 if normalized_query and normalized_query in haystack else 0.0
    source_bonus = 0.18 if source == "builtin-curated" else 0.08 if source.startswith("tool") else 0.02
    target_bonus = TARGET_PRIORITY_BONUS.get(target, 0.0)
    return max(0.0, min(1.0, overlap * 0.72 + phrase_bonus + source_bonus + target_bonus))



def bm25_to_score(raw_scores: dict[str, float | None]) -> dict[str, float]:
    """Normalize SQLite FTS5 bm25() values onto a 0..1 relevance scale.

    FTS5 ranks lower ``bm25()`` values as better. Retrieval keeps that raw
    value for SQL ordering, then converts the candidate-local range into a
    conventional higher-is-better component for final hybrid scoring.
    """

    parsed: dict[str, float] = {}
    for key, value in raw_scores.items():
        try:
            parsed[str(key)] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if not parsed:
        return {}
    best = min(parsed.values())
    worst = max(parsed.values())
    if best == worst:
        return {key: 1.0 for key in parsed}
    span = worst - best
    return {key: max(0.0, min(1.0, (worst - value) / span)) for key, value in parsed.items()}



def semantic_similarity(left: str, right: str) -> float:
    left_tokens = _canonical_tokens(left)
    right_tokens = _canonical_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    jaccard = len(intersection) / max(len(union), 1)
    containment = len(intersection) / max(min(len(left_tokens), len(right_tokens)), 1)
    return max(jaccard, containment * 0.82)


def combine_scores(item: dict[str, Any], *, lexical_weight: float, vector_weight: float, bm25_weight: float = 0.0) -> float:
    lexical = float(item.get("lexical_score") or 0.0)
    vector = float(item.get("vector_score") or 0.0)
    bm25 = float(item.get("bm25_score") or 0.0)
    return max(0.0, min(1.0, lexical * lexical_weight + vector * vector_weight + bm25 * bm25_weight))


def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[str]],
    *,
    weights: dict[str, float] | None = None,
    k: int = 60,
    min_signals: int = 2,
) -> list[tuple[str, float]]:
    """Fuse heterogeneous retrieval rankings using weighted reciprocal rank fusion.

    RRF is preferable to adding raw lexical/vector/BM25 scores because those
    backends produce non-comparable score scales. Memories that appear in
    multiple strong rankings are promoted above single-signal neighbors.
    """

    weights = weights or {}
    scores: dict[str, float] = {}
    signal_hits: dict[str, set[str]] = {}
    for signal, ids in ranked_lists.items():
        weight = float(weights.get(signal, 1.0))
        if weight <= 0.0:
            continue
        seen: set[str] = set()
        for rank, item_id in enumerate(ids, start=1):
            clean_id = str(item_id or "")
            if not clean_id or clean_id in seen:
                continue
            seen.add(clean_id)
            scores[clean_id] = scores.get(clean_id, 0.0) + weight / (max(1, int(k)) + rank)
            signal_hits.setdefault(clean_id, set()).add(signal)
    min_required = max(1, int(min_signals or 1))
    filtered_scores = {item_id: score for item_id, score in scores.items() if len(signal_hits.get(item_id, set())) >= min_required}
    return sorted(filtered_scores.items(), key=lambda item: (item[1], item[0]), reverse=True)
