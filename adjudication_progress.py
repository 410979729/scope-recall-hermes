"""Durable progress receipts for advisory L4 candidate review.

This module owns only progress identity and governance-ledger receipts. It never
calls a model and never changes memory lifecycle, keeping advisory progress
separate from adjudication authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .adjudication_l4 import L4Evidence
from .sql_store import record_governance_audit_event

EVENT_TYPE = "memory_auto_adjudication"
REVIEW_ACTION = "l4_advisory_review"
CURSOR_ACTION = "l4_queue_cursor"
SCAN_CURSOR_ACTION = "candidate_scan_cursor"


@dataclass(frozen=True)
class L4ReviewReceipt:
    """One model recommendation bound to an immutable candidate/evidence view."""

    memory_id: str
    scope_id: str
    review_fingerprint: str
    verdict: str
    reason: str


def advisory_queue_id(scope_ids: Sequence[str], *, all_scopes: bool) -> str:
    """Return a stable non-secret identity for one advisory queue."""

    normalized = tuple(sorted({str(value).strip() for value in scope_ids if str(value).strip()}))
    material = "all_scopes" if all_scopes else "\0".join(normalized)
    if not material:
        raise ValueError("L4 advisory queue requires an explicit scope mode")
    return f"l4_queue:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def latest_queue_cursor(conn: sqlite3.Connection, queue_id: str) -> str:
    """Return the last candidate selected for this queue, if any."""

    row = conn.execute(
        """
        SELECT after_json
        FROM governance_audit_events
        WHERE event_type = ? AND action = ? AND target_id = ? AND dry_run = 0
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, CURSOR_ACTION, queue_id),
    ).fetchone()
    if row is None:
        return ""
    try:
        payload = json.loads(str(row["after_json"] or "{}"))
    except (TypeError, ValueError):
        return ""
    return str(payload.get("last_selected_id") or "")


def latest_scan_cursor(conn: sqlite3.Connection, queue_id: str) -> tuple[str, str]:
    """Return the durable deterministic-lane keyset cursor for one queue."""

    row = conn.execute(
        """
        SELECT after_json
        FROM governance_audit_events
        WHERE event_type = ? AND action = ? AND target_id = ? AND dry_run = 0
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, SCAN_CURSOR_ACTION, queue_id),
    ).fetchone()
    if row is None:
        return "", ""
    try:
        payload = json.loads(str(row["after_json"] or "{}"))
    except (TypeError, ValueError):
        return "", ""
    return (
        str(payload.get("last_scanned_updated_at") or ""),
        str(payload.get("last_scanned_id") or ""),
    )


def record_scan_cursor(
    conn: sqlite3.Connection,
    *,
    queue_id: str,
    updated_at: str,
    memory_id: str,
    batch_id: str,
    created_at: str,
) -> None:
    """Advance deterministic candidate scanning in the caller's transaction."""

    if not queue_id or not memory_id:
        return
    record_governance_audit_event(
        conn,
        event_id=f"gov_{uuid.uuid4().hex}",
        event_type=EVENT_TYPE,
        action=SCAN_CURSOR_ACTION,
        target_id=queue_id,
        batch_id=batch_id,
        after={
            "last_scanned_updated_at": str(updated_at or ""),
            "last_scanned_id": memory_id,
        },
        reason="advance bounded deterministic candidate scan",
        actor="auto_adjudication",
        dry_run=False,
        created_at=created_at,
    )


def candidate_review_fingerprint(
    candidate: Mapping[str, Any], evidence: L4Evidence
) -> str:
    """Hash the candidate and complete evidence snapshot reviewed by L4."""

    payload = {
        "id": str(candidate.get("id") or ""),
        "scope_id": str(candidate.get("scope_id") or ""),
        "content": str(candidate.get("content") or ""),
        "metadata": str(candidate.get("metadata") or ""),
        "updated_at": str(candidate.get("updated_at") or ""),
        "evidence": {
            "text": evidence.text,
            "total_count": evidence.total_count,
            "included_count": evidence.included_count,
            "truncated": evidence.truncated,
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reviewed_fingerprints(
    conn: sqlite3.Connection, memory_ids: Sequence[str]
) -> set[tuple[str, str]]:
    """Load prior advisory receipts for the supplied candidate IDs."""

    normalized = tuple(dict.fromkeys(str(value) for value in memory_ids if str(value)))
    found: set[tuple[str, str]] = set()
    for offset in range(0, len(normalized), 400):
        batch = normalized[offset : offset + 400]
        if not batch:
            continue
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT target_id, after_json
            FROM governance_audit_events
            WHERE event_type = ? AND action = ? AND dry_run = 0
              AND target_id IN ({placeholders})
            """,
            (EVENT_TYPE, REVIEW_ACTION, *batch),
        ).fetchall()
        for row in rows:
            try:
                after = json.loads(str(row["after_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            fingerprint = str(after.get("review_fingerprint") or "")
            if fingerprint:
                found.add((str(row["target_id"] or ""), fingerprint))
    return found


def record_review_receipts(
    conn: sqlite3.Connection,
    receipts: Sequence[L4ReviewReceipt],
    *,
    batch_id: str,
    created_at: str,
    queue_id: str = "",
    last_selected_id: str = "",
) -> None:
    """Append advisory progress receipts to the governance ledger."""

    for receipt in receipts:
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type=EVENT_TYPE,
            action=REVIEW_ACTION,
            scope_id=receipt.scope_id,
            target_id=receipt.memory_id,
            batch_id=batch_id,
            after={
                "review_fingerprint": receipt.review_fingerprint,
                "verdict": receipt.verdict,
                "advisory_only": True,
            },
            reason=receipt.reason,
            actor="auto_adjudication",
            dry_run=False,
            created_at=created_at,
        )
    if queue_id and last_selected_id:
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type=EVENT_TYPE,
            action=CURSOR_ACTION,
            target_id=queue_id,
            batch_id=batch_id,
            after={"last_selected_id": last_selected_id},
            reason="advance bounded L4 advisory queue after one attempted candidate",
            actor="auto_adjudication",
            dry_run=False,
            created_at=created_at,
        )


__all__ = [
    "L4ReviewReceipt",
    "CURSOR_ACTION",
    "SCAN_CURSOR_ACTION",
    "REVIEW_ACTION",
    "advisory_queue_id",
    "candidate_review_fingerprint",
    "latest_queue_cursor",
    "latest_scan_cursor",
    "record_scan_cursor",
    "record_review_receipts",
    "reviewed_fingerprints",
]
