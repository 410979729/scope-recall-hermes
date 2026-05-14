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
    "this",
    "that",
    "these",
    "those",
    "i",
    "we",
    "you",
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
    target_bonus = 0.08 if target == "user" else 0.0
    return overlap * 0.72 + phrase_bonus + source_bonus + target_bonus



def combine_scores(item: dict[str, Any], *, lexical_weight: float, vector_weight: float) -> float:
    lexical = float(item.get("lexical_score") or 0.0)
    vector = float(item.get("vector_score") or 0.0)
    return lexical * lexical_weight + vector * vector_weight
