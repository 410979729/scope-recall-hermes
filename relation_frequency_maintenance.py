"""Bounded maintenance and reporting for the relation-frequency companion.

The foreground index module owns transactional per-memory deltas and bounded
reads.  This module owns resumable background lifecycles: direct-SQL dirty rows,
legacy backfill, threshold reclassification, and operator debt reporting.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
import uuid
from typing import Any, Callable

try:
    from .capture_filters import sanitize_report_text
except ImportError:  # pragma: no cover
    from capture_filters import sanitize_report_text

try:
    from .sqlite_recovery import is_sqlite_lock_contention
except ImportError:  # pragma: no cover
    from sqlite_recovery import is_sqlite_lock_contention

try:
    from .relation_frequency_index import (
        refresh_relation_scope_frequency_receipt,
        relation_frequency_index_schema_status,
        supersede_relation_frequency_failure,
        sync_relation_frequency_memory,
    )
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from relation_frequency_index import (  # type: ignore[no-redef]
        refresh_relation_scope_frequency_receipt,
        relation_frequency_index_schema_status,
        supersede_relation_frequency_failure,
        sync_relation_frequency_memory,
    )

try:
    from .relation_containment import (
        defer_relation_focus_work,
        drain_relation_containment_scope,
        record_relation_focus_failure,
    )
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from relation_containment import (  # type: ignore[no-redef]
        defer_relation_focus_work,
        drain_relation_containment_scope,
        record_relation_focus_failure,
    )

try:
    from .relation_policy_generation import (
        drain_relation_policy_generation,
        restore_program0_relation_containment,
    )
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from relation_policy_generation import (  # type: ignore[no-redef]
        drain_relation_policy_generation,
        restore_program0_relation_containment,
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


def _record_frequency_failure(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    old_scope_id: str,
    new_scope_id: str,
    work_generation: int,
    requested_at: str,
    error: BaseException,
) -> str:
    """Advance one bounded retry counter while preserving the dirty row."""

    last_error = sanitize_report_text(f"{type(error).__name__}: {error}")[:500]
    changed = conn.execute(
        """
        INSERT INTO relation_frequency_failures(
            memory_id, work_generation, work_revision, old_scope_id,
            new_scope_id, attempts, status, last_error, last_failed_at
        )
        SELECT c.memory_id, c.work_generation, c.requested_at,
               c.old_scope_id, c.new_scope_id, 1, 'retry', ?, ?
        FROM relation_frequency_changes c
        WHERE c.memory_id=? AND c.work_generation=? AND c.requested_at=?
          AND c.old_scope_id=? AND c.new_scope_id=?
        ON CONFLICT(memory_id) DO UPDATE SET
            work_generation=excluded.work_generation,
            work_revision=excluded.work_revision,
            old_scope_id=excluded.old_scope_id,
            new_scope_id=excluded.new_scope_id,
            attempts=relation_frequency_failures.attempts+1,
            status=CASE
                WHEN relation_frequency_failures.attempts+1>=? THEN 'dead_letter'
                ELSE 'retry'
            END,
            last_error=excluded.last_error,
            last_failed_at=excluded.last_failed_at
        WHERE relation_frequency_failures.work_generation=excluded.work_generation
          AND EXISTS(
              SELECT 1 FROM relation_frequency_changes current
              WHERE current.memory_id=excluded.memory_id
                AND current.work_generation=excluded.work_generation
                AND current.requested_at=excluded.work_revision
                AND current.old_scope_id=excluded.old_scope_id
                AND current.new_scope_id=excluded.new_scope_id
          )
        """,
        (
            last_error,
            _now_iso(),
            memory_id,
            int(work_generation),
            str(requested_at),
            str(old_scope_id or ""),
            str(new_scope_id or ""),
            _MAX_CHANGE_ATTEMPTS,
        ),
    ).rowcount
    if changed != 1:
        return "superseded"
    recorded = conn.execute(
        """
        SELECT attempts, status FROM relation_frequency_failures
        WHERE memory_id=? AND work_generation=? AND work_revision=?
        """,
        (memory_id, int(work_generation), str(requested_at)),
    ).fetchone()
    if recorded is None:
        return "superseded"
    attempts = int(recorded[0] or 0)
    status = str(recorded[1])
    logger.warning(
        "Scope Recall relation-frequency change failed for %s (%s/%s; %s)",
        memory_id,
        attempts,
        _MAX_CHANGE_ATTEMPTS,
        status,
    )
    return status


def _requeue_orphaned_frequency_failures(
    conn: sqlite3.Connection,
    limit: int,
    *,
    deadline_monotonic: float,
    clock: Callable[[], float],
) -> int:
    """Reconstruct lost dirty work before superseding its durable failure.

    A failure without a change row can never reach the normal retry consumer.
    Rebuild from current truth under a new generation instead of deleting the
    failure on age alone. A failed rebuild retains its change row and therefore
    exhausts the ordinary retry budget rather than being resurrected forever.
    """

    rows = conn.execute(
        """
        SELECT f.memory_id, f.work_generation, f.old_scope_id, f.new_scope_id
        FROM relation_frequency_failures f
        WHERE NOT EXISTS (
            SELECT 1 FROM relation_frequency_changes c WHERE c.memory_id=f.memory_id
        )
        ORDER BY f.last_failed_at, f.memory_id LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    recovered = 0
    for row in rows:
        if clock() >= deadline_monotonic:
            break
        memory_id = str(row[0])
        requested_at = _now_iso()
        # BEGIN held by the caller serializes this with competing truth writers.
        conn.execute(
            """
            INSERT INTO relation_frequency_generations(memory_id, last_generation, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                last_generation=MAX(relation_frequency_generations.last_generation+1,
                                    excluded.last_generation),
                updated_at=excluded.updated_at
            """,
            (memory_id, int(row[1] or 0) + 1, requested_at),
        )
        conn.execute(
            """
            INSERT INTO relation_frequency_changes(
                memory_id, old_scope_id, new_scope_id, work_generation, requested_at
            )
            SELECT ?, ?, ?, last_generation, ?
            FROM relation_frequency_generations WHERE memory_id=?
            """,
            (memory_id, str(row[2] or ""), str(row[3] or ""), requested_at, memory_id),
        )
        supersede_relation_frequency_failure(conn, memory_id, requested_at=requested_at)
        recovered += 1
    return recovered


