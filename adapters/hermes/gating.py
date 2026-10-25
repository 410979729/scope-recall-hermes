"""Compatibility surface for callers checking input without a session."""

from __future__ import annotations

from typing import Optional

from scope_recall.core.auto_recall import automatic_recall_skip_reason as automatic_recall_skip_reason


def is_trivial_prompt(text: Optional[str]) -> bool:
    """Delegate input-only eligibility to the shared core decision."""
    return automatic_recall_skip_reason(text) is not None
