"""Pure query helpers for the supplemental CJK lexical channel.

Generation lifecycle and SQLite migration state belong in ``lexical_generation``.
This module is deliberately pure: it normalizes bounded CJK n-grams, creates an
FTS5 trigram expression, and scores reviewed substring evidence without I/O.
"""

from __future__ import annotations

import re

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def cjk_query_ngrams(query: str, *, limit: int = 24) -> list[str]:
    """Return bounded CJK trigrams then bigrams for supplemental discovery."""

    if limit < 1:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    runs = _CJK_RUN.findall(str(query or ""))
    for width in (3, 2):
        for run in runs:
            if len(run) < width:
                continue
            for index in range(0, len(run) - width + 1):
                term = run[index : index + width]
                if term in seen:
                    continue
                seen.add(term)
                terms.append(term)
                if len(terms) >= limit:
                    return terms
    return terms


def trigram_fts_query(query: str, tokens: list[str]) -> str:
    """Build a bounded OR query suitable for the FTS5 trigram tokenizer."""

    candidates = cjk_query_ngrams(query, limit=16)
    for token in tokens:
        normalized = str(token or "").strip().lower()
        if len(normalized) >= 3 and normalized not in candidates:
            candidates.append(normalized)
        if len(candidates) >= 24:
            break
    quoted = [
        f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in candidates
        if len(term) >= 3
    ]
    return " OR ".join(quoted[:24])


def cjk_substring_score(query: str, content: str, summary: str) -> float:
    """Score CJK evidence without allowing a single generic bigram through."""

    terms = cjk_query_ngrams(query, limit=24)
    if not terms:
        return 0.0
    haystack = f"{content}\n{summary}".lower()
    matches = [term for term in terms if term.lower() in haystack]
    if not matches:
        return 0.0
    trigram_matches = sum(1 for term in matches if len(term) >= 3)
    bigram_matches = sum(1 for term in matches if len(term) == 2)
    if trigram_matches == 0 and bigram_matches < 2:
        return 0.0
    return min(0.5, 0.12 + 0.04 * min(len(matches), 6))
