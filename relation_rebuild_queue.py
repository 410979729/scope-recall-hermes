"""Durable, bounded relation-rebuild debt for the SQLite truth store.

Foreground memory mutations may refresh a small deterministic neighbourhood, but
must not scan an entire large scope.  This queue records the remaining focus-row
debt in the same SQLite transaction.  A lease/CAS worker then rebuilds one stable
peer-id chunk at a time; relation writes and cursor advancement commit together.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NoReturn

try:
    from .capture_filters import sanitize_report_text
    from .graph import lifecycle_visible_sql
    from .relation_frequency_index import (
        ensure_relation_frequency_index_schema,
        relation_frequency_index_schema_status,
        relation_frequency_snapshot,
    )
    from .relation_scope_state import (
        ensure_relation_scope_state_schema,
        relation_scope_state_schema_status,
    )
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from capture_filters import sanitize_report_text
    from graph import lifecycle_visible_sql
    from relation_frequency_index import (  # type: ignore[no-redef]
        ensure_relation_frequency_index_schema,
        relation_frequency_index_schema_status,
        relation_frequency_snapshot,
    )
    from relation_scope_state import (  # type: ignore[no-redef]
        ensure_relation_scope_state_schema,
        relation_scope_state_schema_status,
    )
    from sqlite_schema import execute_script_transaction_neutral

_QUEUE_STATUSES = ("pending", "retry", "processing", "completed", "dead_letter")
_CORPUS_CHANGE_IMMEDIATE_RETRIES = 1
_CORPUS_CHANGE_FENCE_AFTER = _CORPUS_CHANGE_IMMEDIATE_RETRIES + 1
_CORPUS_CHANGE_FENCE_PAIR_LIMIT = 1
_CORPUS_CHANGE_BACKOFF_BASE_SECONDS = 1
_CORPUS_CHANGE_BACKOFF_MAX_SECONDS = 60
RELATION_REBUILD_SCHEMA_VERSION = 10801
RELATION_REBUILD_MIGRATION_ID = "0003_relation_rebuild_queue_v1_8_0"
RELATION_REBUILD_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_REBUILD_MIGRATION_DESCRIPTION = (
    "Durable bounded relation rebuild queue and lease state"
)
RELATION_REBUILD_LEASE_SCHEMA_VERSION = 10803
RELATION_REBUILD_LEASE_MIGRATION_ID = "0005_relation_rebuild_lease_token_v1_8_0"
RELATION_REBUILD_LEASE_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_REBUILD_LEASE_MIGRATION_DESCRIPTION = (
    "Bind relation rebuild completion and failure CAS to an immutable lease token"
)
RELATION_REBUILD_PROGRESS_SCHEMA_VERSION = 10806
RELATION_REBUILD_PROGRESS_MIGRATION_ID = "0008_relation_rebuild_progress_v1_8_0"
RELATION_REBUILD_PROGRESS_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_REBUILD_PROGRESS_MIGRATION_DESCRIPTION = (
    "Monotonic recoverable relation rebuild pass progress"
)
RELATION_REBUILD_EXPIRY_SCHEMA_VERSION = 10808
RELATION_REBUILD_EXPIRY_MIGRATION_ID = (
    "0010_relation_rebuild_lease_expiry_budget_v1_8_0"
)
RELATION_REBUILD_EXPIRY_MIGRATION_PLUGIN_VERSION = "1.8.0"
RELATION_REBUILD_EXPIRY_MIGRATION_DESCRIPTION = (
    "Bound repeated relation rebuild lease expirations with durable counters"
)
_REQUIRED_COLUMNS = {
    "id",
    "scope_id",
    "focus_memory_id",
    "requested_updated_at",
    "next_requested_updated_at",
    "reason",
    "status",
    "cursor_memory_id",
    "processed_pairs",
    "pass_processed_pairs",
    "pass_number",
    "supersession_count",
    "last_progress_at",
    "attempts",
    "lease_expirations",
    "pass_lease_expirations",
    "failures",
    "pass_failures",
    "available_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "corpus_revision",
    "blocked_entities_json",
    "blocked_entities_sha256",
    "last_error",
    "created_at",
    "updated_at",
    "completed_at",
}


class _RelationRebuildSuperseded(RuntimeError):
    """Signal that a newer revision or lease token replaced this worker."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _corpus_change_backoff_seconds(supersession_count: int) -> int:
    """Bound immediate corpus-churn retries without discarding queue debt."""

    count = max(0, int(supersession_count))
    if count <= _CORPUS_CHANGE_IMMEDIATE_RETRIES:
        return 0
    exponent = min(
        count - _CORPUS_CHANGE_IMMEDIATE_RETRIES - 1,
        6,
    )
    return min(
        _CORPUS_CHANGE_BACKOFF_MAX_SECONDS,
        _CORPUS_CHANGE_BACKOFF_BASE_SECONDS * (2**exponent),
    )


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_rebuild_queue'"
        ).fetchone()
        is not None
    )


