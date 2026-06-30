"""Prompt rendering helpers for injecting current-turn recall/profile context.

Rendering must be compact and deterministic because it directly affects the agent prompt budget."""

from __future__ import annotations

from typing import Any

from .gating import compact_text, config_bool, should_skip_retrieval
from .models import RecallItem


def render_current_turn_recall(provider: Any, query: str) -> str:
    """Return the system-prompt recall block for the current user query.

    The provider owns runtime state and config; this module owns the recall
    presentation policy so provider.py stays a lifecycle coordinator.
    """
    if not _should_attempt_recall(provider):
        return ""

    normalized_query = provider._normalize_query(query, int(provider._config_value("query_char_limit", 1000)))
    if should_skip_retrieval(normalized_query, int(provider._config_value("auto_recall_min_length", 15))):
        return ""

    results = provider._recall_service.search_memories(normalized_query, limit=provider._retrieve_limit())
    results = _drop_recently_recalled(provider, results)
    selected = _select_recall_items(provider, results)
    if not selected:
        return ""

    provider._mark_recalled([item.id for item in selected])
    lines = [f"- [{item.target or item.source}] {item.summary}" for item in selected]
    return "## Scope Recall Relevant Memories\n" + "\n".join(lines)


def _should_attempt_recall(provider: Any) -> bool:
    return config_bool(provider._config, "auto_recall", True) and provider._scope.agent_context == "primary"


def _drop_recently_recalled(provider: Any, results: list[RecallItem]) -> list[RecallItem]:
    min_repeated = int(provider._config_value("auto_recall_min_repeated", 8))
    if min_repeated <= 0:
        return results
    filtered: list[RecallItem] = []
    for item in results:
        last_turn = provider._last_recall_turns.get(item.id, 0)
        if last_turn and (provider._current_turn - last_turn) < min_repeated:
            continue
        filtered.append(item)
    return filtered


def _select_recall_items(provider: Any, results: list[RecallItem]) -> list[RecallItem]:
    max_items = min(
        int(provider._config_value("auto_recall_max_items", 3)),
        int(provider._config_value("max_recall_per_turn", 10)),
    )
    max_chars = int(provider._config_value("auto_recall_max_chars", 600))
    per_item_chars = int(provider._config_value("auto_recall_per_item_max_chars", 180))

    selected: list[RecallItem] = []
    used_chars = 0
    for item in results:
        if len(selected) >= max_items:
            break
        summary = _fit_summary(item, per_item_chars=per_item_chars, remaining_chars=max_chars - used_chars)
        if not summary:
            continue
        selected.append(
            RecallItem(
                id=item.id,
                content=item.content,
                summary=summary,
                source=item.source,
                target=item.target,
                score=item.score,
                updated_at=item.updated_at,
                metadata=item.metadata or {},
            )
        )
        used_chars += len(summary)
    return selected


def _fit_summary(item: RecallItem, *, per_item_chars: int, remaining_chars: int) -> str:
    if remaining_chars <= 0:
        return ""
    summary = compact_text(item.summary or item.content, per_item_chars)
    if len(summary) > remaining_chars:
        summary = compact_text(summary, remaining_chars)
    return summary
