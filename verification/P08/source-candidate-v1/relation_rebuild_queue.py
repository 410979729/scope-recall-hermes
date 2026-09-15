"""Retired relation-rebuild queue compatibility and debt reporting.

The historical table remains readable for migration, health, and backup-first
operator cleanup. Every former enqueue, claim, resolve, drain, and all-scope
seed surface now fails closed or returns a disabled no-op. Program 0 relation
maintenance runs only through the finite containment and focus-work paths.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from .capture_filters import sanitize_report_text
    from .relation_frequency_index import (
        ensure_relation_frequency_index_schema,
        relation_frequency_index_schema_status,
    )
    from .relation_scope_state import (
        ensure_relation_scope_state_schema,
        relation_scope_state_schema_status,
    )
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from capture_filters import sanitize_report_text
    from relation_frequency_index import (  # type: ignore[no-redef]
        ensure_relation_frequency_index_schema,
        relation_frequency_index_schema_status,
    )
    from relation_scope_state import (  # type: ignore[no-redef]
        ensure_relation_scope_state_schema,
        relation_scope_state_schema_status,
    )
    from sqlite_schema import execute_script_transaction_neutral

_QUEUE_STATUSES = ("pending", "retry", "processing", "completed", "dead_letter")
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    """Refuse every write to the retired legacy rebuild queue."""

    del conn, scope_id, focus_memory_id, requested_updated_at, reason, commit, force
    raise RuntimeError(
        "legacy relation rebuild enqueue is disabled; use finite containment "
        "or the backup-first operator cleanup"
    )


def resolve_relation_rebuild(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    focus_memory_id: str,
    requested_updated_at: str,
    commit: bool = False,
) -> int:
    """Refuse to relabel retired queue work as completed."""

    del conn, scope_id, focus_memory_id, requested_updated_at, commit
    return 0


def claim_relation_rebuild_events(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 120,
    max_lease_expirations: int = 5,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Return no work without touching the retired legacy queue."""

    del conn, worker_id, limit, lease_seconds, max_lease_expirations, commit
    return []


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
) -> dict[str, Any]:
    """Fail closed without claiming legacy full-scope rebuild work."""

    del (
        conn,
        max_events,
        pair_limit,
        lease_seconds,
        max_lease_expirations,
        max_candidates,
        max_failures,
        worker_id,
    )
    return {
        "claimed": 0,
        "chunks_completed": 0,
        "events_completed": 0,
        "superseded": 0,
        "failed": 0,
        "dead_lettered": 0,
        "disabled": True,
        "reason_code": "legacy_unbounded_drain_disabled",
    }


def seed_scope_relation_rebuilds(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    reason: str = "operator seeded legacy relation rebuild",
    commit: bool = False,
) -> dict[str, Any]:
    """Fail closed: legacy all-scope seed is retained only as a compat surface."""

    del conn, scope_ids, reason, commit
    return {
        "eligible": 0,
        "queued": 0,
        "disabled": True,
        "reason_code": "legacy_unbounded_seed_disabled",
    }


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