def _drain_change_rows(
    conn: sqlite3.Connection,
    limit: int,
    *,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Process dirty memories independently with bounded poison-row retries."""

    rows = conn.execute(
        """
        SELECT c.memory_id, c.work_generation, c.requested_at
        FROM relation_frequency_changes c
        LEFT JOIN relation_frequency_failures f
          ON f.memory_id=c.memory_id
         AND f.work_generation=c.work_generation
        WHERE f.status IS NULL OR f.status='retry'
        ORDER BY c.requested_at, c.memory_id
        LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    affected: set[str] = set()
    processed = 0
    for row in rows:
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            break
        memory_id = str(row[0])
        work_generation = int(row[1] or 0)
        requested_at = str(row[2] or "")
        supersede_relation_frequency_failure(
            conn,
            memory_id,
            work_generation=work_generation,
            requested_at=requested_at,
        )
        pending = conn.execute(
            """
            SELECT old_scope_id, new_scope_id, work_generation, requested_at
            FROM relation_frequency_changes
            WHERE memory_id=? AND work_generation=? AND requested_at=?
            """,
            (memory_id, work_generation, requested_at),
        ).fetchone()
        if pending is None:
            continue
        scopes = {str(value or "") for value in pending[:2] if str(value or "")}
        savepoint = f"relation_frequency_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            sync_result = sync_relation_frequency_memory(
                conn,
                memory_id,
                refresh_receipts=False,
            )
            conn.execute(
                """
                DELETE FROM relation_frequency_failures
                WHERE memory_id=? AND work_generation=? AND work_revision=?
                """,
                (memory_id, work_generation, requested_at),
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            if is_sqlite_lock_contention(exc):
                raise
            _record_frequency_failure(
                conn,
                memory_id=memory_id,
                old_scope_id=str(pending[0] or ""),
                new_scope_id=str(pending[1] or ""),
                work_generation=work_generation,
                requested_at=requested_at,
                error=exc,
            )
            continue
        affected.update(scopes)
        affected.update(
            str(sync_result.get(key) or "") for key in ("old_scope_id", "new_scope_id")
        )
        processed += 1
    for scope in sorted(scope for scope in affected if scope):
        refresh_relation_scope_frequency_receipt(conn, scope)
    return processed


def _drain_backfill_page(
    conn: sqlite3.Connection,
    limit: int,
    *,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, bool]:
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
    if deadline_monotonic is not None and clock() >= deadline_monotonic:
        return 0, True
    processed_ids: list[str] = []
    for memory_id in selected:
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            break
        requested_at = _now_iso()
        existing_change = conn.execute(
            "SELECT 1 FROM relation_frequency_changes WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if existing_change is None:
            conn.execute(
                """
                INSERT INTO relation_frequency_generations(
                    memory_id, last_generation, updated_at
                ) VALUES(?, 1, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    last_generation=relation_frequency_generations.last_generation+1,
                    updated_at=excluded.updated_at
                """,
                (memory_id, requested_at),
            )
            conn.execute(
                """
                INSERT INTO relation_frequency_changes(
                    memory_id, old_scope_id, new_scope_id,
                    work_generation, requested_at
                )
                SELECT ?, '', ?, last_generation, ?
                FROM relation_frequency_generations WHERE memory_id=?
                """,
                (memory_id, scope_id, requested_at, memory_id),
            )
        else:
            conn.execute(
                """
                UPDATE relation_frequency_changes SET new_scope_id=?
                WHERE memory_id=?
                """,
                (scope_id, memory_id),
            )
        current_change = conn.execute(
            "SELECT work_generation, requested_at "
            "FROM relation_frequency_changes WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        if current_change is None:
            continue
        work_generation = int(current_change[0] or 0)
        requested_at = str(current_change[1] or "")
        supersede_relation_frequency_failure(
            conn,
            memory_id,
            work_generation=work_generation,
            requested_at=requested_at,
        )
        failure = conn.execute(
            """
            SELECT status FROM relation_frequency_failures
            WHERE memory_id=? AND work_generation=? AND work_revision=?
            """,
            (memory_id, work_generation, requested_at),
        ).fetchone()
        if failure is not None and str(failure[0]) in {"retry", "dead_letter"}:
            # The unified change worker owns retry progression.  In particular,
            # backfill must never erase or revive a terminal dead-letter item.
            processed_ids.append(memory_id)
            continue
        savepoint = f"relation_frequency_backfill_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            sync_relation_frequency_memory(
                conn,
                memory_id,
                refresh_receipts=False,
            )
            conn.execute(
                """
                DELETE FROM relation_frequency_failures
                WHERE memory_id=? AND work_generation=? AND work_revision=?
                """,
                (memory_id, work_generation, requested_at),
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            if is_sqlite_lock_contention(exc):
                raise
            _record_frequency_failure(
                conn,
                memory_id=memory_id,
                old_scope_id="",
                new_scope_id=scope_id,
                work_generation=work_generation,
                requested_at=requested_at,
                error=exc,
            )
        processed_ids.append(memory_id)
    if not processed_ids and selected:
        return 0, True
    has_more = len(rows) > len(processed_ids)
    now = _now_iso()
    if has_more:
        conn.execute(
            """
            UPDATE relation_frequency_backfill
            SET cursor_memory_id=?,
                processed_memories=processed_memories+?, updated_at=?
            WHERE scope_id=? AND status='pending'
            """,
            (processed_ids[-1], len(processed_ids), now, scope_id),
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
            (
                processed_ids[-1] if processed_ids else cursor,
                len(processed_ids),
                now,
                now,
                scope_id,
            ),
        )
        refresh_relation_scope_frequency_receipt(conn, scope_id)
    return len(processed_ids), True


def _drain_focus_rows(
    conn: sqlite3.Connection,
    limit: int,
    *,
    relation_candidate_cap: int,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Process exact durable focus items without scanning or expanding a scope."""

    try:
        from .relation_extraction import sync_extracted_relations_for_memory
    except ImportError:  # pragma: no cover - direct source-script fallback
        from relation_extraction import sync_extracted_relations_for_memory

    rows = conn.execute(
        """
        SELECT memory_id, work_generation
        FROM relation_focus_work
        WHERE status IN ('pending','retry')
          AND (next_attempt_at='' OR next_attempt_at<=?)
        ORDER BY updated_at, memory_id
        LIMIT ?
        """,
        (_now_iso(), max(0, min(int(limit), 5000))),
    ).fetchall()
    processed = 0
    for row in rows:
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            break
        memory_id = str(row[0])
        generation = int(row[1] or 0)
        savepoint = f"relation_focus_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = sync_extracted_relations_for_memory(
                conn,
                memory_id=memory_id,
                batch_id=f"focus-work-{generation}",
                max_pairs=max(1, min(int(relation_candidate_cap), 5000)),
                local_peer_limit=max(1, min(int(relation_candidate_cap), 5000)),
                commit=False,
            )
            if str(result.get("status") or "") == "synced":
                processed += 1
            elif bool(result.get("blocked")):
                record_relation_focus_failure(
                    conn,
                    memory_id=memory_id,
                    work_generation=generation,
                    error_class="RelationFocusBlocked",
                    reason_code=str(
                        result.get("deferred_reason")
                        or result.get("immediate_status")
                        or "focus_relation_sync_blocked"
                    ),
                    permanent=True,
                )
            else:
                defer_relation_focus_work(
                    conn,
                    memory_id=memory_id,
                    work_generation=generation,
                    delay_seconds=5.0,
                    reason_code=str(
                        result.get("deferred_reason")
                        or result.get("immediate_status")
                        or "focus_relation_dependency_pending"
                    ),
                )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            if is_sqlite_lock_contention(exc):
                raise
            record_relation_focus_failure(
                conn,
                memory_id=memory_id,
                work_generation=generation,
                error_class=type(exc).__name__,
                reason_code="focus_relation_sync_failed",
            )
    return processed


def drain_relation_frequency_work(
    conn: sqlite3.Connection,
    *,
    change_limit: int = 250,
    focus_limit: int = 250,
    backfill_limit: int = 250,
    reclassification_limit: int = 250,
    relation_candidate_cap: int = 250,
    relation_max_attempts: int = 5,
    relation_policy_generation_enabled: bool = False,
    wall_clock_seconds: float = 0.5,
    backoff_base_seconds: float = 5.0,
    backoff_max_seconds: float = 300.0,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    commit: bool = True,
) -> dict[str, Any]:
    """Process bounded index debt and one finite containment target."""

    del reclassification_limit  # legacy full-scope cursor is intentionally disabled
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    deadline = (
        float(deadline_monotonic)
        if deadline_monotonic is not None
        else clock() + max(0.01, float(wall_clock_seconds))
    )
    try:
        recovered = _requeue_orphaned_frequency_failures(
            conn,
            max(0, min(int(change_limit), 5000)),
            deadline_monotonic=deadline,
            clock=clock,
        )
        changed = _drain_change_rows(
            conn,
            max(0, min(int(change_limit), 5000)),
            deadline_monotonic=deadline,
            clock=clock,
        )
        backfilled, backfill_active = _drain_backfill_page(
            conn,
            max(1, min(int(backfill_limit), 5000)),
            deadline_monotonic=deadline,
            clock=clock,
        )
        if clock() >= deadline:
            containment = {
                "status": "deferred_budget",
                "reason_code": "shared_maintenance_budget_exhausted",
                "attempted": 0,
                "completed": 0,
                "failed": 0,
            }
        else:
            if relation_policy_generation_enabled:
                containment = drain_relation_policy_generation(
                    conn,
                    item_limit=max(1, min(int(change_limit), 1000)),
                    candidate_cap=relation_candidate_cap,
                    max_attempts=relation_max_attempts,
                    wall_clock_seconds=wall_clock_seconds,
                    backoff_base_seconds=backoff_base_seconds,
                    backoff_max_seconds=backoff_max_seconds,
                    deadline_monotonic=deadline,
                    clock=clock,
                    commit=False,
                )
            else:
                restore_program0_relation_containment(
                    conn,
                    candidate_cap=relation_candidate_cap,
                )
                containment = drain_relation_containment_scope(
                    conn,
                    candidate_cap=relation_candidate_cap,
                    max_attempts=relation_max_attempts,
                    wall_clock_seconds=wall_clock_seconds,
                    backoff_base_seconds=backoff_base_seconds,
                    backoff_max_seconds=backoff_max_seconds,
                    deadline_monotonic=deadline,
                    clock=clock,
                    commit=False,
                )
        if clock() >= deadline:
            focused = 0
        else:
            focused = _drain_focus_rows(
                conn,
                max(0, min(int(focus_limit), 5000)),
                relation_candidate_cap=relation_candidate_cap,
                deadline_monotonic=deadline,
                clock=clock,
            )
    except Exception:
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    failure_counts = relation_frequency_index_schema_status(conn).get("failures", {})
    retry_failures = int(failure_counts.get("retry", 0) or 0)
    dead_letter_failures = int(failure_counts.get("dead_letter", 0) or 0)
    focus_retry = 0
    focus_dead_letter = 0
    if _table_exists(conn, "relation_focus_work"):
        focus_retry = int(
            conn.execute(
                "SELECT COUNT(*) FROM relation_focus_work WHERE status='retry'"
            ).fetchone()[0]
        )
        focus_dead_letter = int(
            conn.execute(
                "SELECT COUNT(*) FROM relation_focus_work WHERE status='dead_letter'"
            ).fetchone()[0]
        )
    return {
        "requeued_orphan_failures": recovered,
        "changed_memories": changed,
        "focused_memories": focused,
        "backfilled_memories": backfilled,
        "backfill_active": backfill_active,
        "reclassified_memories": 0,
        "reclassification_active": False,
        "containment": containment,
        "retry_failures": retry_failures,
        "dead_letter_failures": dead_letter_failures,
        "focus_retry_failures": focus_retry,
        "focus_dead_letter_failures": focus_dead_letter,
        "failed": (
            retry_failures
            + dead_letter_failures
            + focus_retry
            + focus_dead_letter
            + int(containment.get("failed", 0) or 0)
        ),
    }


def relation_frequency_debt_exists(conn: sqlite3.Connection) -> bool:
    """Return whether any bounded index/reclassification work remains."""

    if not _table_exists(conn, "relation_frequency_changes"):
        return False
    checks = [
        "SELECT 1 FROM relation_frequency_failures f "
        "WHERE NOT EXISTS (SELECT 1 FROM relation_frequency_changes c "
        "WHERE c.memory_id=f.memory_id) LIMIT 1",
        "SELECT 1 FROM relation_frequency_changes c "
        "LEFT JOIN relation_frequency_failures f "
        "ON f.memory_id=c.memory_id AND f.work_generation=c.work_generation "
        "WHERE f.status IS NULL OR f.status='retry' LIMIT 1",
        "SELECT 1 FROM relation_frequency_backfill WHERE status='pending' LIMIT 1",
    ]
    if _table_exists(conn, "relation_focus_work"):
        checks.append(
            "SELECT 1 FROM relation_focus_work "
            "WHERE status IN ('pending','retry') LIMIT 1"
        )
    return any(conn.execute(sql).fetchone() is not None for sql in checks)


def relation_frequency_index_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded readiness/debt counters for doctor and release evidence."""

    status = relation_frequency_index_schema_status(conn)
    if not status["current"]:
        return {"status": "schema_missing", **status}
    if not _table_exists(conn, "relation_focus_work"):
        return {"status": "schema_missing", "missing_focus_work": True, **status}
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
    focus_pending = int(
        conn.execute(
            "SELECT COUNT(*) FROM relation_focus_work WHERE status='pending'"
        ).fetchone()[0]
    )
    focus_retry = int(
        conn.execute(
            "SELECT COUNT(*) FROM relation_focus_work WHERE status='retry'"
        ).fetchone()[0]
    )
    focus_dead_letter = int(
        conn.execute(
            "SELECT COUNT(*) FROM relation_focus_work WHERE status='dead_letter'"
        ).fetchone()[0]
    )
    if dead_letter_failures or focus_dead_letter:
        readiness = "error"
    elif (
        changes
        or backfill_pending
        or reclassification_pending
        or retry_failures
        or focus_pending
        or focus_retry
    ):
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
        "focus_pending": focus_pending,
        "focus_retry_failures": focus_retry,
        "focus_dead_letter_failures": focus_dead_letter,
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