def ensure_relation_rebuild_schema(conn: sqlite3.Connection) -> None:
    """Create the additive relation-debt table without committing caller work."""

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS relation_rebuild_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL,
            focus_memory_id TEXT NOT NULL,
            requested_updated_at TEXT NOT NULL DEFAULT '',
            next_requested_updated_at TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','retry','processing','completed','dead_letter')),
            cursor_memory_id TEXT NOT NULL DEFAULT '',
            processed_pairs INTEGER NOT NULL DEFAULT 0,
            pass_processed_pairs INTEGER NOT NULL DEFAULT 0,
            pass_number INTEGER NOT NULL DEFAULT 1,
            supersession_count INTEGER NOT NULL DEFAULT 0,
            last_progress_at TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_expirations INTEGER NOT NULL DEFAULT 0,
            pass_lease_expirations INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            pass_failures INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT,
            corpus_revision INTEGER NOT NULL DEFAULT 0,
            blocked_entities_json TEXT NOT NULL DEFAULT '[]',
            blocked_entities_sha256 TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(scope_id, focus_memory_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relation_rebuild_queue_claim
            ON relation_rebuild_queue(status, available_at, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_relation_rebuild_queue_scope
            ON relation_rebuild_queue(scope_id, status, focus_memory_id);
        """,
    )
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(relation_rebuild_queue)")
    }
    column_migrations = {
        "lease_token": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"
        ),
        "corpus_revision": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN corpus_revision INTEGER NOT NULL DEFAULT 0"
        ),
        "blocked_entities_json": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN blocked_entities_json TEXT NOT NULL DEFAULT '[]'"
        ),
        "blocked_entities_sha256": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN blocked_entities_sha256 TEXT NOT NULL DEFAULT ''"
        ),
        "next_requested_updated_at": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN next_requested_updated_at TEXT NOT NULL DEFAULT ''"
        ),
        "pass_processed_pairs": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN pass_processed_pairs INTEGER NOT NULL DEFAULT 0"
        ),
        "pass_number": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN pass_number INTEGER NOT NULL DEFAULT 1"
        ),
        "supersession_count": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN supersession_count INTEGER NOT NULL DEFAULT 0"
        ),
        "last_progress_at": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN last_progress_at TEXT NOT NULL DEFAULT ''"
        ),
        "pass_failures": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN pass_failures INTEGER NOT NULL DEFAULT 0"
        ),
        "lease_expirations": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN lease_expirations INTEGER NOT NULL DEFAULT 0"
        ),
        "pass_lease_expirations": (
            "ALTER TABLE relation_rebuild_queue "
            "ADD COLUMN pass_lease_expirations INTEGER NOT NULL DEFAULT 0"
        ),
    }
    added_columns: set[str] = set()
    for column, statement in column_migrations.items():
        if column not in columns:
            conn.execute(statement)
            added_columns.add(column)
    if "pass_processed_pairs" in added_columns:
        conn.execute(
            """
            UPDATE relation_rebuild_queue
            SET pass_processed_pairs=processed_pairs,
                last_progress_at=CASE
                    WHEN processed_pairs>0 THEN updated_at ELSE last_progress_at END
            """
        )
    ensure_relation_scope_state_schema(conn)
    ensure_relation_frequency_index_schema(conn)


def relation_rebuild_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the queue and scope-receipt schema contract without mutation."""

    scope_state = relation_scope_state_schema_status(conn)
    frequency_index = relation_frequency_index_schema_status(conn)
    if not _table_exists(conn):
        return {
            "current": False,
            "schema_version": RELATION_REBUILD_EXPIRY_SCHEMA_VERSION,
            "table_present": False,
            "missing_columns": sorted(_REQUIRED_COLUMNS),
            "scope_state": scope_state,
            "frequency_index": frequency_index,
        }
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(relation_rebuild_queue)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    return {
        "current": (
            not missing
            and bool(scope_state.get("current"))
            and bool(frequency_index.get("current"))
        ),
        "schema_version": RELATION_REBUILD_EXPIRY_SCHEMA_VERSION,
        "table_present": True,
        "missing_columns": missing,
        "scope_state": scope_state,
        "frequency_index": frequency_index,
    }


def enqueue_relation_rebuild(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    focus_memory_id: str,
    requested_updated_at: str,
    reason: str,
    commit: bool = False,
    force: bool = False,
) -> int:
    """Persist one focus pass without discarding in-flight progress.

    A write arriving during a partially processed pass records only
    ``next_requested_updated_at``.  The current lease and cursor remain valid;
    completion atomically promotes the newest requested revision into a fresh
    pass while lifetime progress/attempt counters remain monotonic.
    """

    clean_scope = str(scope_id or "").strip()
    clean_focus = str(focus_memory_id or "").strip()
    clean_requested = str(requested_updated_at or "")
    if not clean_scope or not clean_focus:
        raise ValueError("scope_id and focus_memory_id are required")
    now = _iso()
    safe_reason = sanitize_report_text(
        str(reason or "relation rebuild deferred")
    )[:500]
    row = conn.execute(
        """
        SELECT id, requested_updated_at, next_requested_updated_at, status,
               cursor_memory_id, pass_processed_pairs
        FROM relation_rebuild_queue
        WHERE scope_id=? AND focus_memory_id=?
        """,
        (clean_scope, clean_focus),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO relation_rebuild_queue(
                scope_id, focus_memory_id, requested_updated_at,
                next_requested_updated_at, reason, status,
                cursor_memory_id, processed_pairs, pass_processed_pairs,
                pass_number, supersession_count, last_progress_at,
                attempts, lease_expirations, pass_lease_expirations,
                failures, pass_failures, available_at,
                lease_owner, lease_token,
                lease_expires_at, corpus_revision, blocked_entities_json,
                blocked_entities_sha256, last_error, created_at, updated_at,
                completed_at
            ) VALUES(
                ?, ?, ?, '', ?, 'pending', '', 0, 0, 1, 0, '',
                0, 0, 0, 0, 0, ?, '', '', NULL, 0, '[]', '', '', ?, ?, NULL
            )
            """,
            (clean_scope, clean_focus, clean_requested, safe_reason, now, now, now),
        )
    else:
        event_id = int(row[0])
        active_requested = str(row[1] or "")
        next_requested = str(row[2] or "")
        status = str(row[3] or "")
        has_progress = bool(str(row[4] or "")) or int(row[5] or 0) > 0
        duplicate = clean_requested in {active_requested, next_requested}
        active_pass = status == "processing" or (
            status in {"pending", "retry"} and has_progress
        )
        if active_pass:
            if force or not duplicate:
                conn.execute(
                    """
                    UPDATE relation_rebuild_queue
                    SET next_requested_updated_at=?, reason=?,
                        supersession_count=supersession_count+1,
                        updated_at=?, completed_at=NULL
                    WHERE id=?
                    """,
                    (clean_requested, safe_reason, now, event_id),
                )
        elif force or status == "dead_letter" or not duplicate:
            conn.execute(
                """
                UPDATE relation_rebuild_queue
                SET requested_updated_at=?, next_requested_updated_at='',
                    reason=?, status='pending', cursor_memory_id='',
                    pass_processed_pairs=0, pass_number=pass_number+1,
                    pass_failures=0, pass_lease_expirations=0,
                    available_at=?, lease_owner='', lease_token='',
                    lease_expires_at=NULL, corpus_revision=0,
                    blocked_entities_json='[]', blocked_entities_sha256='',
                    last_error='', updated_at=?, completed_at=NULL
                WHERE id=?
                """,
                (clean_requested, safe_reason, now, now, event_id),
            )
    persisted = conn.execute(
        """
        SELECT id FROM relation_rebuild_queue
        WHERE scope_id=? AND focus_memory_id=?
        """,
        (clean_scope, clean_focus),
    ).fetchone()
    if commit:
        conn.commit()
    if persisted is None:
        raise RuntimeError("relation rebuild event was not persisted")
    return int(persisted[0])


def resolve_relation_rebuild(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    focus_memory_id: str,
    requested_updated_at: str,
    commit: bool = False,
) -> int:
    """Mark obsolete debt complete when foreground sync covered every peer."""

    now = _iso()
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET requested_updated_at=?, next_requested_updated_at='',
            status='completed', lease_owner='', lease_token='', lease_expires_at=NULL,
            available_at=?, updated_at=?, completed_at=?, last_error='',
            cursor_memory_id='', pass_processed_pairs=0, pass_failures=0,
            pass_lease_expirations=0,
            pass_number=pass_number+CASE
                WHEN requested_updated_at <> ? THEN 1 ELSE 0 END
        WHERE scope_id=? AND focus_memory_id=?
          AND requested_updated_at <= ?
          AND (next_requested_updated_at='' OR next_requested_updated_at <= ?)
          AND status IN ('pending','retry','processing','dead_letter')
        """,
        (
            str(requested_updated_at or ""),
            now,
            now,
            now,
            str(requested_updated_at or ""),
            str(scope_id or ""),
            str(focus_memory_id or ""),
            str(requested_updated_at or ""),
            str(requested_updated_at or ""),
        ),
    ).rowcount
    if commit:
        conn.commit()
    return int(changed)


def claim_relation_rebuild_events(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 120,
    max_lease_expirations: int = 5,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Claim debt and bound repeated worker loss with a durable expiry budget."""

    clean_worker = str(worker_id or "").strip()
    if not clean_worker:
        raise ValueError("worker_id is required")
    bounded_limit = max(1, min(int(limit), 100))
    expiry_budget = max(1, int(max_lease_expirations))
    now_dt = _now()
    now = _iso(now_dt)
    lease_expires = _iso(now_dt + timedelta(seconds=max(0, int(lease_seconds))))
    conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET lease_expirations=lease_expirations+1,
            pass_lease_expirations=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 0 ELSE pass_lease_expirations+1 END,
            status=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 'pending' ELSE 'dead_letter' END,
            requested_updated_at=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN next_requested_updated_at ELSE requested_updated_at END,
            next_requested_updated_at=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '' ELSE next_requested_updated_at END,
            cursor_memory_id=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '' ELSE cursor_memory_id END,
            pass_processed_pairs=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 0 ELSE pass_processed_pairs END,
            pass_failures=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 0 ELSE pass_failures END,
            pass_number=pass_number+CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 1 ELSE 0 END,
            corpus_revision=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 0 ELSE corpus_revision END,
            blocked_entities_json=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '[]' ELSE blocked_entities_json END,
            blocked_entities_sha256=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '' ELSE blocked_entities_sha256 END,
            lease_owner='', lease_token='', lease_expires_at=NULL,
            available_at=:now, updated_at=:now, completed_at=NULL,
            last_error=CASE
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 'processing lease expiry budget exhausted; promoted newer revision'
                ELSE 'processing lease expiry budget exhausted' END
        WHERE status='processing'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= :now
          AND pass_lease_expirations+1 >= :expiry_budget
        """,
        {"now": now, "expiry_budget": expiry_budget},
    )
    conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET status='retry', lease_owner='', lease_token='', lease_expires_at=NULL,
            lease_expirations=lease_expirations+1,
            pass_lease_expirations=pass_lease_expirations+1,
            available_at=?, updated_at=?,
            last_error=CASE WHEN last_error='' THEN 'processing lease expired' ELSE last_error END
        WHERE status='processing'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= ?
        """,
        (now, now, now),
    )
    candidates = conn.execute(
        """
        SELECT id
        FROM relation_rebuild_queue
        WHERE status IN ('pending','retry') AND available_at <= ?
        ORDER BY available_at, created_at, id
        LIMIT ?
        """,
        (now, bounded_limit),
    ).fetchall()
    claimed_ids: list[int] = []
    for candidate in candidates:
        event_id = int(candidate[0])
        lease_token = uuid.uuid4().hex
        changed = conn.execute(
            """
            UPDATE relation_rebuild_queue
            SET status='processing', lease_owner=?, lease_token=?, lease_expires_at=?,
                attempts=attempts+1, updated_at=?
            WHERE id=? AND status IN ('pending','retry') AND available_at <= ?
            """,
            (clean_worker, lease_token, lease_expires, now, event_id, now),
        ).rowcount
        if changed == 1:
            claimed_ids.append(event_id)
    rows: list[dict[str, Any]] = []
    for event_id in claimed_ids:
        cursor = conn.execute(
            "SELECT * FROM relation_rebuild_queue WHERE id=?", (event_id,)
        )
        raw = cursor.fetchone()
        if raw is None:
            continue
        columns = [str(item[0]) for item in cursor.description or []]
        rows.append({column: raw[index] for index, column in enumerate(columns)})
    if commit:
        conn.commit()
    return rows


def _complete_missing_focus(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
) -> bool:
    now = _iso()
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET status='completed', next_requested_updated_at='', cursor_memory_id='',
            pass_processed_pairs=0, pass_failures=0, pass_lease_expirations=0,
            lease_owner='', lease_token='',
            lease_expires_at=NULL, updated_at=?, completed_at=?, last_error=''
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            now,
            now,
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).rowcount
    return changed == 1


def _defer_for_frequency_index(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
) -> NoReturn:
    """Release a lease without changing monotonic progress while the index catches up."""

    if conn.in_transaction:
        conn.rollback()
    now = _iso()
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET status='pending', available_at=?, lease_owner='', lease_token='',
            lease_expires_at=NULL, last_error='relation frequency index pending',
            updated_at=?, completed_at=NULL
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            now,
            now,
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).rowcount
    conn.commit()
    if changed != 1:
        raise _RelationRebuildSuperseded(
            "relation rebuild lease changed while deferring frequency maintenance"
        )
    raise _RelationRebuildSuperseded(
        "relation rebuild deferred until incremental frequency index is current"
    )


def _defer_for_corpus_change(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
) -> NoReturn:
    """Rollback stale work, record supersession, and yield with bounded cooldown.

    The cursor and processed-pair counters deliberately stay untouched: a stale
    chunk has no committed relation evidence and remains debt.  The lease/token
    CAS is still required both when reading the counter and when releasing the
    lease, so an old worker cannot modify a replacement claim.
    """

    if conn.in_transaction:
        conn.rollback()
    now_dt = _now()
    now = _iso(now_dt)
    current = conn.execute(
        """
        SELECT supersession_count
        FROM relation_rebuild_queue
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).fetchone()
    if current is None:
        raise _RelationRebuildSuperseded(
            "relation rebuild lease changed while reading corpus supersession state"
        )
    next_supersession_count = int(current[0] or 0) + 1
    delay_seconds = _corpus_change_backoff_seconds(next_supersession_count)
    available_at = _iso(now_dt + timedelta(seconds=delay_seconds))
    last_error = "scope corpus changed before relation commit"
    if delay_seconds:
        last_error += (
            f"; retry cooldown {delay_seconds}s after corpus supersession "
            f"#{next_supersession_count}"
        )
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET status='pending', available_at=?, lease_owner='', lease_token='',
            lease_expires_at=NULL, corpus_revision=0,
            blocked_entities_json='[]', blocked_entities_sha256='',
            supersession_count=supersession_count+1,
            last_error=?, updated_at=?, completed_at=NULL
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            available_at,
            sanitize_report_text(last_error)[:500],
            now,
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).rowcount
    conn.commit()
    if changed != 1:
        raise _RelationRebuildSuperseded(
            "relation rebuild lease changed while deferring stale corpus"
        )
    raise _RelationRebuildSuperseded(
        "relation rebuild deferred after scope corpus revision changed"
    )


