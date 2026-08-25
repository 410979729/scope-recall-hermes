"""Durable, bounded capture intents in the SQLite truth store.

Callers persist an intent before the background writer consumes it. The
in-memory wake channel is only a hint: unconsumed rows survive process restart
and are replayed through ``store_now``. Replay is idempotent because
``store_row`` already deduplicates by scope, target, and content hash. A crash
after the truth write but before this row is marked completed therefore retries
the same store once more and then completes the intent.

Rejected and deferred outcomes increment counters instead of silently dropping
work. Reports expose depth, oldest age, and those counters without filesystem
paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, TypeGuard

from .capture_filters import sanitize_report_text
from .sqlite_schema import execute_script_transaction_neutral

CAPTURE_INTENT_STATUSES = ("pending", "processing", "completed")
DEFAULT_CAPTURE_QUEUE_CAPACITY = 256
MIN_CAPTURE_QUEUE_CAPACITY = 8
MAX_CAPTURE_QUEUE_CAPACITY = 4096
PERSIST_LOCK_TIMEOUT_SECONDS = 0.15
_UNCONSUMED = ("pending", "processing")
_REQUIRED_COLUMNS = {
    "id",
    "coalesce_key",
    "content",
    "source",
    "target",
    "session_id",
    "metadata_json",
    "status",
    "created_at",
    "updated_at",
    "claimed_at",
    "completed_at",
}


def _is_sqlite_connection(value: object) -> TypeGuard[sqlite3.Connection]:
    """Narrow real or proxy SQLite handles after structural validation."""

    return all(
        hasattr(value, name)
        for name in ("execute", "commit", "rollback", "in_transaction")
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def queue_capacity(config: Mapping[str, Any] | None) -> int:
    """Return the explicit unconsumed-intent bound from config or the default."""

    raw = DEFAULT_CAPTURE_QUEUE_CAPACITY
    if isinstance(config, Mapping):
        candidate = config.get("capture_queue_capacity", raw)
        try:
            raw = int(candidate)
        except (TypeError, ValueError):
            raw = DEFAULT_CAPTURE_QUEUE_CAPACITY
    return max(MIN_CAPTURE_QUEUE_CAPACITY, min(MAX_CAPTURE_QUEUE_CAPACITY, raw))


def coalesce_key(*, content: str, source: str, target: str, session_id: str) -> str:
    """Hash one capture payload so duplicate pending work can coalesce."""

    material = "\0".join(
        (str(session_id or ""), str(source or ""), str(target or ""), str(content or ""))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def ensure_capture_intent_schema(conn: sqlite3.Connection) -> None:
    """Create the additive capture-intent tables without committing caller work."""

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS capture_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coalesce_key TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','processing','completed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_capture_intents_claim
            ON capture_intents(status, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_capture_intents_coalesce
            ON capture_intents(coalesce_key, status);
        CREATE TABLE IF NOT EXISTS capture_intent_stats (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            rejected_count INTEGER NOT NULL DEFAULT 0,
            deferred_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capture_intent_outcome_counts (
            status TEXT NOT NULL CHECK(status IN ('rejected','deferred')),
            reason TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (status, reason)
        );
        """,
    )
    if _table_exists(conn, "capture_intents"):
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(capture_intents)")
        }
        if not _REQUIRED_COLUMNS.issubset(columns):
            missing = ", ".join(sorted(_REQUIRED_COLUMNS - columns))
            raise sqlite3.OperationalError(
                f"capture_intents schema is missing columns: {missing}"
            )
    _ensure_stats_row(conn, timestamp=_now_iso())


