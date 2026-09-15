"""Bounded, read-only hygiene reporting for ordinary memory candidates.

The report intentionally inspects candidate text only inside the process.  Its
public rows expose governance metadata and decisions, never memory content or
summaries, and this module has no apply path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .candidate_promotion import (
    candidate_rows,
    classify_candidate_row,
    lifecycle as candidate_lifecycle,
    load_metadata,
)
from .candidate_review import candidate_identity_fields
from .capture_filters import classify_transport_noise
from .truth_connection import connect_truth_database

DEFAULT_CANDIDATE_HYGIENE_LIMIT = 200
MAX_CANDIDATE_HYGIENE_LIMIT = 1000

_AUTOMATIC_ADMISSION_TEXT_FIELDS = ("source", "route", "reviewed_at")
_AUTOMATIC_ADMISSION_BOOL_FIELDS = ("reviewed", "time_sensitive")
_CORRECTION_EVIDENCE_FIELDS = (
    "correction_evidence",
    "correction_evidence_refs",
    "correction_refs",
)
_CORRECTION_TARGET_FIELDS = (
    "superseded_by",
    "correction_memory_id",
    "replacement_memory_id",
)


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_CANDIDATE_HYGIENE_LIMIT
    return max(1, min(parsed, MAX_CANDIDATE_HYGIENE_LIMIT))


def _row_text(row: sqlite3.Row | Mapping[str, Any], key: str) -> str:
    try:
        return str(row[key] or "")  # type: ignore[index]
    except Exception:
        return ""


def _has_evidence(value: Any) -> bool:
    if value in (None, "", False):
        return False
    if isinstance(value, Mapping):
        return any(_has_evidence(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_has_evidence(item) for item in value)
    return True


def _correction_assessment(metadata: Mapping[str, Any]) -> tuple[bool | None, bool]:
    """Return ``(possible, replacement_known)`` without guessing from prose."""

    explicit = metadata.get("correction_possible")
    target_known = any(
        _has_evidence(metadata.get(key)) for key in _CORRECTION_TARGET_FIELDS
    )
    evidence_present = target_known or any(
        _has_evidence(metadata.get(key)) for key in _CORRECTION_EVIDENCE_FIELDS
    )
    if evidence_present:
        return True, target_known
    if isinstance(explicit, bool):
        return explicit, target_known
    # Absence is unknown, not evidence that no correction exists.
    return None, False


def _safe_automatic_admission(value: Any) -> dict[str, Any]:
    """Project the admission receipt onto its non-content contract fields."""

    if not isinstance(value, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for key in _AUTOMATIC_ADMISSION_TEXT_FIELDS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            projected[key] = raw.strip()[:160]
    for key in _AUTOMATIC_ADMISSION_BOOL_FIELDS:
        raw = value.get(key)
        if isinstance(raw, bool):
            projected[key] = raw
    return projected


def _recommended_action(
    *,
    transport_noise: bool,
    correction_possible: bool | None,
    correction_target_known: bool,
    decision_action: str,
    decision_risk: str,
    decision_reason: str,
) -> str:
    if transport_noise:
        return "archive_transport_wrapper"
    if correction_possible is True:
        return "supersede_candidate" if correction_target_known else "needs_review"
    if decision_action == "promote":
        return "promote_after_review"
    if decision_action == "archive":
        # 2.0.1 only defines an automatic archive recommendation for a proven
        # transport wrapper; other low-value classifications stay review-only.
        return "needs_review"
    if decision_risk in {"medium", "high"}:
        return "needs_review"
    if "review" in str(decision_reason or "").casefold():
        return "needs_review"
    return "keep_candidate"


def _candidate_hygiene_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
) -> dict[str, Any]:
    metadata = load_metadata(_row_text(row, "metadata"))
    source = _row_text(row, "source")
    identity = candidate_identity_fields(source=source, metadata=metadata)
    decision = classify_candidate_row(row, conn)
    content_noise = classify_transport_noise(_row_text(row, "content"))
    summary_noise = classify_transport_noise(_row_text(row, "summary"))
    transport_noise = bool(content_noise.blocked or summary_noise.blocked)
    correction_possible, correction_target_known = _correction_assessment(metadata)
    automatic_admission = _safe_automatic_admission(
        identity.get("automatic_admission")
    )
    return {
        "id": _row_text(row, "id"),
        "origin_kind": str(identity.get("origin_kind") or ""),
        "source": source,
        "lifecycle": candidate_lifecycle(metadata)
        or str(identity.get("lifecycle") or "candidate"),
        "target": _row_text(row, "target"),
        "memory_type": str(decision.memory_type or ""),
        "transport_noise": transport_noise,
        "correction_possible": correction_possible,
        "evidence_count": len(decision.evidence_refs),
        "automatic_admission": automatic_admission,
        "review_status": str(identity.get("review_status") or ""),
        "recommended_action": _recommended_action(
            transport_noise=transport_noise,
            correction_possible=correction_possible,
            correction_target_known=correction_target_known,
            decision_action=decision.action,
            decision_risk=decision.risk,
            decision_reason=decision.reason,
        ),
    }


def build_candidate_hygiene_report(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_CANDIDATE_HYGIENE_LIMIT,
) -> dict[str, Any]:
    """Build one bounded report while proving this call changed no truth rows."""

    bounded = _bounded_limit(limit)
    before_changes = int(conn.total_changes)
    previous_row_factory = conn.row_factory
    previous_query_only_row = conn.execute("PRAGMA query_only").fetchone()
    previous_query_only = bool(
        previous_query_only_row and int(previous_query_only_row[0]) == 1
    )
    conn.execute("PRAGMA query_only = ON")
    try:
        query_only_row = conn.execute("PRAGMA query_only").fetchone()
        query_only = bool(query_only_row and int(query_only_row[0]) == 1)
        if not query_only:
            raise RuntimeError(
                "candidate hygiene report requires PRAGMA query_only=ON"
            )

        # Fetch one sentinel beyond the public bound only to report truncation.
        rows = candidate_rows(conn, limit=bounded + 1)
        candidates = [
            _candidate_hygiene_row(conn, row) for row in rows[:bounded]
        ]
        total_changes = int(conn.total_changes) - before_changes
        if total_changes != 0:
            raise RuntimeError("candidate hygiene report mutated SQLite truth")
        return {
            "ok": True,
            "read_only": True,
            "query_only": query_only,
            "limit": bounded,
            "candidate_count": len(candidates),
            "truncated": len(rows) > bounded,
            "total_changes": total_changes,
            "candidates": candidates,
        }
    finally:
        conn.row_factory = previous_row_factory
        if not previous_query_only:
            conn.execute("PRAGMA query_only = OFF")


def candidate_hygiene_report(
    database_path: str | Path,
    *,
    limit: int = DEFAULT_CANDIDATE_HYGIENE_LIMIT,
) -> dict[str, Any]:
    """Open SQLite truth read-only and return the bounded hygiene report."""

    conn = connect_truth_database(database_path, mode="ro")
    try:
        conn.execute("PRAGMA query_only = ON")
        return build_candidate_hygiene_report(conn, limit=limit)
    finally:
        conn.close()


__all__ = [
    "DEFAULT_CANDIDATE_HYGIENE_LIMIT",
    "MAX_CANDIDATE_HYGIENE_LIMIT",
    "build_candidate_hygiene_report",
    "candidate_hygiene_report",
]
