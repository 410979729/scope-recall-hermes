"""Atomic schedule ownership for provider-triggered auto adjudication.

The governance audit ledger remains the single schedule authority.  Short
``BEGIN IMMEDIATE`` transactions claim, release, or complete a run; the slow
LLM/adjudication work happens outside the transaction.  Claims expire so a
worker crash cannot suppress adjudication forever.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text
from .maintenance_ops import connect_memory_db
from .sql_store import ensure_governance_schema, record_governance_audit_event
from .writer_lease import holding_truth_writer_lease

EVENT_TYPE = "memory_auto_adjudication"
TARGET_ID = "auto_adjudication_schedule"
CLAIM_ACTION = "schedule_claim"
RELEASE_ACTION = "schedule_release"
COMPLETE_ACTION = "schedule_complete"
_CONTROL_ACTIONS = (CLAIM_ACTION, RELEASE_ACTION, COMPLETE_ACTION)


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _open_for_schedule(db_path: Path) -> sqlite3.Connection:
    """Open the truth DB under the canonical writer lease contract."""

    conn = connect_memory_db(db_path, apply=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    ensure_governance_schema(conn)
    # Schema DDL is deliberately outside the business transaction.
    conn.commit()
    return conn


def _latest_control(conn: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ", ".join("?" for _ in _CONTROL_ACTIONS)
    return conn.execute(
        f"""
        SELECT action, after_json
        FROM governance_audit_events
        WHERE event_type = ? AND target_id = ? AND dry_run = 0
          AND action IN ({placeholders})
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, TARGET_ID, *_CONTROL_ACTIONS),
    ).fetchone()


def _last_completion(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT after_json
        FROM governance_audit_events
        WHERE event_type = ? AND action = ? AND target_id = ? AND dry_run = 0
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, COMPLETE_ACTION, TARGET_ID),
    ).fetchone()
    if row is None:
        return 0.0
    try:
        return float(_payload(row["after_json"]).get("completed_at_unix") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def claim_adjudication_schedule(
    db_path: Path,
    *,
    now: float,
    interval_hours: float,
    claim_timeout_hours: float,
    trigger: str,
) -> str | None:
    """Atomically claim a due schedule slot, returning its opaque token."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with holding_truth_writer_lease(db_path.parent, role="auto_adjudication"):
        conn = _open_for_schedule(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            last_completion = _last_completion(conn)
            if last_completion and now - last_completion < interval_hours * 3600.0:
                conn.rollback()
                return None

            latest = _latest_control(conn)
            if latest is not None and str(latest["action"]) == CLAIM_ACTION:
                try:
                    expires_at = float(
                        _payload(latest["after_json"]).get("expires_at_unix") or 0.0
                    )
                except (TypeError, ValueError):
                    expires_at = 0.0
                if expires_at > now:
                    conn.rollback()
                    return None

            token = uuid.uuid4().hex
            record_governance_audit_event(
                conn,
                event_id=f"gov_{uuid.uuid4().hex}",
                event_type=EVENT_TYPE,
                action=CLAIM_ACTION,
                target_id=TARGET_ID,
                after={
                    "claim_id": token,
                    "claimed_at_unix": now,
                    "expires_at_unix": now + claim_timeout_hours * 3600.0,
                    "interval_hours": interval_hours,
                    "trigger": sanitize_report_text(trigger),
                },
                reason="atomic auto-adjudication schedule claim",
                actor="auto_adjudication",
                dry_run=False,
            )
            conn.commit()
            return token
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def _finish_claim(
    db_path: Path,
    *,
    claim_token: str,
    action: str,
    at: float,
    trigger: str,
    interval_hours: float,
) -> bool:
    """Append a terminal event only while ``claim_token`` still owns the slot."""

    with holding_truth_writer_lease(db_path.parent, role="auto_adjudication"):
        conn = _open_for_schedule(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = _latest_control(conn)
            latest_payload = _payload(latest["after_json"]) if latest is not None else {}
            if (
                latest is None
                or str(latest["action"]) != CLAIM_ACTION
                or str(latest_payload.get("claim_id") or "") != claim_token
            ):
                conn.rollback()
                return False
            after: dict[str, Any] = {
                "claim_id": claim_token,
                "trigger": sanitize_report_text(trigger),
            }
            if action == COMPLETE_ACTION:
                after.update(
                    completed_at_unix=at,
                    interval_hours=interval_hours,
                )
            else:
                after["released_at_unix"] = at
            record_governance_audit_event(
                conn,
                event_id=f"gov_{uuid.uuid4().hex}",
                event_type=EVENT_TYPE,
                action=action,
                target_id=TARGET_ID,
                after=after,
                reason=(
                    "auto-adjudication successful completion throttle mark"
                    if action == COMPLETE_ACTION
                    else "auto-adjudication claim released without successful completion"
                ),
                actor="auto_adjudication",
                dry_run=False,
            )
            conn.commit()
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def complete_adjudication_schedule(
    db_path: Path,
    *,
    claim_token: str,
    completed_at: float,
    trigger: str,
    interval_hours: float,
) -> bool:
    """Record successful completion for a still-owned schedule claim."""

    return _finish_claim(
        db_path,
        claim_token=claim_token,
        action=COMPLETE_ACTION,
        at=completed_at,
        trigger=trigger,
        interval_hours=interval_hours,
    )


def release_adjudication_schedule(
    db_path: Path,
    *,
    claim_token: str,
    released_at: float,
    trigger: str,
    interval_hours: float,
) -> bool:
    """Release a failed/non-successful claim so the next provider may retry."""

    return _finish_claim(
        db_path,
        claim_token=claim_token,
        action=RELEASE_ACTION,
        at=released_at,
        trigger=trigger,
        interval_hours=interval_hours,
    )


__all__ = [
    "claim_adjudication_schedule",
    "complete_adjudication_schedule",
    "release_adjudication_schedule",
]