def _ensure_stats_row(conn: sqlite3.Connection, *, timestamp: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO capture_intent_stats(id, rejected_count, deferred_count, updated_at)
        VALUES (1, 0, 0, ?)
        """,
        (timestamp,),
    )


def _increment_counter(
    conn: sqlite3.Connection, column: str, *, timestamp: str, count: int = 1
) -> None:
    if column not in {"rejected_count", "deferred_count"}:
        raise ValueError("unsupported capture intent counter")
    _ensure_stats_row(conn, timestamp=timestamp)
    conn.execute(
        f"""
        UPDATE capture_intent_stats
        SET {column} = {column} + ?, updated_at = ?
        WHERE id = 1
        """,
        (max(1, int(count)), timestamp),
    )


def record_outcome_on_connection(
    conn: sqlite3.Connection,
    status: str,
    reason: str,
    *,
    count: int = 1,
    timestamp: str = "",
) -> None:
    """Increment sanitized rejected/deferred metadata. Does not commit.

    ``reason`` must already be a short token. This function never writes
    capture payload text.
    """

    if status not in {"rejected", "deferred"}:
        raise ValueError("unsupported capture outcome status")
    ensure_capture_intent_schema(conn)
    at = timestamp or _now_iso()
    amount = max(1, int(count))
    token = str(reason or "unknown")[:64]
    column = "rejected_count" if status == "rejected" else "deferred_count"
    _increment_counter(conn, column, timestamp=at, count=amount)
    conn.execute(
        """
        INSERT INTO capture_intent_outcome_counts(status, reason, count, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(status, reason) DO UPDATE SET
            count = count + excluded.count,
            updated_at = excluded.updated_at
        """,
        (status, token, amount, at),
    )


def unconsumed_depth(conn: sqlite3.Connection) -> int:
    """Return pending+processing rows that still occupy queue capacity."""

    if not _table_exists(conn, "capture_intents"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM capture_intents
        WHERE status IN ('pending', 'processing')
        """
    ).fetchone()
    return int(row[0] if row is not None else 0)


def persist_capture_intent(
    conn: sqlite3.Connection,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: Mapping[str, Any] | None = None,
    capacity: int | None = None,
    timestamp: str = "",
) -> dict[str, Any]:
    """Insert or coalesce one intent. Does not commit the caller transaction."""

    ensure_capture_intent_schema(conn)
    at = timestamp or _now_iso()
    bound = queue_capacity({"capture_queue_capacity": capacity} if capacity is not None else None)
    if capacity is not None:
        bound = max(MIN_CAPTURE_QUEUE_CAPACITY, min(MAX_CAPTURE_QUEUE_CAPACITY, int(capacity)))
    key = coalesce_key(
        content=content, source=source, target=target, session_id=session_id
    )
    existing = conn.execute(
        """
        SELECT id FROM capture_intents
        WHERE coalesce_key = ? AND status IN ('pending', 'processing')
        ORDER BY id ASC
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    depth = unconsumed_depth(conn)
    if existing is not None:
        intent_id = int(existing[0])
        conn.execute(
            "UPDATE capture_intents SET updated_at = ? WHERE id = ?",
            (at, intent_id),
        )
        return {
            "status": "coalesced",
            "reason": "duplicate_unconsumed",
            "intent_id": intent_id,
            "depth": depth,
            "capacity": bound,
        }
    if depth >= bound:
        record_outcome_on_connection(conn, "rejected", "queue_full", timestamp=at)
        return {
            "status": "rejected",
            "reason": "queue_full",
            "intent_id": None,
            "depth": depth,
            "capacity": bound,
            "durable_accounted": True,
        }
    payload = metadata if isinstance(metadata, Mapping) else {}
    metadata_json = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        """
        INSERT INTO capture_intents(
            coalesce_key, content, source, target, session_id, metadata_json,
            status, created_at, updated_at, claimed_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, '', '')
        """,
        (key, content, source, target, session_id, metadata_json, at, at),
    )
    return {
        "status": "accepted",
        "reason": "persisted",
        "intent_id": int(cursor.lastrowid or 0),
        "depth": depth + 1,
        "capacity": bound,
    }


def record_deferred_capture_intent(
    conn: sqlite3.Connection, *, timestamp: str = ""
) -> None:
    """Count a persist that could not take the truth write lock."""

    record_outcome_on_connection(
        conn, "deferred", "unknown", timestamp=timestamp or _now_iso()
    )


def claim_next_capture_intent(
    conn: sqlite3.Connection, *, timestamp: str = ""
) -> dict[str, Any] | None:
    """Claim the oldest pending intent. Does not commit the caller transaction."""

    ensure_capture_intent_schema(conn)
    at = timestamp or _now_iso()
    row = conn.execute(
        """
        SELECT id, content, source, target, session_id, metadata_json
        FROM capture_intents
        WHERE status = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    intent_id = int(row[0])
    cursor = conn.execute(
        """
        UPDATE capture_intents
        SET status = 'processing', claimed_at = ?, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (at, at, intent_id),
    )
    if cursor.rowcount != 1:
        return None
    try:
        metadata = json.loads(str(row[5] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "id": intent_id,
        "content": str(row[1] or ""),
        "source": str(row[2] or ""),
        "target": str(row[3] or ""),
        "session_id": str(row[4] or ""),
        "metadata": metadata,
    }


def complete_capture_intent(
    conn: sqlite3.Connection, intent_id: int, *, timestamp: str = ""
) -> bool:
    """Mark one claimed intent consumed. Does not commit."""

    at = timestamp or _now_iso()
    cursor = conn.execute(
        """
        UPDATE capture_intents
        SET status = 'completed', completed_at = ?, updated_at = ?
        WHERE id = ? AND status = 'processing'
        """,
        (at, at, int(intent_id)),
    )
    return cursor.rowcount == 1


def requeue_capture_intent(
    conn: sqlite3.Connection, intent_id: int, *, timestamp: str = ""
) -> bool:
    """Return a failed processing row to pending for idempotent retry."""

    at = timestamp or _now_iso()
    cursor = conn.execute(
        """
        UPDATE capture_intents
        SET status = 'pending', claimed_at = '', updated_at = ?
        WHERE id = ? AND status = 'processing'
        """,
        (at, int(intent_id)),
    )
    return cursor.rowcount == 1


def release_stale_processing(conn: sqlite3.Connection, *, timestamp: str = "") -> int:
    """Requeue processing rows after a simulated or real process restart."""

    ensure_capture_intent_schema(conn)
    at = timestamp or _now_iso()
    cursor = conn.execute(
        """
        UPDATE capture_intents
        SET status = 'pending', claimed_at = '', updated_at = ?
        WHERE status = 'processing'
        """,
        (at,),
    )
    return int(cursor.rowcount or 0)


def _age_seconds(created_at: str) -> float:
    raw = str(created_at or "").strip()
    if not raw:
        return 0.0
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds())


def capture_intent_report(
    conn: sqlite3.Connection, *, capacity: int | None = None
) -> dict[str, Any]:
    """Return sanitized queue gauges. Never includes local filesystem paths."""

    bound = (
        max(MIN_CAPTURE_QUEUE_CAPACITY, min(MAX_CAPTURE_QUEUE_CAPACITY, int(capacity)))
        if capacity is not None
        else DEFAULT_CAPTURE_QUEUE_CAPACITY
    )
    empty = {
        "status": "schema_missing",
        "capacity": bound,
        "depth": 0,
        "pending": 0,
        "processing": 0,
        "oldest_age_seconds": 0.0,
        "rejected": 0,
        "deferred": 0,
    }
    if not _table_exists(conn, "capture_intents"):
        return empty
    counts = {"pending": 0, "processing": 0, "completed": 0}
    for row in conn.execute("SELECT status, COUNT(*) FROM capture_intents GROUP BY status"):
        status = str(row[0] or "")
        if status in counts:
            counts[status] = int(row[1] or 0)
    oldest = conn.execute(
        """
        SELECT MIN(created_at) FROM capture_intents
        WHERE status IN ('pending', 'processing')
        """
    ).fetchone()
    rejected = 0
    deferred = 0
    if _table_exists(conn, "capture_intent_stats"):
        stats = conn.execute(
            "SELECT rejected_count, deferred_count FROM capture_intent_stats WHERE id = 1"
        ).fetchone()
        if stats is not None:
            rejected = int(stats[0] or 0)
            deferred = int(stats[1] or 0)
    return {
        "status": sanitize_report_text("ok"),
        "capacity": bound,
        "depth": counts["pending"] + counts["processing"],
        "pending": counts["pending"],
        "processing": counts["processing"],
        "oldest_age_seconds": _age_seconds(str(oldest[0] or "") if oldest else ""),
        "rejected": rejected,
        "deferred": deferred,
    }


def _busy_timeout_ms(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        return int(row[0] or 0) if row is not None else 0
    except Exception:
        return 0


def _set_busy_timeout_ms(conn: sqlite3.Connection, milliseconds: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {max(0, int(milliseconds))}")


def _is_sqlite_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "busy" in text or "locked" in text


def persist_from_provider(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one intent under a bounded lock, or return deferred/rejected."""

    from .write_kernel import has_positive_write_authority

    config = getattr(provider, "_config", None)
    capacity = queue_capacity(config if isinstance(config, Mapping) else None)
    if not has_positive_write_authority(provider):
        return {
            "status": "rejected",
            "reason": "write_authority",
            "intent_id": None,
            "depth": 0,
            "capacity": capacity,
            "durable_accounted": False,
        }
    require = getattr(provider, "_require_conn", None)
    lock = getattr(provider, "_lock", None)
    if not callable(require) or lock is None:
        return {
            "status": "deferred",
            "reason": "durable_store_unavailable",
            "intent_id": None,
            "depth": 0,
            "capacity": capacity,
            "_fallback": True,
            "durable_accounted": False,
        }
    acquired = False
    try:
        acquired = bool(lock.acquire(timeout=PERSIST_LOCK_TIMEOUT_SECONDS))
    except TypeError:
        lock.acquire()
        acquired = True
    if not acquired:
        return {
            "status": "deferred",
            "reason": "truth_writer_busy",
            "intent_id": None,
            "depth": 0,
            "capacity": capacity,
            "durable_accounted": False,
        }
    previous_busy_ms: int | None = None
    persist_conn: sqlite3.Connection | None = None
    try:
        conn = require()
        if not _is_sqlite_connection(conn):
            return {
                "status": "deferred",
                "reason": "durable_store_unavailable",
                "intent_id": None,
                "depth": 0,
                "capacity": capacity,
                "_fallback": True,
                "durable_accounted": False,
            }
        persist_conn = conn
        if conn.in_transaction:
            return {
                "status": "deferred",
                "reason": "truth_transaction_open",
                "intent_id": None,
                "depth": unconsumed_depth(conn)
                if _table_exists(conn, "capture_intents")
                else 0,
                "capacity": capacity,
                "durable_accounted": False,
            }
        previous_busy_ms = _busy_timeout_ms(conn)
        _set_busy_timeout_ms(
            conn, max(1, int(PERSIST_LOCK_TIMEOUT_SECONDS * 1000))
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = persist_capture_intent(
                conn,
                content=content,
                source=source,
                target=target,
                session_id=session_id,
                metadata=metadata,
                capacity=capacity,
            )
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return {
                "status": "deferred",
                "reason": "sqlite_busy",
                "intent_id": None,
                "depth": 0,
                "capacity": capacity,
                "durable_accounted": False,
            }
        raise
    finally:
        if persist_conn is not None and previous_busy_ms is not None:
            try:
                _set_busy_timeout_ms(persist_conn, previous_busy_ms)
            except Exception:
                pass
        if acquired:
            lock.release()


def merge_capture_queue_report(
    durable: Mapping[str, Any], provider: Any, *, capacity: int
) -> dict[str, Any]:
    """Combine durable gauges with sidecar and process-local leftovers."""

    extra_rejected = 0
    extra_deferred = 0
    try:
        from .capture_outcomes import sidecar_counter_totals

        extra_rejected, extra_deferred = sidecar_counter_totals(provider)
    except Exception:
        extra_rejected, extra_deferred = 0, 0
    report = {
        "status": sanitize_report_text(str(durable.get("status") or "ok")),
        "capacity": int(durable.get("capacity") or capacity),
        "depth": int(durable.get("depth") or 0),
        "pending": int(durable.get("pending") or 0),
        "processing": int(durable.get("processing") or 0),
        "oldest_age_seconds": float(durable.get("oldest_age_seconds") or 0.0),
        "rejected": int(durable.get("rejected") or 0)
        + int(getattr(provider, "_capture_queue_rejected", 0) or 0)
        + extra_rejected,
        "deferred": int(durable.get("deferred") or 0)
        + int(getattr(provider, "_capture_queue_deferred", 0) or 0)
        + extra_deferred,
    }
    return report


__all__ = [
    "DEFAULT_CAPTURE_QUEUE_CAPACITY",
    "MAX_CAPTURE_QUEUE_CAPACITY",
    "MIN_CAPTURE_QUEUE_CAPACITY",
    "PERSIST_LOCK_TIMEOUT_SECONDS",
    "capture_intent_report",
    "claim_next_capture_intent",
    "coalesce_key",
    "complete_capture_intent",
    "ensure_capture_intent_schema",
    "merge_capture_queue_report",
    "persist_capture_intent",
    "persist_from_provider",
    "queue_capacity",
    "record_deferred_capture_intent",
    "record_outcome_on_connection",
    "release_stale_processing",
    "requeue_capture_intent",
    "unconsumed_depth",
]
