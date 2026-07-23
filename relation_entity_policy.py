"""Shared entity semantics for relation extraction and incremental indexing.

This module is deliberately free of SQLite and queue dependencies.  Both the
extractor and the durable frequency index must normalize, filter, and threshold
entities identically or persisted document-frequency counts cease to represent
relation semantics.
"""

from __future__ import annotations

import re
from math import ceil
from typing import Any

HIGH_FREQUENCY_MIN_CORPUS = 20
HIGH_FREQUENCY_MIN_DOCUMENTS = 12
HIGH_FREQUENCY_DOCUMENT_RATIO = 0.03

GENERIC_RELATION_ENTITIES = frozenset(
    {
        "active",
        "agent",
        "check",
        "cron",
        "digest",
        "hermes",
        "live",
        "max",
        "medium",
        "memory",
        "minimal",
        "model",
        "msg",
        "none",
        "not",
        "plugin",
        "provider",
        "release",
        "scope recall",
        "scope-recall",
        "telegram",
        "tool",
        "xhigh",
        "任务",
        "插件",
        "模型",
        "系统",
        "记忆",
        "配置",
    }
)


def normalize_relation_entity(entity: Any) -> str:
    """Return the canonical relation-entity spelling persisted in postings."""

    return re.sub(r"\s+", " ", str(entity or "").strip().lower())


def _has_minimum_entity_length(normalized: str) -> bool:
    cjk_count = sum(1 for char in normalized if "\u4e00" <= char <= "\u9fff")
    if cjk_count:
        return cjk_count >= 2
    return len(normalized) >= 3


def distinctive_relation_entity(
    entity: Any,
    *,
    blocked_entities: set[str] | None = None,
) -> bool:
    """Return whether an entity is specific enough to justify graph evidence."""

    normalized = normalize_relation_entity(entity)
    if (
        not _has_minimum_entity_length(normalized)
        or normalized in GENERIC_RELATION_ENTITIES
        or normalized.isdigit()
    ):
        return False
    return not blocked_entities or normalized not in blocked_entities


def high_frequency_document_threshold(visible_memory_count: int) -> int | None:
    """Return the blocking document count, or ``None`` for a small corpus."""

    count = max(0, int(visible_memory_count))
    if count < HIGH_FREQUENCY_MIN_CORPUS:
        return None
    return max(
        HIGH_FREQUENCY_MIN_DOCUMENTS,
        ceil(count * HIGH_FREQUENCY_DOCUMENT_RATIO),
    )


__all__ = [
    "GENERIC_RELATION_ENTITIES",
    "HIGH_FREQUENCY_DOCUMENT_RATIO",
    "HIGH_FREQUENCY_MIN_CORPUS",
    "HIGH_FREQUENCY_MIN_DOCUMENTS",
    "distinctive_relation_entity",
    "high_frequency_document_threshold",
    "normalize_relation_entity",
]
