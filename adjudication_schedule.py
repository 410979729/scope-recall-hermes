"""Atomic schedule ownership for provider-triggered auto adjudication.

The governance audit ledger remains the single schedule authority.  Short
``BEGIN IMMEDIATE`` transactions claim, release, or complete a run; the slow
LLM/adjudication work happens outside the transaction.  Claims expire so a
worker crash cannot suppress adjudication forever.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .capture_filters import sanitize_report_text
from .maintenance_ops import connect_memory_db
from .sql_store import ensure_governance_schema, record_governance_audit_event
from .writer_lease import holding_truth_writer_lease

EVENT_TYPE = "memory_auto_adjudication"
TARGET_ID = "auto_adjudication_schedule"
CLAIM_ACTION = "schedule_claim"
RELEASE_ACTION = "schedule_release"
RETRY_ACTION = "schedule_retry"
COMPLETE_ACTION = "schedule_complete"
_CONTROL_ACTIONS = (CLAIM_ACTION, RELEASE_ACTION, RETRY_ACTION, COMPLETE_ACTION)


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


def schedule_target_id(scope_ids: tuple[str, ...]) -> str:
    """Return a deterministic, non-revealing schedule authority per scope set."""

    normalized = tuple(
        sorted({str(value).strip() for value in scope_ids if str(value).strip()})
    )
    if not normalized:
        raise ValueError("auto-adjudication schedule requires at least one scope")
    digest = hashlib.sha256("\0".join(normalized).encode("utf-8")).hexdigest()
    return f"{TARGET_ID}:{digest}"


def _latest_control(
    conn: sqlite3.Connection, *, target_id: str
) -> sqlite3.Row | None:
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
        (EVENT_TYPE, target_id, *_CONTROL_ACTIONS),
    ).fetchone()


def _last_completion(conn: sqlite3.Connection, *, target_id: str) -> float:
    row = conn.execute(
        """
        SELECT after_json
        FROM governance_audit_events
        WHERE event_type = ? AND action = ? AND target_id = ? AND dry_run = 0
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, COMPLETE_ACTION, target_id),
    ).fetchone()
    if row is None:
        return 0.0
    try:
        return float(_payload(row["after_json"]).get("completed_at_unix") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def latest_schedule_retry_context(db_path: Path, *, target_id: str) -> dict[str, Any]:
    """Return context from the currently pending retry, if one exists."""

    if not db_path.exists():
        return {}
    conn = connect_memory_db(db_path, apply=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        latest = _latest_control(conn, target_id=target_id)
        if latest is None or str(latest["action"]) != RETRY_ACTION:
            return {}
        context = _payload(latest["after_json"]).get("retry_context")
        return dict(context) if isinstance(context, dict) else {}
    finally:
        conn.close()


def adjudication_schedule_status(
    conn: sqlite3.Connection,
    *,
    target_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Return persistent operator status for one schedule authority."""

    current_time = time.time() if now is None else float(now)
    latest = _latest_control(conn, target_id=target_id)
    latest_action = str(latest["action"] or "") if latest is not None else ""
    latest_payload = _payload(latest["after_json"]) if latest is not None else {}
    completion = conn.execute(
        """
        SELECT rowid, after_json
        FROM governance_audit_events
        WHERE event_type = ? AND action = ? AND target_id = ? AND dry_run = 0
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (EVENT_TYPE, COMPLETE_ACTION, target_id),
    ).fetchone()
    completion_rowid = int(completion["rowid"]) if completion is not None else 0
    completion_payload = (
        _payload(completion["after_json"]) if completion is not None else {}
    )
    failures = conn.execute(
        """
        SELECT COUNT(*)
        FROM governance_audit_events
        WHERE event_type = ? AND target_id = ? AND dry_run = 0
          AND rowid > ? AND action IN (?, ?)
        """,
        (EVENT_TYPE, target_id, completion_rowid, RETRY_ACTION, RELEASE_ACTION),
    ).fetchone()[0]
    retry_context = latest_payload.get("retry_context")
    retry_context = retry_context if isinstance(retry_context, dict) else {}
    claim_expires_at = float(latest_payload.get("expires_at_unix") or 0.0)
    return {
        "status": latest_action or "never_run",
        "last_success_at_unix": float(
            completion_payload.get("completed_at_unix") or 0.0
        ),
        "retry_due_at_unix": float(latest_payload.get("retry_at_unix") or 0.0)
        if latest_action == RETRY_ACTION
        else 0.0,
        "claim_expires_at_unix": claim_expires_at
        if latest_action == CLAIM_ACTION
        else 0.0,
        "stale_claim": bool(
            latest_action == CLAIM_ACTION
            and claim_expires_at
            and claim_expires_at <= current_time
        ),
        "consecutive_failures": int(failures or 0),
        "l4_config_error": retry_context.get("reason") == "l4_config_error",
    }


def claim_adjudication_schedule(
    db_path: Path,
    *,
    now: float,
    interval_hours: float,
    claim_timeout_hours: float,
    trigger: str,
    target_id: str,
) -> str | None:
    """Atomically claim a due schedule slot, returning its opaque token."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with holding_truth_writer_lease(db_path.parent, role="auto_adjudication"):
        conn = _open_for_schedule(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            last_completion = _last_completion(conn, target_id=target_id)
            if last_completion and now - last_completion < interval_hours * 3600.0:
                conn.rollback()
                return None

            latest = _latest_control(conn, target_id=target_id)
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
            if latest is not None and str(latest["action"]) == RETRY_ACTION:
                try:
                    retry_at = float(
                        _payload(latest["after_json"]).get("retry_at_unix") or 0.0
                    )
                except (TypeError, ValueError):
                    retry_at = 0.0
                if retry_at > now:
                    conn.rollback()
                    return None

            token = uuid.uuid4().hex
            record_governance_audit_event(
                conn,
                event_id=f"gov_{uuid.uuid4().hex}",
                event_type=EVENT_TYPE,
                action=CLAIM_ACTION,
                target_id=target_id,
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
    target_id: str,
    retry_after_seconds: float = 0.0,
    retry_context: Mapping[str, Any] | None = None,
) -> bool:
    """Append a terminal event only while ``claim_token`` still owns the slot."""

    with holding_truth_writer_lease(db_path.parent, role="auto_adjudication"):
        conn = _open_for_schedule(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = _latest_control(conn, target_id=target_id)
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
            elif action == RETRY_ACTION:
                after.update(
                    retry_scheduled_at_unix=at,
                    retry_at_unix=at + max(0.0, retry_after_seconds),
                    retry_after_seconds=max(0.0, retry_after_seconds),
                )
                if retry_context:
                    after["retry_context"] = dict(retry_context)
            else:
                after["released_at_unix"] = at
            record_governance_audit_event(
                conn,
                event_id=f"gov_{uuid.uuid4().hex}",
                event_type=EVENT_TYPE,
                action=action,
                target_id=target_id,
                after=after,
                reason=(
                    "auto-adjudication successful completion throttle mark"
                    if action == COMPLETE_ACTION
                    else (
                        "auto-adjudication bounded retry after partial/failed review"
                        if action == RETRY_ACTION
                        else "auto-adjudication claim released without successful completion"
                    )
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
    target_id: str,
) -> bool:
    """Record successful completion for a still-owned schedule claim."""

    return _finish_claim(
        db_path,
        claim_token=claim_token,
        action=COMPLETE_ACTION,
        at=completed_at,
        trigger=trigger,
        interval_hours=interval_hours,
        target_id=target_id,
    )


def release_adjudication_schedule(
    db_path: Path,
    *,
    claim_token: str,
    released_at: float,
    trigger: str,
    interval_hours: float,
    target_id: str,
) -> bool:
    """Release a failed/non-successful claim so the next provider may retry."""

    return _finish_claim(
        db_path,
        claim_token=claim_token,
        action=RELEASE_ACTION,
        at=released_at,
        trigger=trigger,
        interval_hours=interval_hours,
        target_id=target_id,
    )


def retry_adjudication_schedule(
    db_path: Path,
    *,
    claim_token: str,
    scheduled_at: float,
    trigger: str,
    interval_hours: float,
    retry_after_seconds: float,
    target_id: str,
    retry_context: Mapping[str, Any] | None = None,
) -> bool:
    """Record a bounded retry window without claiming successful completion."""

    return _finish_claim(
        db_path,
        claim_token=claim_token,
        action=RETRY_ACTION,
        at=scheduled_at,
        trigger=trigger,
        interval_hours=interval_hours,
        retry_after_seconds=retry_after_seconds,
        target_id=target_id,
        retry_context=retry_context,
    )


__all__ = [
    "adjudication_schedule_status",
    "claim_adjudication_schedule",
    "complete_adjudication_schedule",
    "latest_schedule_retry_context",
    "release_adjudication_schedule",
    "retry_adjudication_schedule",
    "schedule_target_id",
]
