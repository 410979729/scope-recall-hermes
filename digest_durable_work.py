"""Read-only DurableWork projections for journal and nightly digest evidence.

Both pipelines retain their existing run/source tables, recovery rules, and
truth-writer OS lease.  This module adds a common content-free health surface;
it never claims work, rewrites provenance, or synthesizes lease authority that
the native process-lifetime lock does not persist.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from .durable_work import (
        DURABLE_WORK_ITEM_STATES,
        DurableWorkDescriptor,
        DurableWorkItem,
        canonical_snapshot_hash,
        durable_work_health,
    )
    from .writer_lease import read_truth_writer_owner
except ImportError:  # pragma: no cover - direct source-script fallback
    from durable_work import (  # type: ignore
        DURABLE_WORK_ITEM_STATES,
        DurableWorkDescriptor,
        DurableWorkItem,
        canonical_snapshot_hash,
        durable_work_health,
    )
    from writer_lease import read_truth_writer_owner  # type: ignore


JOURNAL_DURABLE_POLICY_VERSION = "journal-run-source.v1"
NIGHTLY_DURABLE_POLICY_VERSION = "nightly-run-source.v1"
JOURNAL_DURABLE_DOMAIN = "journal_digest"
NIGHTLY_DURABLE_DOMAIN = "nightly_digest"
DEFAULT_JOURNAL_MAX_ATTEMPTS = 3
JOURNAL_DURABLE_OWNER_ROLES = frozenset({"journal_digest", "provider"})
NIGHTLY_DURABLE_OWNER_ROLES = frozenset({"nightly_digest"})

_HANDLED_FALLBACK_CLASSIFICATIONS = {
    "accepted_fallback",
    "handled",
    "no_replay",
}


def _normalized_iso(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _age_seconds(value: Any, *, now: datetime | None = None) -> float:
    normalized = _normalized_iso(value)
    if not normalized:
        return 0.0
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0.0, (current - datetime.fromisoformat(normalized)).total_seconds())


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _row_mapping(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    columns = [str(item[0]) for item in (cursor.description or ())]
    return dict(zip(columns, row, strict=True))


def _safe_json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _source_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def native_digest_lease_snapshot(
    storage_dir: Path | None,
    *,
    domain_roles: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Describe the native OS lease honestly without probing or taking it."""

    owner = read_truth_writer_owner(storage_dir) if storage_dir is not None else {}
    owner_role = str(owner.get("role") or "")
    return {
        "schema_version": "native_truth_writer_lease.v1",
        "mode": "process_lifetime_os_lock",
        "state": "owner_hint_present" if owner_role else "idle_or_unobserved",
        "owner_role": owner_role,
        "owner_matches_domain": owner_role in domain_roles,
        "lease_token_persisted": False,
        "lease_generation_persisted": False,
        "lease_expiry_persisted": False,
        "crash_releases_os_authority": True,
    }