def _prepare_frequency_snapshot(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
    commit_receipt: bool = True,
) -> tuple[int, set[str]]:
    """Bind an indexed receipt, optionally retaining a caller-owned write fence."""

    scope_id = str(event["scope_id"])
    snapshot = relation_frequency_snapshot(
        conn,
        scope_id,
        bounded_repair_limit=0,
    )
    if snapshot is None:
        _defer_for_frequency_index(conn, event=event, worker_id=worker_id)
    revision = int(snapshot["corpus_revision"])
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET corpus_revision=?, blocked_entities_json=?,
            blocked_entities_sha256=?, updated_at=?, last_error=''
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            revision,
            str(snapshot["blocked_entities_json"]),
            str(snapshot["blocked_entities_sha256"]),
            _iso(),
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).rowcount
    if changed != 1:
        conn.rollback()
        raise _RelationRebuildSuperseded(
            "relation rebuild lease changed before indexed receipt commit"
        )
    if commit_receipt:
        conn.commit()
    return revision, set(snapshot["blocked_entities"])


def _process_relation_chunk(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
    pair_limit: int,
    max_candidates: int,
) -> tuple[int, bool]:
    """Rebuild one deterministic peer-id chunk and advance its cursor atomically."""

    try:
        from .relation_extraction import rebuild_extracted_relations
    except ImportError:  # pragma: no cover - direct source-script execution fallback
        from relation_extraction import rebuild_extracted_relations  # type: ignore[no-redef]

    event_id = int(event["id"])
    scope_id = str(event["scope_id"])
    focus_id = str(event["focus_memory_id"])
    cursor_memory_id = str(event.get("cursor_memory_id") or "")
    fence_corpus = (
        int(event.get("supersession_count") or 0) >= _CORPUS_CHANGE_FENCE_AFTER
    )
    if fence_corpus:
        # Escalate only after bounded optimistic retries.  The reservation is
        # held for this one bounded chunk, so corpus truth, its frequency
        # receipt, relation writes, and cursor advancement share one snapshot.
        conn.execute("BEGIN IMMEDIATE")
    focus = conn.execute(
        f"""
        SELECT m.id
        FROM memories m
        WHERE m.id=? AND m.scope_id=? AND {lifecycle_visible_sql('m')}
        """,
        (focus_id, scope_id),
    ).fetchone()
    if focus is None:
        if not _complete_missing_focus(conn, event=event, worker_id=worker_id):
            raise _RelationRebuildSuperseded(
                "relation rebuild lease changed before completion"
            )
        conn.commit()
        return 0, True

    corpus_revision, blocked_entities = _prepare_frequency_snapshot(
        conn,
        event=event,
        worker_id=worker_id,
        commit_receipt=not fence_corpus,
    )

    bounded_pairs = max(1, min(int(pair_limit), 5000))
    if fence_corpus:
        # Once a corpus fence is needed, keep the write lock to one pair so a
        # long caller chunk cannot starve concurrent corpus writers.
        bounded_pairs = min(bounded_pairs, _CORPUS_CHANGE_FENCE_PAIR_LIMIT)
    peer_rows = conn.execute(
        f"""
        SELECT m.id
        FROM memories m
        WHERE m.scope_id=?
          AND m.id<>?
          AND m.id>?
          AND {lifecycle_visible_sql('m')}
        ORDER BY m.id
        LIMIT ?
        """,
        (scope_id, focus_id, cursor_memory_id, bounded_pairs + 1),
    ).fetchall()
    peer_ids = [str(row[0]) for row in peer_rows[:bounded_pairs]]
    if peer_ids:
        candidate_cap = max(1, int(max_candidates))
        selected_peer_ids = list(peer_ids)
        preview: dict[str, Any] = {}
        while selected_peer_ids:
            preview = rebuild_extracted_relations(
                conn,
                scope_ids=[scope_id],
                memory_ids=[focus_id, *selected_peer_ids],
                focus_memory_ids=[focus_id],
                dry_run=True,
                batch_id=f"relation-queue-{event_id}-preview",
                max_pairs=len(selected_peer_ids),
                max_candidates=0,
                commit=False,
                blocked_entities=blocked_entities,
            )
            if not bool(preview.get("ok")):
                raise RuntimeError(
                    str(preview.get("error") or "bounded relation preview was blocked")
                )
            candidate_count = int(preview.get("candidate_count") or 0)
            if candidate_count <= candidate_cap or len(selected_peer_ids) == 1:
                break
            scaled_size = max(
                1,
                min(
                    len(selected_peer_ids) - 1,
                    (len(selected_peer_ids) * candidate_cap) // candidate_count,
                ),
            )
            selected_peer_ids = selected_peer_ids[:scaled_size]
        peer_ids = selected_peer_ids
        preview_candidate_count = int(preview.get("candidate_count") or 0)
        # One pair is the indivisible queue unit. The relation type set is
        # fixed and bounded, so process that pair once instead of retrying the
        # same deterministic cap error until dead-letter.
        effective_candidate_cap = (
            0
            if len(peer_ids) == 1 and preview_candidate_count > candidate_cap
            else candidate_cap
        )
        rebuilt = rebuild_extracted_relations(
            conn,
            scope_ids=[scope_id],
            memory_ids=[focus_id, *peer_ids],
            focus_memory_ids=[focus_id],
            dry_run=False,
            batch_id=f"relation-queue-{event_id}",
            max_pairs=len(peer_ids),
            max_candidates=effective_candidate_cap,
            commit=False,
            blocked_entities=blocked_entities,
        )
        if not bool(rebuilt.get("ok")):
            raise RuntimeError(
                str(rebuilt.get("error") or "bounded relation rebuild was blocked")
            )
    has_more = len(peer_rows) > len(peer_ids)
    last_cursor = peer_ids[-1] if peer_ids else cursor_memory_id
    now = _iso()
    updated = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET processed_pairs=processed_pairs+:processed,
            pass_processed_pairs=CASE
                WHEN :has_more=0
                 AND next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 0
                ELSE pass_processed_pairs+:processed
            END,
            last_progress_at=CASE
                WHEN :processed>0 THEN :now ELSE last_progress_at END,
            status=CASE
                WHEN :has_more=1 THEN 'pending'
                WHEN next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 'pending'
                ELSE 'completed'
            END,
            cursor_memory_id=CASE
                WHEN :has_more=0
                 AND next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '' ELSE :last_cursor
            END,
            requested_updated_at=CASE
                WHEN :has_more=0
                 AND next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN next_requested_updated_at ELSE requested_updated_at
            END,
            next_requested_updated_at=CASE
                WHEN :has_more=0
                 AND next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN '' ELSE next_requested_updated_at
            END,
            pass_number=pass_number+CASE
                WHEN :has_more=0
                 AND next_requested_updated_at<>''
                 AND next_requested_updated_at<>requested_updated_at
                THEN 1 ELSE 0
            END,
            lease_owner='', lease_token='', lease_expires_at=NULL,
            available_at=:now, updated_at=:now,
            completed_at=CASE
                WHEN :has_more=0
                 AND (next_requested_updated_at=''
                      OR next_requested_updated_at=requested_updated_at)
                THEN :now ELSE NULL
            END,
            pass_failures=0, pass_lease_expirations=0,
            last_error=''
        WHERE id=:event_id AND status='processing' AND lease_owner=:worker_id
          AND lease_token=:lease_token
          AND requested_updated_at=:requested_updated_at
          AND corpus_revision=:corpus_revision
          AND EXISTS (
              SELECT 1
              FROM relation_scope_statistics scope_state
              WHERE scope_state.scope_id=:scope_id
                AND scope_state.corpus_revision=:corpus_revision
                AND scope_state.statistics_revision=:corpus_revision
                AND scope_state.blocked_entities_sha256=
                    relation_rebuild_queue.blocked_entities_sha256
          )
        RETURNING status, requested_updated_at, pass_number,
                  processed_pairs, pass_processed_pairs
        """,
        {
            "processed": len(peer_ids),
            "has_more": int(has_more),
            "now": now,
            "last_cursor": last_cursor,
            "event_id": event_id,
            "worker_id": worker_id,
            "lease_token": str(event.get("lease_token") or ""),
            "requested_updated_at": str(event.get("requested_updated_at") or ""),
            "corpus_revision": corpus_revision,
            "scope_id": scope_id,
        },
    ).fetchone()
    if updated is None:
        _defer_for_corpus_change(
            conn,
            event=event,
            worker_id=worker_id,
        )
    conn.commit()
    return len(peer_ids), str(updated[0]) == "completed"


def _fail_relation_event(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    worker_id: str,
    error: str,
    max_failures: int,
) -> str:
    current = conn.execute(
        """
        SELECT failures, pass_failures, next_requested_updated_at,
               requested_updated_at
        FROM relation_rebuild_queue
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).fetchone()
    if current is None:
        return "superseded"
    total_failures = int(current[0] or 0) + 1
    pass_failures = int(current[1] or 0) + 1
    next_requested = str(current[2] or "")
    active_requested = str(current[3] or "")
    terminal = pass_failures >= max(1, int(max_failures))
    promote_next = terminal and bool(next_requested) and next_requested != active_requested
    status = "pending" if promote_next else ("dead_letter" if terminal else "retry")
    now = _iso()
    changed = conn.execute(
        """
        UPDATE relation_rebuild_queue
        SET status=?, failures=?,
            pass_failures=CASE WHEN ?=1 THEN 0 ELSE ? END,
            pass_lease_expirations=CASE
                WHEN ?=1 THEN 0 ELSE pass_lease_expirations END,
            requested_updated_at=CASE
                WHEN ?=1 THEN next_requested_updated_at ELSE requested_updated_at END,
            next_requested_updated_at=CASE
                WHEN ?=1 THEN '' ELSE next_requested_updated_at END,
            cursor_memory_id=CASE WHEN ?=1 THEN '' ELSE cursor_memory_id END,
            pass_processed_pairs=CASE WHEN ?=1 THEN 0 ELSE pass_processed_pairs END,
            pass_number=pass_number+CASE WHEN ?=1 THEN 1 ELSE 0 END,
            lease_owner='', lease_token='', lease_expires_at=NULL,
            available_at=?, updated_at=?, last_error=?, completed_at=NULL
        WHERE id=? AND status='processing' AND lease_owner=?
          AND lease_token=? AND requested_updated_at=?
        """,
        (
            status,
            total_failures,
            int(promote_next),
            pass_failures,
            int(promote_next),
            int(promote_next),
            int(promote_next),
            int(promote_next),
            int(promote_next),
            int(promote_next),
            now,
            now,
            sanitize_report_text(str(error or "relation rebuild failed"))[:1000],
            int(event["id"]),
            worker_id,
            str(event.get("lease_token") or ""),
            str(event.get("requested_updated_at") or ""),
        ),
    ).rowcount
    conn.commit()
    if changed != 1:
        return "superseded"
    return "retry" if promote_next else status


