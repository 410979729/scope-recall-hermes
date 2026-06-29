from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Sequence

from .journal import ensure_journal_schema
from .maintenance_ops import make_batch_id, now_utc_iso
from .sql_store import ensure_schema, record_governance_audit_event


def _now_iso() -> str:
    return now_utc_iso()


def classify_rejection_reason(reason: str) -> str:
    lowered = str(reason or "").strip().lower()
    detail = lowered.split(":", 1)[1] if ":" in lowered else lowered
    if any(token in detail for token in ("auth", "unauthorized", "forbidden", "permission", "credential", "token", "api_key", "api key")):
        return "auth"
    if any(token in detail for token in ("quota", "rate", "429", "limit", "too many requests")):
        return "quota"
    if any(token in detail for token in ("timeout", "timed out", "deadline", "connection reset", "temporarily unavailable")):
        return "timeout"
    if any(token in detail for token in ("parse", "json", "schema", "decode", "invalid response")):
        return "parse"
    if any(token in detail for token in ("low-value", "low_value", "no-value", "no value", "skip", "noise", "empty")):
        return "low_value"
    return "unknown"


def _prefix_clause(reason_prefixes: Sequence[str]) -> tuple[str, list[str]]:
    prefixes = [str(item).strip() for item in reason_prefixes if str(item).strip()]
    if not prefixes:
        prefixes = ["retry-exhausted:"]
    clauses = " OR ".join("r.reason LIKE ?" for _ in prefixes)
    return f"({clauses})", [f"{prefix}%" for prefix in prefixes]


