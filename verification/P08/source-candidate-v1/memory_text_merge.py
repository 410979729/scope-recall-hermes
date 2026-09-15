"""Segment-aware text deduplication for reviewed memory merges.

Automatic digest pipelines must not use this as permission to merge merely
similar assertions.  It exists for explicit/manual merges and audited cleanup.
"""
from __future__ import annotations

import re

_SEGMENT_RE = re.compile(r"(?:\s*\n\s*§\s*\n\s*|\s+/\s+)")
_KEY_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)


def _key(value: str) -> str:
    return _KEY_RE.sub("", str(value or "").lower())


def _segments(text: str) -> list[str]:
    return [part.strip() for part in _SEGMENT_RE.split(str(text or "")) if part.strip()]


def automatic_merge_is_safe(existing: str, candidate: str) -> bool:
    """Allow opt-in automatic merge only for exact/contained assertions.

    Similarity alone cannot prove that attribution, a concrete value, time, or
    polarity stayed the same. Containment is intentionally conservative: other
    paraphrases remain separate until an explicit reviewed merge names both IDs.
    """

    existing_key = _key(existing)
    candidate_key = _key(candidate)
    if not existing_key or not candidate_key:
        return False
    if existing_key == candidate_key:
        return True
    if min(len(existing_key), len(candidate_key)) < 12:
        return False
    return existing_key in candidate_key or candidate_key in existing_key


def deduplicate_memory_text(text: str) -> str:
    """Remove exact or strictly-contained repeated segments without paraphrase loss."""

    kept: list[tuple[str, str]] = []
    for segment in _segments(text):
        key = _key(segment)
        if not key:
            continue
        replaced = False
        skip = False
        for index, (old_segment, old_key) in enumerate(kept):
            if key == old_key or (len(key) >= 12 and key in old_key):
                skip = True
                break
            if len(old_key) >= 12 and old_key in key:
                kept[index] = (segment, key)
                replaced = True
                break
        if not skip and not replaced:
            kept.append((segment, key))
    return "\n§\n".join(segment for segment, _ in kept)


def combine_reviewed_memory_text(existing: str, candidate: str) -> str:
    """Combine two explicitly reviewed assertions using visible boundaries."""

    return deduplicate_memory_text(f"{str(existing or '').strip()}\n§\n{str(candidate or '').strip()}")
