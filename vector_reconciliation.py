"""Recoverable, bounded vector reconciliation planning.

Ordinary startup must never materialize the complete truth store or vector
companion.  This module persists a compound ``(updated_at, id)`` watermark and
turns at most one bounded truth page into durable vector-outbox intents.  It does
not embed text or mutate a physical vector backend; the existing causal outbox
executor remains the sole physical writer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from typing import Any, Callable

from .capture_filters import sanitize_report_text
from .vector_generation import enqueue_vector_event, ensure_vector_generation_schema

VECTOR_RECONCILIATION_SCHEMA_VERSION = 10807
VECTOR_RECONCILIATION_MIGRATION_ID = "0009_vector_reconciliation_watermark_v1_8_0"
VECTOR_RECONCILIATION_MIGRATION_PLUGIN_VERSION = "1.8.0"
VECTOR_RECONCILIATION_MIGRATION_DESCRIPTION = (
    "Outbox-first bounded vector reconciliation with a recoverable truth watermark"
)

_REQUIRED_COLUMNS = {
    "generation_id",
    "status",
    "cursor_updated_at",
    "cursor_memory_id",
    "upper_updated_at",
    "upper_memory_id",
    "cycle_number",
    "processed_rows",
    "enqueued_events",
    "next_cycle_at",
    "started_at",
    "last_progress_at",
    "completed_at",
    "last_error",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def ensure_vector_reconciliation_schema(conn: sqlite3.Connection) -> None:
    """Create the additive watermark companion without committing the caller."""

    ensure_vector_generation_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_reconciliation_state (
            generation_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'idle'
                CHECK(status IN ('idle','running','blocked','failed')),
            cursor_updated_at TEXT NOT NULL DEFAULT '',
            cursor_memory_id TEXT NOT NULL DEFAULT '',
            upper_updated_at TEXT NOT NULL DEFAULT '',
            upper_memory_id TEXT NOT NULL DEFAULT '',
            cycle_number INTEGER NOT NULL DEFAULT 0,
            processed_rows INTEGER NOT NULL DEFAULT 0,
            enqueued_events INTEGER NOT NULL DEFAULT 0,
            next_cycle_at TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            last_progress_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vector_reconciliation_due
        ON vector_reconciliation_state(status, next_cycle_at, generation_id)
        """
    )
    # The truth-page order and upper-bound probes must use an index rather than
    # sorting the complete memories table on each maintenance tick.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_vector_reconcile
        ON memories(updated_at, id)
        """
    )


def vector_reconciliation_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect the additive watermark schema without mutating it."""

    table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='vector_reconciliation_state'
        """
    ).fetchone()
    if table is None:
        return {
            "current": False,
            "schema_version": VECTOR_RECONCILIATION_SCHEMA_VERSION,
            "table_present": False,
            "missing_columns": sorted(_REQUIRED_COLUMNS),
        }
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(vector_reconciliation_state)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    truth_index = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='index' AND name='idx_memories_vector_reconcile'
        """
    ).fetchone()
    return {
        "current": not missing and truth_index is not None,
        "schema_version": VECTOR_RECONCILIATION_SCHEMA_VERSION,
        "table_present": True,
        "missing_columns": missing,
        "truth_index_present": truth_index is not None,
    }


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state(conn: sqlite3.Connection, generation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM vector_reconciliation_state WHERE generation_id=?",
        (generation_id,),
    ).fetchone()


