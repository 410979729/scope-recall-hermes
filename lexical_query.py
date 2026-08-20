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


_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.-]{3,}")


def cjk_substring_score(query: str, content: str, summary: str) -> float:
    """Score supplemental-channel evidence without letting one generic bigram through.

    The curve is calibrated against the retrieval ``min_score`` gate (default
    0.35): one trigram plus a couple of supporting bigrams must survive it,
    because colloquial Chinese paraphrases rarely share more surface than
    that with the stored text. The old flat curve (0.12 + 0.04/match, cap
    0.5) scored exactly those hits at 0.24-0.32 and silently erased the
    shadow channel's top-ranked candidates. ASCII identifiers (``systemd``,
    ``final`` …) count as strong evidence too; the FTS trigram tokenizer
    already matched on them, so scoring must not ignore them.
    """

    terms = cjk_query_ngrams(query, limit=24)
    query_ascii = set(_ASCII_TOKEN.findall(str(query or "").lower()))
    if not terms and not query_ascii:
        return 0.0
    haystack = f"{content}\n{summary}".lower()
    trigram_hits = sum(
        1 for term in terms if len(term) >= 3 and term.lower() in haystack
    )
    bigram_hits = sum(
        1 for term in terms if len(term) == 2 and term.lower() in haystack
    )
    ascii_hits = sum(1 for token in query_ascii if token in haystack)
    strong_hits = trigram_hits + ascii_hits
    if strong_hits == 0 and bigram_hits < 2:
        return 0.0
    return min(
        0.55,
        0.20 + 0.06 * min(strong_hits, 4) + 0.03 * min(bigram_hits, 5),
    )