def unavailable_digest_health(
    *,
    domain_type: str,
    policy_version: str,
    reason_code: str,
    state: str = "disabled",
    storage_dir: Path | None = None,
    domain_roles: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    report = durable_work_health(
        domain_type=domain_type,
        state=state,
        reason_code=reason_code,
        auto_recoverable=False,
        operator_action_required=state in {"blocked", "needs_repair"},
        fairness={"strategy": "native_domain_scheduler"},
    )
    report.update(
        {
            "policy_version": policy_version,
            "lease": native_digest_lease_snapshot(
                storage_dir, domain_roles=domain_roles
            ),
            "retry": {"retry_count": 0, "poisoned_count": 0},
        }
    )
    return report


def _active_journal_recovery_reason(
    conn: sqlite3.Connection, *, entry_id: int, processed_run_id: str
) -> str:
    if not processed_run_id:
        return ""
    tables = _table_names(conn)
    if not {"journal_rejections", "memory_journal_sources"} <= tables:
        return ""
    source = conn.execute(
        "SELECT 1 FROM memory_journal_sources WHERE journal_entry_id=? LIMIT 1",
        (int(entry_id),),
    ).fetchone()
    if source is not None:
        return ""
    row = conn.execute(
        """
        SELECT reason
        FROM journal_rejections
        WHERE journal_entry_id=? AND run_id=?
          AND (reason LIKE 'retry-exhausted:%' OR reason LIKE 'dead-letter:%')
        ORDER BY CASE WHEN reason LIKE 'dead-letter:%' THEN 0 ELSE 1 END,
                 created_at DESC
        LIMIT 1
        """,
        (int(entry_id), processed_run_id),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _journal_entry_row(
    conn: sqlite3.Connection, entry_id: int
) -> dict[str, Any] | None:
    if "journal_entries" not in _table_names(conn):
        return None
    return _row_mapping(
        conn.execute(
            """
            SELECT id, scope_id, session_id, content_hash, created_at,
                   processed_run_id, processed_at, extraction_attempts,
                   retryable_failures
            FROM journal_entries
            WHERE id=?
            """,
            (int(entry_id),),
        )
    )


def journal_entry_descriptor(
    conn: sqlite3.Connection, entry_id: int
) -> DurableWorkDescriptor | None:
    """Project one journal source identity without loading its content."""

    row = _journal_entry_row(conn, entry_id)
    if row is None:
        return None
    identity = {
        "entry_id": int(row["id"]),
        "scope_id": str(row["scope_id"]),
        "session_id": str(row["session_id"]),
        "content_hash": str(row["content_hash"]),
    }
    return DurableWorkDescriptor(
        work_id=f"journal-entry:{int(row['id'])}",
        domain_type=JOURNAL_DURABLE_DOMAIN,
        idempotency_key=str(row["content_hash"]),
        scope_snapshot={
            "scope_id": str(row["scope_id"]),
            "session_id": str(row["session_id"]),
        },
        authority_snapshot={
            "truth_authority": "sqlite",
            "source_type": "journal_entry",
        },
        policy_version=JOURNAL_DURABLE_POLICY_VERSION,
        generation=int(row["id"]),
        frozen_upper_bound=1,
        item_set_hash=canonical_snapshot_hash(identity),
        created_at=_normalized_iso(
            row["created_at"], fallback="1970-01-01T00:00:00+00:00"
        ),
    )


def journal_entry_item(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    max_attempts: int = DEFAULT_JOURNAL_MAX_ATTEMPTS,
) -> DurableWorkItem | None:
    row = _journal_entry_row(conn, entry_id)
    if row is None:
        return None
    processed_run_id = str(row["processed_run_id"] or "")
    recovery_reason = _active_journal_recovery_reason(
        conn,
        entry_id=int(row["id"]),
        processed_run_id=processed_run_id,
    )
    retryable_failures = max(0, int(row["retryable_failures"] or 0))
    extraction_attempts = max(0, int(row["extraction_attempts"] or 0))
    attempt = max(retryable_failures, extraction_attempts)
    if recovery_reason.startswith("dead-letter:"):
        state = "poisoned"
        error_class = "poison"
        error_code = "journal_dead_letter"
    elif recovery_reason.startswith("retry-exhausted:"):
        state = "retry"
        error_class = "retriable"
        error_code = "journal_retry_exhausted"
    elif processed_run_id:
        state = "completed"
        error_class = ""
        error_code = ""
    elif retryable_failures:
        state = "retry"
        error_class = "retriable"
        error_code = "journal_retryable_failure"
    else:
        state = "pending"
        error_class = ""
        error_code = ""
    return DurableWorkItem(
        item_identity=f"journal-entry:{int(row['id'])}",
        state=state,
        attempt=attempt,
        max_attempts=max(1, int(max_attempts), attempt),
        last_error_class=error_class,
        last_error_code=error_code,
        last_progress_at=_normalized_iso(
            row["processed_at"] or row["created_at"]
        ),
        receipt={
            "journal_entry_id": int(row["id"]),
            "processed_run_id": processed_run_id,
            "content_hash": str(row["content_hash"]),
        },
    )


def journal_durable_health(
    conn: sqlite3.Connection,
    *,
    storage_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate current journal/recovery debt without reading row contents."""

    required = {
        "journal_entries",
        "journal_digest_runs",
        "journal_rejections",
        "memory_journal_sources",
    }
    if not required <= _table_names(conn):
        return unavailable_digest_health(
            domain_type=JOURNAL_DURABLE_DOMAIN,
            policy_version=JOURNAL_DURABLE_POLICY_VERSION,
            reason_code="schema_missing",
            storage_dir=storage_dir,
            domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
        )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(journal_entries)").fetchall()
    }
    required_columns = {
        "id",
        "scope_id",
        "session_id",
        "created_at",
        "processed_run_id",
        "processed_at",
        "retryable_failures",
    }
    if not required_columns <= columns:
        return unavailable_digest_health(
            domain_type=JOURNAL_DURABLE_DOMAIN,
            policy_version=JOURNAL_DURABLE_POLICY_VERSION,
            reason_code="schema_incomplete",
            state="needs_repair",
            storage_dir=storage_dir,
            domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
        )

    row = conn.execute(
        """
        WITH classified AS (
            SELECT
                e.id,
                e.scope_id,
                e.session_id,
                e.created_at,
                e.processed_at,
                COALESCE(e.processed_run_id, '') AS processed_run_id,
                COALESCE(e.retryable_failures, 0) AS retryable_failures,
                CASE WHEN EXISTS (
                    SELECT 1 FROM journal_rejections r
                    WHERE r.journal_entry_id=e.id
                      AND r.run_id=e.processed_run_id
                      AND r.reason LIKE 'dead-letter:%'
                ) AND NOT EXISTS (
                    SELECT 1 FROM memory_journal_sources s
                    WHERE s.journal_entry_id=e.id
                ) THEN 1 ELSE 0 END AS active_dead_letter,
                CASE WHEN EXISTS (
                    SELECT 1 FROM journal_rejections r
                    WHERE r.journal_entry_id=e.id
                      AND r.run_id=e.processed_run_id
                      AND r.reason LIKE 'retry-exhausted:%'
                ) AND NOT EXISTS (
                    SELECT 1 FROM memory_journal_sources s
                    WHERE s.journal_entry_id=e.id
                ) THEN 1 ELSE 0 END AS active_retry_exhausted
            FROM journal_entries e
        )
        SELECT
            SUM(CASE WHEN processed_run_id='' AND retryable_failures=0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN processed_run_id='' AND retryable_failures>0
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN processed_run_id<>'' AND active_retry_exhausted=1
                          AND active_dead_letter=0
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN processed_run_id<>'' AND active_dead_letter=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN processed_run_id<>'' AND active_dead_letter=0
                          AND active_retry_exhausted=0 THEN 1 ELSE 0 END),
            MIN(CASE WHEN processed_run_id='' OR active_dead_letter=1
                          OR active_retry_exhausted=1 THEN created_at END),
            COUNT(DISTINCT CASE WHEN processed_run_id='' THEN scope_id END),
            COUNT(DISTINCT CASE WHEN processed_run_id='' THEN scope_id || char(0) || session_id END),
            MAX(CASE WHEN processed_run_id='' OR active_retry_exhausted=1
                     THEN retryable_failures ELSE 0 END),
            MAX(CASE WHEN processed_run_id<>'' THEN processed_at END)
        FROM classified
        """
    ).fetchone()
    pending = int(row[0] or 0) if row else 0
    auto_retry = int(row[1] or 0) if row else 0
    retry_exhausted = int(row[2] or 0) if row else 0
    retry = auto_retry + retry_exhausted
    poisoned = int(row[3] or 0) if row else 0
    completed = int(row[4] or 0) if row else 0
    oldest = str(row[5] or "") if row else ""
    pending_scopes = int(row[6] or 0) if row else 0
    pending_sessions = int(row[7] or 0) if row else 0
    max_retryable_failures = int(row[8] or 0) if row else 0
    last_entry_progress = str(row[9] or "") if row else ""

    latest = _row_mapping(
        conn.execute(
            """
            SELECT id, status, started_at, finished_at
            FROM journal_digest_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        )
    ) or {}
    latest_status = str(latest.get("status") or "").strip().lower()
    last_progress_at = _normalized_iso(
        latest.get("finished_at")
        or latest.get("started_at")
        or last_entry_progress
    )
    item_counts = {state: 0 for state in DURABLE_WORK_ITEM_STATES}
    item_counts.update(
        {
            "pending": pending,
            "retry": retry,
            "completed": completed,
            "poisoned": poisoned,
        }
    )
    if poisoned:
        state = "needs_repair"
        reason_code = "dead_letter_recovery_debt"
        auto_recoverable = False
        operator_action_required = True
    elif retry_exhausted:
        state = "blocked"
        reason_code = "retry_exhausted_recovery_required"
        auto_recoverable = False
        operator_action_required = True
    elif retry:
        state = "degraded"
        reason_code = "retry_debt_present"
        auto_recoverable = True
        operator_action_required = False
    elif latest_status in {"dead_letter"}:
        state = "needs_repair"
        reason_code = "latest_run_dead_letter"
        auto_recoverable = False
        operator_action_required = True
    elif latest_status in {"error", "retry_scheduled", "running"}:
        state = "degraded"
        reason_code = f"latest_run_{latest_status}"
        auto_recoverable = True
        operator_action_required = False
    elif pending:
        state = "degraded"
        reason_code = "backlog_present"
        auto_recoverable = True
        operator_action_required = False
    else:
        state = "ready"
        reason_code = "healthy"
        auto_recoverable = True
        operator_action_required = False

    lease = native_digest_lease_snapshot(
        storage_dir, domain_roles=JOURNAL_DURABLE_OWNER_ROLES
    )
    report = durable_work_health(
        domain_type=JOURNAL_DURABLE_DOMAIN,
        state=state,
        reason_code=reason_code,
        item_counts=item_counts,
        oldest_age_seconds=_age_seconds(oldest, now=now),
        last_progress_at=last_progress_at,
        progress_rate=0.0,
        lease_expirations=0,
        lock_contention=0,
        auto_recoverable=auto_recoverable,
        operator_action_required=operator_action_required,
        fairness={
            "strategy": "oldest_entry_with_session_cursor",
            "pending_scope_count": pending_scopes,
            "pending_session_count": pending_sessions,
            "foreground_pressure": "bounded_by_digest_window",
        },
    )
    report.update(
        {
            "policy_version": JOURNAL_DURABLE_POLICY_VERSION,
            "latest_run_id": str(latest.get("id") or ""),
            "latest_run_status": latest_status,
            "lease": lease,
            "retry": {
                "retry_count": retry,
                "auto_retry_count": auto_retry,
                "retry_exhausted_count": retry_exhausted,
                "poisoned_count": poisoned,
                "max_retryable_failures": max_retryable_failures,
            },
        }
    )
    return report


def _nightly_run_row(
    conn: sqlite3.Connection, run_id: str
) -> dict[str, Any] | None:
    if "nightly_digest_runs" not in _table_names(conn):
        return None
    return _row_mapping(
        conn.execute(
            """
            SELECT id, digest_date, source_db, started_at, finished_at, status,
                   dry_run, inserted, updated, skipped, deleted, metadata
            FROM nightly_digest_runs
            WHERE id=?
            """,
            (str(run_id),),
        )
    )


def _nightly_state(row: Mapping[str, Any]) -> tuple[str, str, str, int]:
    status = str(row.get("status") or "").strip().lower()
    metadata = _safe_json_object(row.get("metadata"))
    fallbacks = metadata.get("extractor_fallbacks")
    fallback_rows = [item for item in fallbacks if isinstance(item, dict)] if isinstance(fallbacks, list) else []
    attempts = max(
        [max(0, int(item.get("attempts") or 0)) for item in fallback_rows]
        or [0]
    )
    retryable = any(bool(item.get("retryable")) for item in fallback_rows)
    if status == "dry_run" or bool(row.get("dry_run")):
        return "cancelled", "", "", attempts
    if status == "running":
        return "processing", "", "", attempts
    if status == "error":
        if retryable:
            return "retry", "retriable", "nightly_retryable_failure", attempts
        return "poisoned", "poison", "nightly_unclassified_failure", attempts
    if status in {"ok", "ok_with_fallback"}:
        return "completed", "", "", attempts
    return "poisoned", "permanent", "nightly_unknown_run_status", attempts


def nightly_run_descriptor(
    conn: sqlite3.Connection, run_id: str
) -> DurableWorkDescriptor | None:
    """Project a persisted nightly run while hashing the local source path."""

    row = _nightly_run_row(conn, run_id)
    if row is None:
        return None
    source_db_hash = _source_hash(row["source_db"])
    identity = {
        "run_id": str(row["id"]),
        "digest_date": str(row["digest_date"]),
        "source_db_hash": source_db_hash,
    }
    generation = int(hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()[:15], 16)
    return DurableWorkDescriptor(
        work_id=f"nightly-run:{row['id']}",
        domain_type=NIGHTLY_DURABLE_DOMAIN,
        idempotency_key=str(row["id"]),
        scope_snapshot={"digest_date": str(row["digest_date"])},
        authority_snapshot={
            "truth_authority": "sqlite",
            "source_db_hash": source_db_hash,
        },
        policy_version=NIGHTLY_DURABLE_POLICY_VERSION,
        generation=generation,
        frozen_upper_bound=1,
        item_set_hash=canonical_snapshot_hash(identity),
        created_at=_normalized_iso(
            row["started_at"], fallback="1970-01-01T00:00:00+00:00"
        ),
    )


def nightly_run_item(
    conn: sqlite3.Connection, run_id: str
) -> DurableWorkItem | None:
    row = _nightly_run_row(conn, run_id)
    if row is None:
        return None
    state, error_class, error_code, attempts = _nightly_state(row)
    source_links = 0
    if "memory_digest_sources" in _table_names(conn):
        source_links = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_digest_sources WHERE run_id=?",
                (str(row["id"]),),
            ).fetchone()[0]
            or 0
        )
    return DurableWorkItem(
        item_identity=f"nightly-run:{row['id']}",
        state=state,
        attempt=attempts,
        max_attempts=max(1, attempts),
        last_error_class=error_class,
        last_error_code=error_code,
        last_progress_at=_normalized_iso(
            row["finished_at"] or row["started_at"]
        ),
        receipt={
            "run_id": str(row["id"]),
            "digest_date": str(row["digest_date"]),
            "source_db_hash": _source_hash(row["source_db"]),
            "source_link_count": source_links,
            "native_status": str(row["status"]),
        },
    )