def _start_cycle(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    now: str,
    previous_cycle: int,
) -> sqlite3.Row:
    upper = conn.execute(
        """
        SELECT updated_at, id
        FROM memories
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    upper_updated_at = str(upper[0] or "") if upper is not None else ""
    upper_memory_id = str(upper[1] or "") if upper is not None else ""
    conn.execute(
        """
        INSERT INTO vector_reconciliation_state(
            generation_id, status, cursor_updated_at, cursor_memory_id,
            upper_updated_at, upper_memory_id, cycle_number,
            processed_rows, enqueued_events, next_cycle_at, started_at,
            last_progress_at, completed_at, last_error
        ) VALUES(?, 'running', '', '', ?, ?, ?, 0, 0, '', ?, '', '', '')
        ON CONFLICT(generation_id) DO UPDATE SET
            status='running', cursor_updated_at='', cursor_memory_id='',
            upper_updated_at=excluded.upper_updated_at,
            upper_memory_id=excluded.upper_memory_id,
            cycle_number=excluded.cycle_number,
            processed_rows=0, enqueued_events=0, next_cycle_at='',
            started_at=excluded.started_at, last_progress_at='',
            completed_at='', last_error=''
        """,
        (
            generation_id,
            upper_updated_at,
            upper_memory_id,
            max(1, int(previous_cycle) + 1),
            now,
        ),
    )
    row = _state(conn, generation_id)
    assert row is not None
    return row


def _page_rows(
    conn: sqlite3.Connection,
    *,
    cursor_updated_at: str,
    cursor_memory_id: str,
    upper_updated_at: str,
    upper_memory_id: str,
    limit: int,
) -> list[sqlite3.Row]:
    if not upper_updated_at and not upper_memory_id:
        return []
    return conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary,
               updated_at, metadata
        FROM memories
        WHERE (
            updated_at > ? OR (updated_at = ? AND id > ?)
        )
          AND (
            updated_at < ? OR (updated_at = ? AND id <= ?)
          )
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (
            cursor_updated_at,
            cursor_updated_at,
            cursor_memory_id,
            upper_updated_at,
            upper_updated_at,
            upper_memory_id,
            max(1, int(limit)),
        ),
    ).fetchall()


def _event_key(
    *,
    generation_id: str,
    cycle_number: int,
    memory_id: str,
    updated_at: str,
    operation: str,
) -> str:
    material = "\x1f".join(
        (
            generation_id,
            "startup-reconcile",
            str(cycle_number),
            memory_id,
            updated_at,
            operation,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def vector_outbox_backlog_status(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
) -> dict[str, int]:
    """Return bounded aggregate debt counts for one generation."""

    ensure_vector_generation_schema(conn)
    rows = conn.execute(
        """
        SELECT status, COUNT(*)
        FROM vector_outbox
        WHERE generation_id=?
          AND status IN ('pending','retry','processing','dead_letter')
        GROUP BY status
        """,
        (generation_id,),
    ).fetchall()
    result = {"pending": 0, "retry": 0, "processing": 0, "dead_letter": 0}
    for row in rows:
        result[str(row[0])] = int(row[1] or 0)
    result["replayable"] = (
        result["pending"] + result["retry"] + result["processing"]
    )
    return result


def prepare_vector_reconciliation_page(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    should_index_row: Callable[[str, Any], bool],
    page_size: int = 200,
    interval_seconds: int = 86_400,
) -> dict[str, Any]:
    """Atomically enqueue one bounded truth page and advance its watermark.

    The function owns only a short SQLite transaction.  No embedding or physical
    backend operation is permitted through this API.
    """

    resolved_generation = str(generation_id or "").strip()
    if not resolved_generation:
        raise ValueError("generation_id is required")
    bounded_page = max(1, min(int(page_size or 1), 2000))
    interval = max(60, int(interval_seconds or 86_400))
    ensure_vector_reconciliation_schema(conn)
    if conn.in_transaction:
        raise RuntimeError("vector reconciliation planning requires an idle SQLite connection")
    now_dt = _now()
    now = _iso(now_dt)
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _state(conn, resolved_generation)
        if state is None:
            state = _start_cycle(
                conn,
                generation_id=resolved_generation,
                now=now,
                previous_cycle=0,
            )
        elif str(state["status"] or "") == "idle":
            due = _parse_iso(str(state["next_cycle_at"] or ""))
            if due is not None and due > now_dt:
                conn.commit()
                return {
                    "status": "not_due",
                    "generation_id": resolved_generation,
                    "cycle_number": int(state["cycle_number"] or 0),
                    "planned": 0,
                    "has_more": False,
                    "cursor_updated_at": str(state["cursor_updated_at"] or ""),
                    "cursor_memory_id": str(state["cursor_memory_id"] or ""),
                }
            state = _start_cycle(
                conn,
                generation_id=resolved_generation,
                now=now,
                previous_cycle=int(state["cycle_number"] or 0),
            )
        elif str(state["status"] or "") in {"blocked", "failed"}:
            conn.commit()
            return {
                "status": str(state["status"]),
                "generation_id": resolved_generation,
                "cycle_number": int(state["cycle_number"] or 0),
                "planned": 0,
                "has_more": True,
                "error": sanitize_report_text(str(state["last_error"] or ""))[:500],
            }

        cycle_number = int(state["cycle_number"] or 1)
        rows = _page_rows(
            conn,
            cursor_updated_at=str(state["cursor_updated_at"] or ""),
            cursor_memory_id=str(state["cursor_memory_id"] or ""),
            upper_updated_at=str(state["upper_updated_at"] or ""),
            upper_memory_id=str(state["upper_memory_id"] or ""),
            limit=bounded_page + 1,
        )
        page = rows[:bounded_page]
        has_more = len(rows) > len(page)
        for row in page:
            memory_id = str(row["id"])
            updated_at = str(row["updated_at"] or "")
            operation = (
                "upsert"
                if should_index_row(str(row["target"] or ""), row["metadata"])
                else "delete"
            )
            enqueue_vector_event(
                conn,
                event_key=_event_key(
                    generation_id=resolved_generation,
                    cycle_number=cycle_number,
                    memory_id=memory_id,
                    updated_at=updated_at,
                    operation=operation,
                ),
                generation_id=resolved_generation,
                memory_id=memory_id,
                operation=operation,
                payload={
                    "updated_at": updated_at,
                    "reason": "bounded startup truth reconciliation",
                },
                timestamp=now,
            )

        last_updated_at = (
            str(page[-1]["updated_at"] or "")
            if page
            else str(state["cursor_updated_at"] or "")
        )
        last_memory_id = (
            str(page[-1]["id"] or "")
            if page
            else str(state["cursor_memory_id"] or "")
        )
        if has_more:
            conn.execute(
                """
                UPDATE vector_reconciliation_state
                SET cursor_updated_at=?, cursor_memory_id=?,
                    processed_rows=processed_rows+?,
                    enqueued_events=enqueued_events+?,
                    last_progress_at=?, last_error=''
                WHERE generation_id=? AND status='running'
                """,
                (
                    last_updated_at,
                    last_memory_id,
                    len(page),
                    len(page),
                    now,
                    resolved_generation,
                ),
            )
            status = "running"
        else:
            conn.execute(
                """
                UPDATE vector_reconciliation_state
                SET status='idle', cursor_updated_at=?, cursor_memory_id=?,
                    processed_rows=processed_rows+?,
                    enqueued_events=enqueued_events+?,
                    last_progress_at=?, completed_at=?, next_cycle_at=?,
                    last_error=''
                WHERE generation_id=? AND status='running'
                """,
                (
                    last_updated_at,
                    last_memory_id,
                    len(page),
                    len(page),
                    now,
                    now,
                    _iso(now_dt + timedelta(seconds=interval)),
                    resolved_generation,
                ),
            )
            status = "completed"
        conn.commit()
        return {
            "status": status,
            "generation_id": resolved_generation,
            "cycle_number": cycle_number,
            "planned": len(page),
            "has_more": has_more,
            "cursor_updated_at": last_updated_at,
            "cursor_memory_id": last_memory_id,
        }
    except Exception as exc:
        conn.rollback()
        # Planning failed before watermark/outbox commit.  Preserve the previous
        # state and expose the bounded diagnostic on a fresh short transaction.
        safe_error = sanitize_report_text(str(exc))[:1000]
        try:
            conn.execute(
                """
                UPDATE vector_reconciliation_state
                SET status='failed', last_error=?
                WHERE generation_id=?
                """,
                (safe_error, resolved_generation),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise


def mark_generation_snapshot_reconciled(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    interval_seconds: int = 86_400,
    timestamp: str = "",
) -> dict[str, Any]:
    """Seed an activated full-snapshot generation at the current truth upper bound.

    Callers must invoke this only after a shadow build validated every indexable
    row and activation CAS succeeded.  It prevents an exact new generation from
    immediately re-enqueuing the entire truth cohort on first startup.
    Transaction ownership remains with the caller.
    """

    resolved_generation = str(generation_id or "").strip()
    if not resolved_generation:
        raise ValueError("generation_id is required")
    ensure_vector_reconciliation_schema(conn)
    at = str(timestamp or _iso(datetime.now(timezone.utc)))
    at_dt = _parse_iso(at) or datetime.now(timezone.utc)
    upper = conn.execute(
        "SELECT updated_at, id FROM memories ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    upper_updated_at = str(upper[0] or "") if upper is not None else ""
    upper_memory_id = str(upper[1] or "") if upper is not None else ""
    processed_rows = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    conn.execute(
        """
        INSERT INTO vector_reconciliation_state(
            generation_id, status, cursor_updated_at, cursor_memory_id,
            upper_updated_at, upper_memory_id, cycle_number,
            processed_rows, enqueued_events, next_cycle_at, started_at,
            last_progress_at, completed_at, last_error
        ) VALUES(?, 'idle', ?, ?, ?, ?, 1, ?, 0, ?, ?, ?, ?, '')
        ON CONFLICT(generation_id) DO UPDATE SET
            status='idle', cursor_updated_at=excluded.cursor_updated_at,
            cursor_memory_id=excluded.cursor_memory_id,
            upper_updated_at=excluded.upper_updated_at,
            upper_memory_id=excluded.upper_memory_id,
            cycle_number=MAX(vector_reconciliation_state.cycle_number, 1),
            processed_rows=excluded.processed_rows, enqueued_events=0,
            next_cycle_at=excluded.next_cycle_at,
            started_at=excluded.started_at,
            last_progress_at=excluded.last_progress_at,
            completed_at=excluded.completed_at, last_error=''
        """,
        (
            resolved_generation,
            upper_updated_at,
            upper_memory_id,
            upper_updated_at,
            upper_memory_id,
            processed_rows,
            _iso(at_dt + timedelta(seconds=max(60, int(interval_seconds)))),
            at,
            at,
            at,
        ),
    )
    result = _state(conn, resolved_generation)
    assert result is not None
    return {key: result[key] for key in result.keys()}


def vector_reconciliation_state(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
) -> dict[str, Any] | None:
    """Return one report-safe watermark row."""

    ensure_vector_reconciliation_schema(conn)
    row = _state(conn, str(generation_id or ""))
    if row is None:
        return None
    result = {key: row[key] for key in row.keys()}
    result["last_error"] = sanitize_report_text(str(result.get("last_error") or ""))[:500]
    return result


__all__ = [
    "VECTOR_RECONCILIATION_MIGRATION_DESCRIPTION",
    "VECTOR_RECONCILIATION_MIGRATION_ID",
    "VECTOR_RECONCILIATION_MIGRATION_PLUGIN_VERSION",
    "VECTOR_RECONCILIATION_SCHEMA_VERSION",
    "ensure_vector_reconciliation_schema",
    "mark_generation_snapshot_reconciled",
    "prepare_vector_reconciliation_page",
    "vector_outbox_backlog_status",
    "vector_reconciliation_schema_status",
    "vector_reconciliation_state",
]
