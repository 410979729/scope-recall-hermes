"""Canonical public status contract for the rebuildable vector companion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


VECTOR_STATES = frozenset({"ready", "degraded", "needs_repair", "disabled"})
VECTOR_DEBT_KEYS = ("pending", "processing", "retry", "dead_letter")
VECTOR_STATUS_SCHEMA_VERSION = "vector_status.v1"


def normalize_vector_debt_counts(value: Mapping[str, Any] | None) -> dict[str, int]:
    """Return stable, non-negative aggregate debt counters."""

    source = value or {}
    result = {
        key: max(0, int(source.get(key) or 0))
        for key in VECTOR_DEBT_KEYS
    }
    result["replayable"] = sum(
        result[key] for key in ("pending", "processing", "retry")
    )
    return result


def vector_status_contract(
    *,
    state: str,
    reason_code: str,
    message: str = "",
    debt_counts: Mapping[str, Any] | None = None,
    usable_for_query: bool | None = None,
) -> dict[str, Any]:
    """Build the stable four-state public vector status payload.

    ``status`` is retained as a compatibility alias, but it is constrained to
    the same four-state value as ``state``. Detailed diagnostics belong in
    ``reason_code`` rather than creating additional top-level states.
    """

    normalized_state = str(state or "").strip().lower()
    if normalized_state not in VECTOR_STATES:
        raise ValueError(f"unsupported vector state: {normalized_state or '<empty>'}")

    defaults = {
        "ready": (True, False, True),
        "degraded": (True, False, True),
        "needs_repair": (False, True, False),
        "disabled": (False, False, False),
    }
    auto_recoverable, repair_required, default_usable = defaults[normalized_state]
    resolved_usable = default_usable if usable_for_query is None else bool(usable_for_query)
    return {
        "schema_version": VECTOR_STATUS_SCHEMA_VERSION,
        "state": normalized_state,
        "status": normalized_state,
        "reason_code": str(reason_code or "unspecified").strip().lower(),
        "auto_recoverable": auto_recoverable,
        "repair_required": repair_required,
        "usable_for_query": resolved_usable,
        "message": str(message or ""),
        "debt_counts": normalize_vector_debt_counts(debt_counts),
    }


__all__ = [
    "VECTOR_DEBT_KEYS",
    "VECTOR_STATUS_SCHEMA_VERSION",
    "VECTOR_STATES",
    "normalize_vector_debt_counts",
    "vector_status_contract",
]