def drain_relation_rebuild_queue(
    conn: sqlite3.Connection,
    *,
    max_events: int = 1,
    pair_limit: int = 250,
    lease_seconds: int = 120,
    max_lease_expirations: int = 5,
    max_candidates: int = 24,
    max_failures: int = 5,
    worker_id: str | None = None,
) -> dict[str, int]:
    """Process bounded relation chunks and classify lease loss as supersession."""

    worker = str(worker_id or f"relation-rebuild-{uuid.uuid4().hex}")
    totals = {
        "claimed": 0,
        "chunks_completed": 0,
        "events_completed": 0,
        "superseded": 0,
        "failed": 0,
        "dead_lettered": 0,
    }
    for _ in range(max(0, min(int(max_events), 100))):
        claimed = claim_relation_rebuild_events(
            conn,
            worker_id=worker,
            limit=1,
            lease_seconds=lease_seconds,
            max_lease_expirations=max_lease_expirations,
            commit=True,
        )
        if not claimed:
            break
        event = claimed[0]
        totals["claimed"] += 1
        try:
            _processed, completed = _process_relation_chunk(
                conn,
                event=event,
                worker_id=worker,
                pair_limit=pair_limit,
                max_candidates=max_candidates,
            )
            totals["chunks_completed"] += 1
            if completed:
                totals["events_completed"] += 1
        except _RelationRebuildSuperseded:
            conn.rollback()
            totals["superseded"] += 1
        except Exception as exc:
            conn.rollback()
            status = _fail_relation_event(
                conn,
                event=event,
                worker_id=worker,
                error=str(exc),
                max_failures=max_failures,
            )
            if status == "superseded":
                totals["superseded"] += 1
                continue
            totals["failed"] += 1
            if status == "dead_letter":
                totals["dead_lettered"] += 1
    return totals


