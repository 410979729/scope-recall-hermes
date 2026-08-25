"""Truth-side membership ledger for incremental vector cardinality.

Ordinary Lance lookups cannot prove that ``where(id).limit(1)`` is an indexed
probe. This module keeps an explicit SQLite primary key on
``(generation_id, memory_id)`` so insert/update/delete can test existence
with a B-tree equality lookup instead of scanning the physical corpus.

The ledger is updated only after a successful physical mutation. Doctor and
other explicit audits may rebuild it from a full companion listing; ordinary
writes may not.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_vector_membership_schema(conn: sqlite3.Connection) -> None:
    """Create the membership ledger and backfill ready-state for empty generations.

    An empty generation (cached ``row_count`` and ``unique_id_count`` both 0)
    can honestly start ready: absence of a ledger row means the id is new.
    Non-empty historical generations stay unready until an explicit audit
    replaces the ledger. This helper never lists or scans a vector store.
    """

    owned_transaction = not bool(getattr(conn, "in_transaction", False))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_id_membership (
            generation_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            PRIMARY KEY (generation_id, memory_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_membership_state (
            generation_id TEXT PRIMARY KEY,
            ready INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO vector_membership_state(generation_id, ready, updated_at)
        SELECT generation_id, 1, COALESCE(updated_at, '')
        FROM vector_generations
        WHERE COALESCE(row_count, 0) = 0 AND COALESCE(unique_id_count, 0) = 0
        """
    )
    if owned_transaction and bool(getattr(conn, "in_transaction", False)):
        conn.commit()


def membership_is_ready(conn: sqlite3.Connection, generation_id: str) -> bool:
    """Return whether the ledger is authoritative for one generation."""

    resolved = str(generation_id or "").strip()
    if not resolved:
        return False
    row = conn.execute(
        """
        SELECT ready
        FROM vector_membership_state
        WHERE generation_id = ?
        LIMIT 1
        """,
        (resolved,),
    ).fetchone()
    if row is None:
        return False
    if isinstance(row, sqlite3.Row):
        return int(row["ready"] or 0) == 1
    return int(row[0] or 0) == 1


def mark_membership_ready(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    timestamp: str = "",
) -> None:
    """Mark one generation's ledger as the incremental existence source."""

    resolved = str(generation_id or "").strip()
    if not resolved:
        return
    conn.execute(
        """
        INSERT INTO vector_membership_state(generation_id, ready, updated_at)
        VALUES (?, 1, ?)
        ON CONFLICT(generation_id) DO UPDATE SET
            ready = 1,
            updated_at = excluded.updated_at
        """,
        (resolved, timestamp or _now_iso()),
    )


def mark_membership_unready(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    timestamp: str = "",
) -> None:
    """Forget ledger authority after an external non-zero recount.

    The caller supplied aggregate counts without the matching id set, so
    ordinary writes must not treat a missing ledger row as "does not exist".
    """

    resolved = str(generation_id or "").strip()
    if not resolved:
        return
    conn.execute(
        """
        INSERT INTO vector_membership_state(generation_id, ready, updated_at)
        VALUES (?, 0, ?)
        ON CONFLICT(generation_id) DO UPDATE SET
            ready = 0,
            updated_at = excluded.updated_at
        """,
        (resolved, timestamp or _now_iso()),
    )


def sync_membership_state_after_register(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    row_count: int,
    unique_id_count: int,
) -> None:
    """Set ready-state from a generation register/bootstrap write.

    Zero/zero is a new empty companion: the empty ledger is honest. Any
    non-zero external count invalidates readiness until Doctor/sync rebuilds
    the id set. Incremental cardinality persist must not call this helper.
    """

    if max(0, int(row_count or 0)) == 0 and max(0, int(unique_id_count or 0)) == 0:
        mark_membership_ready(conn, generation_id)
        return
    mark_membership_unready(conn, generation_id)


def apply_membership_mutation(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    memory_id: str,
    operation: str,
) -> bool | None:
    """Apply one post-mutation membership change and report prior existence.

    The primary-key equality lookup is the existence proof. ``None`` means
    the ledger is not ready or the arguments are unusable; callers must not
    invent a count adjustment. Transaction ownership stays with the caller
    so membership and cached counts commit or roll back together.
    """

    resolved_generation = str(generation_id or "").strip()
    resolved_id = str(memory_id or "").strip()
    resolved_operation = str(operation or "").strip().lower()
    if not resolved_generation or not resolved_id:
        return None
    if not membership_is_ready(conn, resolved_generation):
        return None
    existing = conn.execute(
        """
        SELECT 1
        FROM vector_id_membership
        WHERE generation_id = ? AND memory_id = ?
        LIMIT 1
        """,
        (resolved_generation, resolved_id),
    ).fetchone()
    existed = existing is not None
    if resolved_operation == "upsert":
        if not existed:
            conn.execute(
                """
                INSERT INTO vector_id_membership(generation_id, memory_id)
                VALUES (?, ?)
                """,
                (resolved_generation, resolved_id),
            )
        return existed
    if resolved_operation == "delete":
        if existed:
            conn.execute(
                """
                DELETE FROM vector_id_membership
                WHERE generation_id = ? AND memory_id = ?
                """,
                (resolved_generation, resolved_id),
            )
        return existed
    return None


def replace_generation_membership(
    conn: sqlite3.Connection,
    generation_id: str,
    memory_ids: Iterable[str],
    *,
    timestamp: str = "",
) -> None:
    """Replace one generation's ledger from an explicit full audit id set."""

    resolved = str(generation_id or "").strip()
    if not resolved:
        return
    unique_ids = []
    seen: set[str] = set()
    for item in memory_ids:
        memory_id = str(item or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        unique_ids.append(memory_id)
    conn.execute(
        "DELETE FROM vector_id_membership WHERE generation_id = ?",
        (resolved,),
    )
    conn.executemany(
        "INSERT INTO vector_id_membership(generation_id, memory_id) VALUES (?, ?)",
        [(resolved, memory_id) for memory_id in unique_ids],
    )
    mark_membership_ready(conn, resolved, timestamp=timestamp)


__all__ = [
    "apply_membership_mutation",
    "ensure_vector_membership_schema",
    "mark_membership_ready",
    "mark_membership_unready",
    "membership_is_ready",
    "replace_generation_membership",
    "sync_membership_state_after_register",
]
