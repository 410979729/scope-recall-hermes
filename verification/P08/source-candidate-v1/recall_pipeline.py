"""Compatibility shim. Implementation: _internal.recall.pipeline."""
from __future__ import annotations

from ._internal.recall.pipeline import (
    RecallSearchPlan,
    apply_general_policy,
    build_search_plan,
    filter_recall_lifecycle,
    final_trace_payload,
    humanize_filter_trace,
    humanize_recall_components,
    initial_trace,
    merge_recall_candidates,
    normalize_query,
    normalize_recall_mode,
    positive_int,
    rank_recall_items,
    recall_dedup_key,
    safe_recall_item,
    sanitize_recall_window,
    trim_recall_budget,
)

__all__ = [
    "RecallSearchPlan",
    "apply_general_policy",
    "build_search_plan",
    "filter_recall_lifecycle",
    "final_trace_payload",
    "humanize_filter_trace",
    "humanize_recall_components",
    "initial_trace",
    "merge_recall_candidates",
    "normalize_query",
    "normalize_recall_mode",
    "positive_int",
    "rank_recall_items",
    "recall_dedup_key",
    "safe_recall_item",
    "sanitize_recall_window",
    "trim_recall_budget",
]