def seed_scope_relation_rebuilds(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    reason: str = "operator seeded legacy relation rebuild",
    commit: bool = False,
) -> dict[str, int]:
    """Seed one idempotent focus event per visible memory for legacy graph debt."""

    scopes = sorted({str(item) for item in (scope_ids or []) if str(item)})
    where = [lifecycle_visible_sql("m")]
    params: list[Any] = []
    if scopes:
        where.append(f"m.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    rows = conn.execute(
        f"""
        SELECT m.id, m.scope_id, m.updated_at
        FROM memories m
        WHERE {' AND '.join(where)}
        ORDER BY m.scope_id, m.id
        """,
        params,
    ).fetchall()
    for row in rows:
        enqueue_relation_rebuild(
            conn,
            scope_id=str(row[1]),
            focus_memory_id=str(row[0]),
            requested_updated_at=str(row[2] or ""),
            reason=reason,
            commit=False,
            force=True,
        )
    if commit:
        conn.commit()
    return {"eligible": len(rows), "queued": len(rows)}


def relation_rebuild_debt_exists(conn: sqlite3.Connection) -> bool:
    """Cheap readiness probe used by the existing background writer."""

    if not _table_exists(conn):
        return False
    return (
        conn.execute(
            """
            SELECT 1 FROM relation_rebuild_queue
            WHERE status IN ('pending','retry','processing')
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def relation_rebuild_queue_report(
    conn: sqlite3.Connection,
    *,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Return sanitized debt counts without mutating or recovering leases."""

    if not _table_exists(conn):
        return {
            "status": "schema_missing",
            "unresolved": 0,
            "pending": 0,
            "retry": 0,
            "processing": 0,
            "completed": 0,
            "dead_letter": 0,
            "lifetime_processed_pairs": 0,
            "lifetime_attempts": 0,
            "lifetime_lease_expirations": 0,
            "lifetime_failures": 0,
            "supersession_count": 0,
            "active_passes_with_progress": 0,
            "oldest_unresolved_age_seconds": 0.0,
            "samples": [],
        }
    counts = {status: 0 for status in _QUEUE_STATUSES}
    for row in conn.execute(
        "SELECT status, COUNT(*) FROM relation_rebuild_queue GROUP BY status"
    ):
        status = str(row[0])
        if status in counts:
            counts[status] = int(row[1])
    unresolved = (
        counts["pending"]
        + counts["retry"]
        + counts["processing"]
        + counts["dead_letter"]
    )
    oldest_row = conn.execute(
        """
        SELECT MIN(created_at)
        FROM relation_rebuild_queue
        WHERE status IN ('pending','retry','processing','dead_letter')
        """
    ).fetchone()
    age = 0.0
    if oldest_row and oldest_row[0]:
        try:
            created = datetime.fromisoformat(str(oldest_row[0]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0.0, (_now() - created.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            age = 0.0
    progress_row = conn.execute(
        """
        SELECT COALESCE(SUM(processed_pairs), 0), COALESCE(SUM(attempts), 0),
               COALESCE(SUM(lease_expirations), 0), COALESCE(SUM(failures), 0),
               COALESCE(SUM(supersession_count), 0),
               COALESCE(SUM(CASE WHEN pass_processed_pairs>0 THEN 1 ELSE 0 END), 0)
        FROM relation_rebuild_queue
        """
    ).fetchone()
    samples = [
        {
            "id": int(row[0]),
            "scope_id": str(row[1]),
            "focus_memory_id": str(row[2]),
            "status": str(row[3]),
            "processed_pairs": int(row[4] or 0),
            "pass_processed_pairs": int(row[5] or 0),
            "pass_number": int(row[6] or 0),
            "supersession_count": int(row[7] or 0),
            "next_revision_pending": bool(str(row[8] or "")),
            "attempts": int(row[9] or 0),
            "lease_expirations": int(row[10] or 0),
            "pass_lease_expirations": int(row[11] or 0),
            "failures": int(row[12] or 0),
            "pass_failures": int(row[13] or 0),
            "last_progress_at": str(row[14] or ""),
            "last_error": sanitize_report_text(str(row[15] or ""))[:240],
            "available_at": str(row[16] or ""),
            "corpus_revision": int(row[17] or 0),
        }
        for row in conn.execute(
            """
            SELECT id, scope_id, focus_memory_id, status, processed_pairs,
                   pass_processed_pairs, pass_number, supersession_count,
                   next_requested_updated_at, attempts, lease_expirations,
                   pass_lease_expirations, failures, pass_failures,
                   last_progress_at, last_error, available_at, corpus_revision
            FROM relation_rebuild_queue
            WHERE status IN ('pending','retry','processing','dead_letter')
            ORDER BY created_at, id
            LIMIT ?
            """,
            (max(0, int(sample_limit)),),
        ).fetchall()
    ]
    return {
        "status": "debt" if unresolved else "ready",
        "unresolved": unresolved,
        **counts,
        "lifetime_processed_pairs": int(progress_row[0] or 0),
        "lifetime_attempts": int(progress_row[1] or 0),
        "lifetime_lease_expirations": int(progress_row[2] or 0),
        "lifetime_failures": int(progress_row[3] or 0),
        "supersession_count": int(progress_row[4] or 0),
        "active_passes_with_progress": int(progress_row[5] or 0),
        "oldest_unresolved_age_seconds": round(age, 3),
        "samples": samples,
    }


__all__ = [
    "RELATION_REBUILD_MIGRATION_DESCRIPTION",
    "RELATION_REBUILD_MIGRATION_ID",
    "RELATION_REBUILD_MIGRATION_PLUGIN_VERSION",
    "RELATION_REBUILD_SCHEMA_VERSION",
    "RELATION_REBUILD_LEASE_MIGRATION_DESCRIPTION",
    "RELATION_REBUILD_LEASE_MIGRATION_ID",
    "RELATION_REBUILD_LEASE_MIGRATION_PLUGIN_VERSION",
    "RELATION_REBUILD_LEASE_SCHEMA_VERSION",
    "RELATION_REBUILD_PROGRESS_MIGRATION_DESCRIPTION",
    "RELATION_REBUILD_PROGRESS_MIGRATION_ID",
    "RELATION_REBUILD_PROGRESS_MIGRATION_PLUGIN_VERSION",
    "RELATION_REBUILD_PROGRESS_SCHEMA_VERSION",
    "RELATION_REBUILD_EXPIRY_MIGRATION_DESCRIPTION",
    "RELATION_REBUILD_EXPIRY_MIGRATION_ID",
    "RELATION_REBUILD_EXPIRY_MIGRATION_PLUGIN_VERSION",
    "RELATION_REBUILD_EXPIRY_SCHEMA_VERSION",
    "claim_relation_rebuild_events",
    "drain_relation_rebuild_queue",
    "enqueue_relation_rebuild",
    "ensure_relation_rebuild_schema",
    "relation_rebuild_debt_exists",
    "relation_rebuild_queue_report",
    "relation_rebuild_schema_status",
    "resolve_relation_rebuild",
    "seed_scope_relation_rebuilds",
]
