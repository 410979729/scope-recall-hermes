"""Deterministic guard against copying source transcripts into durable memory.

The LLM prompt may ask for synthesis, but prompts are not an enforcement boundary.
This module provides a bounded, storage-independent overlap check shared by
per-turn capture and digest admission paths. Short quotations remain allowed;
long exact or near-verbatim copies fail closed before persistence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_MIN_CANDIDATE_CHARS = 600
_SHINGLE_CHARS = 8
_SHINGLE_STRIDE = 8
_MAX_CANDIDATE_CHARS = 16_000
_MAX_SOURCE_CHARS = 131_072
_OVERLAP_THRESHOLD = 0.75
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(value: object, *, limit: int) -> str:
    """Return a case-folded, whitespace-stable and bounded comparison string."""

    text = str(value or "")[:limit]
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _shingles(text: str, *, stride: int = _SHINGLE_STRIDE) -> list[str]:
    """Return bounded character shingles that also work for unsegmented CJK text."""

    if not text:
        return []
    if len(text) <= _SHINGLE_CHARS:
        return [text]
    final_start = len(text) - _SHINGLE_CHARS
    starts = list(range(0, final_start + 1, max(1, int(stride))))
    if starts[-1] != final_start:
        starts.append(final_start)
    return [text[start : start + _SHINGLE_CHARS] for start in starts]


def is_source_transcript_copy(
    candidate_content: object,
    source_texts: Iterable[object],
) -> bool:
    """Return whether a long candidate substantially copies its source evidence.

    Work is deliberately bounded. Candidates shorter than 600 normalized
    characters are permitted so concise necessary quotations do not become false
    positives. Longer candidates are rejected when they are an exact source
    substring or at least 75% of their character shingles occur in the source.
    Candidates beyond the comparison cap fail closed rather than hiding copied
    material after the bounded window.
    """

    raw_candidate = str(candidate_content or "")
    if len(raw_candidate) > _MAX_CANDIDATE_CHARS:
        return True
    candidate = _normalize(raw_candidate, limit=_MAX_CANDIDATE_CHARS)
    if len(candidate) < _MIN_CANDIDATE_CHARS:
        return False

    normalized_sources: list[str] = []
    remaining = _MAX_SOURCE_CHARS
    for source in source_texts:
        if remaining <= 0:
            break
        normalized = _normalize(source, limit=remaining)
        if not normalized:
            continue
        normalized_sources.append(normalized)
        remaining -= len(normalized)
    if not normalized_sources:
        return False

    combined = " ".join(normalized_sources)
    if candidate in combined:
        return True

    candidate_shingles = _shingles(candidate)
    if not candidate_shingles:
        return False
    source_shingles = set(_shingles(combined, stride=1))
    overlap = sum(shingle in source_shingles for shingle in candidate_shingles)
    return overlap / len(candidate_shingles) >= _OVERLAP_THRESHOLD
