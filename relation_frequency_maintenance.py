"""Bounded maintenance and reporting for the relation-frequency companion.

The foreground index module owns transactional per-memory deltas and bounded
reads.  This module owns resumable background lifecycles: direct-SQL dirty rows,
legacy backfill, threshold reclassification, and operator debt reporting.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from typing import Any

try:
    from .capture_filters import sanitize_report_text
except ImportError:  # pragma: no cover
    from capture_filters import sanitize_report_text

try:
    from .relation_frequency_index import (
        refresh_relation_scope_frequency_receipt,
        relation_frequency_index_schema_status,
        sync_relation_frequency_memory,
    )
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from relation_frequency_index import (  # type: ignore[no-redef]
        refresh_relation_scope_frequency_receipt,
        relation_frequency_index_schema_status,
        sync_relation_frequency_memory,
    )


logger = logging.getLogger(__name__)
_MAX_CHANGE_ATTEMPTS = 3


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(name),),
        ).fetchone()
        is not None
    )


def _drain_change_rows(conn: sqlite3.Connection, limit: int) -> int:
    """Process dirty memories independently with bounded poison-row retries."""

    rows = conn.execute(
        """
        SELECT memory_id
        FROM relation_frequency_changes
        ORDER BY requested_at, memory_id
        LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    affected: set[str] = set()
    processed = 0
    for row in rows:
        memory_id = str(row[0])
        pending = conn.execute(
            """
            SELECT old_scope_id, new_scope_id
            FROM relation_frequency_changes WHERE memory_id=?
            """,
            (memory_id,),
        ).fetchone()
        if pending is None:
            continue
        scopes = {str(value or "") for value in pending if str(value or "")}
        savepoint = f"relation_frequency_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            sync_relation_frequency_memory(
                conn,
                memory_id,
                refresh_receipts=False,
            )
            conn.execute(
                "DELETE FROM relation_frequency_failures WHERE memory_id=?",
                (memory_id,),
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            previous = conn.execute(
                "SELECT attempts FROM relation_frequency_failures WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            attempts = int(previous[0] if previous is not None else 0) + 1
            status = (
                "dead_letter"
                if attempts >= _MAX_CHANGE_ATTEMPTS
                else "retry"
            )
            last_error = sanitize_report_text(
                f"{type(exc).__name__}: {exc}"
            )[:500]
            conn.execute(
                """
                INSERT INTO relation_frequency_failures(
                    memory_id, old_scope_id, new_scope_id, attempts,
                    status, last_error, last_failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    old_scope_id=excluded.old_scope_id,
                    new_scope_id=excluded.new_scope_id,
                    attempts=excluded.attempts,
                    status=excluded.status,
                    last_error=excluded.last_error,
                    last_failed_at=excluded.last_failed_at
                """,
                (
                    memory_id,
                    str(pending[0] or ""),
                    str(pending[1] or ""),
                    attempts,
                    status,
                    last_error,
                    _now_iso(),
                ),
            )
            if status == "dead_letter":
                conn.execute(
                    "DELETE FROM relation_frequency_changes WHERE memory_id=?",
                    (memory_id,),
                )
            logger.warning(
                "Scope Recall relation-frequency change failed for %s "
                "(%s/%s; %s)",
                memory_id,
                attempts,
                _MAX_CHANGE_ATTEMPTS,
                status,
            )
            continue
        affected.update(scopes)
        processed += 1
    for scope in sorted(scope for scope in affected if scope):
        refresh_relation_scope_frequency_receipt(conn, scope)
    return processed


def _drain_backfill_page(conn: sqlite3.Connection, limit: int) -> tuple[int, bool]:
    state = conn.execute(
        """
        SELECT scope_id, cursor_memory_id
        FROM relation_frequency_backfill
        WHERE status='pending'
        ORDER BY updated_at, scope_id
        LIMIT 1
        """
    ).fetchone()
    if state is None:
        return 0, False
    scope_id = str(state[0])
    cursor = str(state[1] or "")
    bounded = max(1, int(limit))
    rows = conn.execute(
        """
        SELECT id FROM memories
        WHERE scope_id=? AND id>?
        ORDER BY id
        LIMIT ?
        """,
        (scope_id, cursor, bounded + 1),
    ).fetchall()
    selected = [str(row[0]) for row in rows[:bounded]]
    for memory_id in selected:
        sync_relation_frequency_memory(
            conn,
            memory_id,
            refresh_receipts=False,
        )
    has_more = len(rows) > len(selected)
    now = _now_iso()
    if has_more:
        conn.execute(
            """
            UPDATE relation_frequency_backfill
            SET cursor_memory_id=?,
                processed_memories=processed_memories+?, updated_at=?
            WHERE scope_id=? AND status='pending'
            """,
            (selected[-1], len(selected), now, scope_id),
        )
    else:
        conn.execute(
            """
            UPDATE relation_frequency_backfill
            SET status='complete', cursor_memory_id=?,
                processed_memories=processed_memories+?,
                updated_at=?, completed_at=?
            WHERE scope_id=? AND status='pending'
            """,
            (selected[-1] if selected else cursor, len(selected), now, now, scope_id),
        )
        refresh_relation_scope_frequency_receipt(conn, scope_id)
    return len(selected), True


def _drain_reclassification_page(conn: sqlite3.Connection, limit: int) -> tuple[int, bool]:
    state = conn.execute(
        """
        SELECT scope_id, active_revision, next_revision, cursor_memory_id
        FROM relation_scope_reclassification
        WHERE status='pending'
        ORDER BY updated_at, scope_id
        LIMIT 1
        """
    ).fetchone()
    if state is None:
        return 0, False
    scope_id = str(state[0])
    active_revision = int(state[1] or 0)
    next_revision = int(state[2] or 0)
    cursor = str(state[3] or "")
    bounded = max(1, int(limit))
    rows = conn.execute(
        """
        SELECT m.id, m.updated_at
        FROM memories m
        JOIN relation_indexed_memories rim
          ON rim.memory_id=m.id AND rim.visible=1
        WHERE m.scope_id=? AND m.id>?
        ORDER BY m.id
        LIMIT ?
        """,
        (scope_id, cursor, bounded + 1),
    ).fetchall()
    selected = rows[:bounded]
    # Lazy import preserves the dependency direction: the queue consumes
    # frequency snapshots; only this maintenance edge enqueues queue work.
    try:
        from .relation_rebuild_queue import enqueue_relation_rebuild
    except ImportError:  # pragma: no cover
        from relation_rebuild_queue import enqueue_relation_rebuild
    for row in selected:
        enqueue_relation_rebuild(
            conn,
            scope_id=scope_id,
            focus_memory_id=str(row[0]),
            requested_updated_at=str(row[1] or ""),
            reason=f"relation frequency classification changed at revision {active_revision}",
            commit=False,
            force=True,
        )
    has_more = len(rows) > len(selected)
    now = _now_iso()
    if has_more:
        conn.execute(
            """
            UPDATE relation_scope_reclassification
            SET cursor_memory_id=?,
                pass_processed_memories=pass_processed_memories+?,
                total_processed_memories=total_processed_memories+?,
                updated_at=?
            WHERE scope_id=? AND status='pending' AND active_revision=?
            """,
            (str(selected[-1][0]), len(selected), len(selected), now, scope_id, active_revision),
        )
    elif next_revision > active_revision:
        conn.execute(
            """
            UPDATE relation_scope_reclassification
            SET active_revision=next_revision, next_revision=0,
                cursor_memory_id='', pass_processed_memories=0,
                total_processed_memories=total_processed_memories+?,
                pass_number=pass_number+1, updated_at=?, completed_at=NULL
            WHERE scope_id=? AND status='pending' AND active_revision=?
            """,
            (len(selected), now, scope_id, active_revision),
        )
    else:
        conn.execute(
            """
            UPDATE relation_scope_reclassification
            SET status='complete', cursor_memory_id=?,
                pass_processed_memories=pass_processed_memories+?,
                total_processed_memories=total_processed_memories+?,
                updated_at=?, completed_at=?
            WHERE scope_id=? AND status='pending' AND active_revision=?
            """,
            (
                str(selected[-1][0]) if selected else cursor,
                len(selected),
                len(selected),
                now,
                now,
                scope_id,
                active_revision,
            ),
        )
    return len(selected), True


def drain_relation_frequency_work(
    conn: sqlite3.Connection,
    *,
    change_limit: int = 250,
    backfill_limit: int = 250,
    reclassification_limit: int = 250,
    commit: bool = True,
) -> dict[str, Any]:
    """Process bounded dirty ids, one backfill page, and one reclassification page."""

    changed = _drain_change_rows(conn, max(0, min(int(change_limit), 5000)))
    backfilled, backfill_active = _drain_backfill_page(
        conn, max(1, min(int(backfill_limit), 5000))
    )
    reclassified, reclassification_active = _drain_reclassification_page(
        conn, max(1, min(int(reclassification_limit), 5000))
    )
    if commit:
        conn.commit()
    failure_counts = relation_frequency_index_schema_status(conn).get("failures", {})
    retry_failures = int(failure_counts.get("retry", 0) or 0)
    dead_letter_failures = int(failure_counts.get("dead_letter", 0) or 0)
    return {
        "changed_memories": changed,
        "backfilled_memories": backfilled,
        "backfill_active": backfill_active,
        "reclassified_memories": reclassified,
        "reclassification_active": reclassification_active,
        "retry_failures": retry_failures,
        "dead_letter_failures": dead_letter_failures,
        "failed": retry_failures + dead_letter_failures,
    }


def relation_frequency_debt_exists(conn: sqlite3.Connection) -> bool:
    """Return whether any bounded index/reclassification work remains."""

    if not _table_exists(conn, "relation_frequency_changes"):
        return False
    checks = [
        "SELECT 1 FROM relation_frequency_changes LIMIT 1",
        "SELECT 1 FROM relation_frequency_backfill WHERE status='pending' LIMIT 1",
        "SELECT 1 FROM relation_scope_reclassification WHERE status='pending' LIMIT 1",
    ]
    if _table_exists(conn, "relation_frequency_failures"):
        checks.append("SELECT 1 FROM relation_frequency_failures LIMIT 1")
    return any(conn.execute(sql).fetchone() is not None for sql in checks)


def relation_frequency_index_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded readiness/debt counters for doctor and release evidence."""

    status = relation_frequency_index_schema_status(conn)
    if not status["current"]:
        return {"status": "schema_missing", **status}
    changes = int(conn.execute("SELECT COUNT(*) FROM relation_frequency_changes").fetchone()[0])
    backfill_pending = int(
        conn.execute(
            "SELECT COUNT(*) FROM relation_frequency_backfill WHERE status='pending'"
        ).fetchone()[0]
    )
    reclassification_pending = int(
        conn.execute(
            "SELECT COUNT(*) FROM relation_scope_reclassification WHERE status='pending'"
        ).fetchone()[0]
    )
    failure_counts = status.get("failures", {})
    retry_failures = int(failure_counts.get("retry", 0) or 0)
    dead_letter_failures = int(failure_counts.get("dead_letter", 0) or 0)
    if dead_letter_failures:
        readiness = "error"
    elif changes or backfill_pending or reclassification_pending or retry_failures:
        readiness = "debt"
    else:
        readiness = "ready"
    return {
        **status,
        "status": readiness,
        "dirty_memories": changes,
        "backfill_pending_scopes": backfill_pending,
        "reclassification_pending_scopes": reclassification_pending,
        "retry_failures": retry_failures,
        "dead_letter_failures": dead_letter_failures,
        "indexed_memories": int(
            conn.execute("SELECT COUNT(*) FROM relation_indexed_memories").fetchone()[0]
        ),
        "posting_rows": int(
            conn.execute("SELECT COUNT(*) FROM relation_entity_postings").fetchone()[0]
        ),
    }


__all__ = [
    "drain_relation_frequency_work",
    "relation_frequency_debt_exists",
    "relation_frequency_index_report",
]