def find_replay_candidates(
    conn: sqlite3.Connection,
    *,
    reason_prefixes: Sequence[str] = ("retry-exhausted:",),
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Find processed journal entries that are safe to replay after digest failure.

    Candidates are rejected entries whose reason starts with a retry/dead-letter
    prefix and that do not already have a durable memory source link.  This keeps
    replay conservative: we only re-open entries that were skipped/quarantined,
    not entries that already produced durable memories.
    """

    reason_sql, reason_params = _prefix_clause(reason_prefixes)
    max_items = max(0, int(limit))
    fetch_limit = max_items * 10 if max_items else 0
    rows = conn.execute(
        f"""
        SELECT
            e.id AS journal_entry_id,
            e.scope_id,
            e.session_id,
            e.turn_number,
            e.role,
            e.created_at,
            e.processed_run_id,
            e.processed_at,
            r.run_id,
            r.reason,
            r.candidate,
            r.created_at AS rejection_created_at
        FROM journal_rejections AS r
        JOIN journal_entries AS e ON e.id = r.journal_entry_id
        LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
        WHERE {reason_sql}
          AND COALESCE(e.processed_run_id, '') != ''
          -- Defensive gate: only replay the rejection for the entry's current
          -- processed run. Historical stale rejections must not reopen entries
          -- already handled by a newer digest run.
          AND r.run_id = e.processed_run_id
          AND s.memory_id IS NULL
        ORDER BY r.created_at DESC, e.id ASC
        LIMIT ?
        """,
        [*reason_params, fetch_limit],
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    # Multiple rejection rows can exist across digest attempts. Replay each
    # journal entry once so one bad entry cannot inflate operator counts/audits.
    seen_entry_ids: set[int] = set()
    for row in rows:
        entry_id = int(row["journal_entry_id"])
        if entry_id in seen_entry_ids:
            continue
        seen_entry_ids.add(entry_id)
        candidates.append(
            {
                "journal_entry_id": entry_id,
                "scope_id": str(row["scope_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "turn_number": int(row["turn_number"] or 0),
                "role": str(row["role"] or ""),
                "created_at": str(row["created_at"] or ""),
                "processed_run_id": str(row["processed_run_id"] or ""),
                "processed_at": str(row["processed_at"] or ""),
                "run_id": str(row["run_id"] or ""),
                "reason": str(row["reason"] or ""),
                "rejection_created_at": str(row["rejection_created_at"] or ""),
            }
        )
        if max_items and len(candidates) >= max_items:
            break
    return candidates


def recovery_report(
    conn: sqlite3.Connection,
    *,
    reason_prefixes: Sequence[str] = ("retry-exhausted:",),
    limit: int = 500,
) -> dict[str, Any]:
    candidates = find_replay_candidates(conn, reason_prefixes=reason_prefixes, limit=limit)
    by_reason: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    for item in candidates:
        reason = str(item["reason"])
        by_reason[reason] = by_reason.get(reason, 0) + 1
        category = classify_rejection_reason(reason)
        by_category[category] = by_category.get(category, 0) + 1
        by_scope[item["scope_id"]] = by_scope.get(item["scope_id"], 0) + 1
    return {
        "candidate_count": len(candidates),
        "reason_prefixes": list(reason_prefixes),
        "by_reason": dict(sorted(by_reason.items())),
        "by_category": dict(sorted(by_category.items())),
        "by_scope": dict(sorted(by_scope.items())),
        "items": candidates,
    }


def schedule_replay(
    conn: sqlite3.Connection,
    *,
    reason_prefixes: Sequence[str] = ("retry-exhausted:",),
    limit: int = 500,
    dry_run: bool = True,
    batch_id: str | None = None,
    actor: str = "journal.recovery.py",
) -> dict[str, Any]:
    if not dry_run:
        ensure_schema(conn)
        ensure_journal_schema(conn)
    batch = batch_id or make_batch_id("journal-recovery")
    candidates = find_replay_candidates(conn, reason_prefixes=reason_prefixes, limit=limit)
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "candidate_count": len(candidates),
        "scheduled": 0,
        "entry_ids": [item["journal_entry_id"] for item in candidates],
        "by_reason": {},
        "by_category": {},
    }
    by_reason: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for item in candidates:
        reason = str(item["reason"])
        by_reason[reason] = by_reason.get(reason, 0) + 1
        category = classify_rejection_reason(reason)
        by_category[category] = by_category.get(category, 0) + 1
    result["by_reason"] = dict(sorted(by_reason.items()))
    result["by_category"] = dict(sorted(by_category.items()))
    if dry_run or not candidates:
        return result

    now = _now_iso()
    scheduled = 0
    for item in candidates:
        entry_id = int(item["journal_entry_id"])
        before_entry = conn.execute(
            "SELECT id, scope_id, session_id, turn_number, role, processed_run_id, processed_at, created_at FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if before_entry is None:
            continue
        if str(before_entry["processed_run_id"] or "") != str(item["run_id"] or ""):
            continue
        before = dict(item)
        before["entry"] = dict(before_entry)
        conn.execute("UPDATE journal_entries SET processed_run_id = '', processed_at = NULL WHERE id = ?", (entry_id,))
        conn.execute("DELETE FROM journal_rejections WHERE journal_entry_id = ? AND run_id = ?", (entry_id, item["run_id"]))
        after = {
            "journal_entry_id": entry_id,
            "scope_id": item["scope_id"],
            "session_id": item["session_id"],
            "turn_number": item["turn_number"],
            "role": item["role"],
            "processed_run_id": "",
            "processed_at": None,
            "replay_scheduled_at": now,
        }
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type="journal_recovery",
            action="schedule_replay",
            scope_id=item["scope_id"],
            target_id=str(entry_id),
            batch_id=batch,
            before=before,
            after=after,
            reason=item["reason"],
            actor=actor,
            dry_run=False,
            created_at=now,
        )
        scheduled += 1
    conn.commit()
    result["scheduled"] = scheduled
    return result


def classify_recovery_candidates(
    conn: sqlite3.Connection,
    *,
    reason_prefixes: Sequence[str] = ("retry-exhausted:",),
    limit: int = 500,
    dry_run: bool = True,
    batch_id: str | None = None,
    classification: str = "no_replay",
    reason: str = "operator classified as no replay needed",
    actor: str = "journal.recovery.py",
) -> dict[str, Any]:
    """Classify retry/dead-letter recovery candidates as handled without replay.

    This is for operator-reviewed historical failures where replay would only
    recreate known auth/parse/noise failures or promote stale task-progress text.
    It preserves the original journal entry and rejection row but rewrites the
    current rejection reason away from retry/dead-letter prefixes so the recovery
    queue reaches a durable, auditable terminal state.
    """

    normalized_classification = str(classification or "no_replay").strip().lower().replace(" ", "_")
    safe_reason = str(reason or "").strip()
    if not normalized_classification:
        normalized_classification = "no_replay"
    if not safe_reason:
        safe_reason = "operator classified as no replay needed"
    batch = batch_id or make_batch_id("journal-recovery-classify")
    candidates = find_replay_candidates(conn, reason_prefixes=reason_prefixes, limit=limit)
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "candidate_count": len(candidates),
        "classified": 0,
        "entry_ids": [item["journal_entry_id"] for item in candidates],
        "classification": normalized_classification,
        "reason": safe_reason,
        "by_reason": {},
        "by_category": {},
    }
    by_reason: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for item in candidates:
        item_reason = str(item["reason"])
        by_reason[item_reason] = by_reason.get(item_reason, 0) + 1
        category = classify_rejection_reason(item_reason)
        by_category[category] = by_category.get(category, 0) + 1
    result["by_reason"] = dict(sorted(by_reason.items()))
    result["by_category"] = dict(sorted(by_category.items()))
    if dry_run or not candidates:
        return result

    ensure_schema(conn)
    ensure_journal_schema(conn)
    now = _now_iso()
    classified = 0
    for item in candidates:
        entry_id = int(item["journal_entry_id"])
        run_id = str(item["run_id"] or "")
        old_reason = str(item["reason"] or "")
        before_entry = conn.execute(
            "SELECT id, scope_id, session_id, turn_number, role, processed_run_id, processed_at, created_at FROM journal_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if before_entry is None:
            continue
        if str(before_entry["processed_run_id"] or "") != run_id:
            continue
        new_reason = f"operator-classified:{normalized_classification}:{old_reason}"
        before = dict(item)
        before["entry"] = dict(before_entry)
        cursor = conn.execute(
            "UPDATE journal_rejections SET reason = ?, candidate = ? WHERE journal_entry_id = ? AND run_id = ? AND reason = ?",
            (new_reason, safe_reason, entry_id, run_id, old_reason),
        )
        if cursor.rowcount != 1:
            continue
        after = {
            "journal_entry_id": entry_id,
            "scope_id": item["scope_id"],
            "session_id": item["session_id"],
            "turn_number": item["turn_number"],
            "role": item["role"],
            "processed_run_id": run_id,
            "processed_at": item["processed_at"],
            "old_reason": old_reason,
            "new_reason": new_reason,
            "classification": normalized_classification,
            "classified_at": now,
        }
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type="journal_recovery",
            action="classify_no_replay",
            scope_id=item["scope_id"],
            target_id=str(entry_id),
            batch_id=batch,
            before=before,
            after=after,
            reason=safe_reason,
            actor=actor,
            dry_run=False,
            created_at=now,
        )
        classified += 1
    conn.commit()
    result["classified"] = classified
    return result
