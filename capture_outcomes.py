"""Process-local capture enqueue status accounting.

Receipts intentionally contain only status/reason/caller tokens and disappear
with the provider process.  Capture outcomes never create SQLite rows or
filesystem sidecars.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .capture_filters import sanitize_report_text

logger = logging.getLogger(__name__)

ENQUEUE_STATUSES = frozenset({"accepted", "rejected", "deferred"})
SANITIZED_OUTCOME_REASONS = frozenset(
    {
        "queued",
        "filtered",
        "queue_full",
        "write_authority",
        "writer_unavailable",
        "invalid_result",
        "unknown",
    }
)
RECEIPT_RING_SIZE = 8


def sanitize_outcome_reason(reason: Any) -> str:
    """Return a short allow-listed reason token with no path or secret text."""

    cleaned = sanitize_report_text(str(reason or "").strip() or "unknown")
    token = cleaned.replace(" ", "_")[:64]
    return token if token in SANITIZED_OUTCOME_REASONS else "unknown"


def _account_process_outcome(provider: Any, status: str) -> None:
    if status not in {"rejected", "deferred"}:
        return
    attr = f"_capture_queue_{status}"
    setattr(provider, attr, int(getattr(provider, attr, 0) or 0) + 1)


def ensure_outcome_accounted(provider: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Account a rejected/deferred enqueue only in this provider process."""

    accounted = dict(result)
    status = str(accounted.get("status") or "")
    if status not in ENQUEUE_STATUSES:
        accounted = {
            "status": "deferred",
            "reason": "invalid_result",
            "intent_id": None,
            "depth": 0,
            "capacity": 0,
        }
        status = "deferred"
    _account_process_outcome(provider, status)
    return accounted


def handle_capture_enqueue(
    provider: Any, result: Any, *, caller: str
) -> dict[str, Any]:
    """Observe one already-accounted production enqueue result."""

    if not isinstance(result, dict) or result.get("status") not in ENQUEUE_STATUSES:
        accounted = ensure_outcome_accounted(
            provider,
            {
                "status": "deferred",
                "reason": "invalid_result",
                "intent_id": None,
                "depth": 0,
                "capacity": 0,
            },
        )
    else:
        accounted = dict(result)
    receipt = {
        "status": str(accounted.get("status") or ""),
        "reason": sanitize_outcome_reason(accounted.get("reason")),
        "caller": sanitize_report_text(str(caller or "unknown"))[:64],
    }
    provider._last_capture_enqueue = receipt
    ring = list(getattr(provider, "_capture_enqueue_receipts", []) or [])
    ring.append(receipt)
    provider._capture_enqueue_receipts = ring[-RECEIPT_RING_SIZE:]
    if receipt["status"] in {"rejected", "deferred"}:
        logger.warning(
            "Scope Recall capture enqueue %s (%s) from %s",
            receipt["status"],
            receipt["reason"],
            receipt["caller"],
        )
    return accounted


__all__ = [
    "ENQUEUE_STATUSES",
    "RECEIPT_RING_SIZE",
    "SANITIZED_OUTCOME_REASONS",
    "ensure_outcome_accounted",
    "handle_capture_enqueue",
    "sanitize_outcome_reason",
]