def nightly_durable_health(
    conn: sqlite3.Connection,
    *,
    storage_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report the latest nightly work result; older runs remain provenance."""

    if "nightly_digest_runs" not in _table_names(conn):
        return unavailable_digest_health(
            domain_type=NIGHTLY_DURABLE_DOMAIN,
            policy_version=NIGHTLY_DURABLE_POLICY_VERSION,
            reason_code="schema_missing",
            storage_dir=storage_dir,
            domain_roles=NIGHTLY_DURABLE_OWNER_ROLES,
        )
    latest = _row_mapping(
        conn.execute(
            """
            SELECT id, digest_date, source_db, started_at, finished_at, status,
                   dry_run, inserted, updated, skipped, deleted, metadata
            FROM nightly_digest_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """
        )
    )
    lease = native_digest_lease_snapshot(
        storage_dir, domain_roles=NIGHTLY_DURABLE_OWNER_ROLES
    )
    if latest is None:
        report = durable_work_health(
            domain_type=NIGHTLY_DURABLE_DOMAIN,
            state="ready",
            reason_code="no_runs_yet",
            auto_recoverable=True,
            operator_action_required=False,
            fairness={"strategy": "scheduled_digest_date_order"},
        )
        report.update(
            {
                "policy_version": NIGHTLY_DURABLE_POLICY_VERSION,
                "latest_run_id": "",
                "latest_run_status": "",
                "lease": lease,
                "retry": {"retry_count": 0, "poisoned_count": 0},
                "historical_quarantine_count": 0,
            }
        )
        return report

    state_name, error_class, _error_code, _attempts = _nightly_state(latest)
    metadata = _safe_json_object(latest.get("metadata"))
    classification = str(metadata.get("operator_classification") or "").strip()
    native_status = str(latest.get("status") or "").strip().lower()
    item_counts = {state: 0 for state in DURABLE_WORK_ITEM_STATES}
    item_counts[state_name] = 1
    if state_name == "poisoned":
        state = "needs_repair"
        reason_code = "latest_run_poisoned"
        auto_recoverable = False
        operator_action_required = True
    elif state_name in {"retry", "processing"}:
        state = "degraded"
        reason_code = f"latest_run_{state_name}"
        auto_recoverable = True
        operator_action_required = False
    elif (
        native_status == "ok_with_fallback"
        and classification not in _HANDLED_FALLBACK_CLASSIFICATIONS
    ):
        state = "degraded"
        reason_code = "latest_run_fallback"
        auto_recoverable = True
        operator_action_required = False
    else:
        state = "ready"
        reason_code = "healthy"
        auto_recoverable = True
        operator_action_required = False
    quarantine_count = 0
    if "nightly_digest_quarantine" in _table_names(conn):
        quarantine_count = int(
            conn.execute("SELECT COUNT(*) FROM nightly_digest_quarantine").fetchone()[0]
            or 0
        )
    last_progress_at = _normalized_iso(
        latest.get("finished_at") or latest.get("started_at")
    )
    debt_age = (
        _age_seconds(latest.get("started_at"), now=now)
        if state_name in {"processing", "retry", "poisoned"}
        else 0.0
    )
    report = durable_work_health(
        domain_type=NIGHTLY_DURABLE_DOMAIN,
        state=state,
        reason_code=reason_code,
        item_counts=item_counts,
        oldest_age_seconds=debt_age,
        last_progress_at=last_progress_at,
        progress_rate=0.0,
        lease_expirations=0,
        lock_contention=0,
        auto_recoverable=auto_recoverable,
        operator_action_required=operator_action_required,
        fairness={
            "strategy": "scheduled_digest_date_order",
            "source_authority": "run_receipt_and_source_links",
            "foreground_pressure": "bounded_session_limit",
        },
    )
    report.update(
        {
            "policy_version": NIGHTLY_DURABLE_POLICY_VERSION,
            "latest_run_id": str(latest.get("id") or ""),
            "latest_run_status": native_status,
            "lease": lease,
            "retry": {
                "retry_count": int(state_name == "retry"),
                "poisoned_count": int(state_name == "poisoned"),
                "last_error_class": error_class,
            },
            "historical_quarantine_count": quarantine_count,
        }
    )
    return report


__all__ = [
    "DEFAULT_JOURNAL_MAX_ATTEMPTS",
    "JOURNAL_DURABLE_DOMAIN",
    "JOURNAL_DURABLE_OWNER_ROLES",
    "JOURNAL_DURABLE_POLICY_VERSION",
    "NIGHTLY_DURABLE_DOMAIN",
    "NIGHTLY_DURABLE_OWNER_ROLES",
    "NIGHTLY_DURABLE_POLICY_VERSION",
    "journal_durable_health",
    "journal_entry_descriptor",
    "journal_entry_item",
    "native_digest_lease_snapshot",
    "nightly_durable_health",
    "nightly_run_descriptor",
    "nightly_run_item",
    "unavailable_digest_health",
]
