"""Single executor for committed vector-outbox events.

SQLite owns vector intent and lease/CAS state.  This module is the only ordinary
runtime path that turns a committed outbox event into a physical companion
mutation.  Callers provide backend and embedding adapters but do not perform a
second direct write.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
import sqlite3
from typing import Any, Callable, Iterator, Sequence
import uuid

from .capture_filters import sanitize_report_text
from .vector_generation import (
    claim_vector_events,
    complete_vector_event,
    fail_vector_event,
)
from .vector_membership import membership_is_ready
from .vector_store import store_contains_id, store_id_lookup_is_indexed

ReplayResult = dict[str, int]

_MEMORY_COLUMNS = (
    "id",
    "scope_id",
    "source",
    "target",
    "content",
    "summary",
    "updated_at",
    "metadata",
)
_MAX_SNAPSHOT_PREPARE_ATTEMPTS = 3


@contextmanager
def _held(lock: Any) -> Iterator[None]:
    """Hold a caller lock when supplied, otherwise provide a no-op context."""

    if lock is None:
        yield
        return
    with lock:
        yield


def _memory_row(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row | None:
    """Read the vector payload fields for one current SQLite truth row."""

    return conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary,
               updated_at, metadata
        FROM memories
        WHERE id = ?
        """,
        (memory_id,),
    ).fetchone()


def _row_snapshot(row: sqlite3.Row | None) -> tuple[Any, ...] | None:
    """Return the fields that bind a prepared embedding to one truth revision."""

    if row is None:
        return None
    return tuple(row[column] for column in _MEMORY_COLUMNS)


def _truth_requires_delete(
    row: sqlite3.Row | None,
    should_index_row: Callable[[str, Any], bool],
) -> bool:
    """Resolve the physical operation from current truth, never stale intent."""

    if row is None:
        return True
    return not should_index_row(str(row["target"] or ""), row["metadata"])


def _record_for_row(
    row: sqlite3.Row,
    *,
    vector: Any,
    default_scope_id: str,
) -> dict[str, Any]:
    """Build one backend-neutral physical vector record."""

    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or default_scope_id),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "content": str(row["content"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "vector": vector,
    }


def replay_committed_vector_events(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    vector_store: Any,
    embedder: Any,
    vector_text: Callable[[str, str], str],
    should_index_row: Callable[[str, Any], bool],
    default_scope_id: str = "",
    db_lock: Any = None,
    mutation_context: Callable[[], AbstractContextManager[Any]],
    limit: int = 200,
    event_ids: Sequence[int] | None = None,
    worker_id: str = "",
    on_failure: Callable[[str], None] | None = None,
    after_replay: Callable[[], Any] | None = None,
    on_physical_mutation: Callable[[str, str, bool | None], Any] | None = None,
) -> ReplayResult:
    """Claim and causally apply committed events with crash-safe replay.

    Claim remains a separate commit so expired workers can be recovered.
    Embedding and backend I/O run under the physical mutation guard but outside
    every SQLite transaction. Short ``BEGIN IMMEDIATE`` pre/post fences validate
    the prepared truth snapshot and complete the lease CAS. A truth writer may
    therefore commit while a backend blocks; its higher outbox event remains the
    durable repair obligation and applies after the older physical mutation.

    A direct truth drift without a newer event triggers bounded re-preparation.
    A crash after physical mutation but before completion remains safe because
    backend upsert/delete operations are idempotent by memory id.

    ``after_replay`` is skipped when no event is claimed so empty replay cannot
    trigger a full companion audit. Successful physical mutations notify
    ``on_physical_mutation`` so callers can maintain cached counts from the
    SQLite membership ledger or an indexed backend probe. Unindexed Lance
    ``where(id).limit(1)`` filters are never issued here.
    """

    resolved_generation = str(generation_id or "").strip()
    if not resolved_generation or vector_store is None or embedder is None:
        return {"claimed": 0, "completed": 0, "failed": 0}

    resolved_worker = str(worker_id or f"runtime-{uuid.uuid4().hex}")
    max_events = max(1, int(limit or 1))
    claimed = 0
    completed = 0
    failed = 0

    while claimed < max_events:
        with _held(db_lock):
            events = claim_vector_events(
                conn,
                generation_id=resolved_generation,
                worker_id=resolved_worker,
                limit=1,
                event_ids=event_ids,
            )
            conn.commit()
        if not events:
            break

        event = events[0]
        event_id = int(event["id"])
        memory_id = str(event["memory_id"])
        claimed += 1
        try:
            event_applied = False
            for prepare_attempt in range(_MAX_SNAPSHOT_PREPARE_ATTEMPTS):
                with _held(db_lock):
                    prepared_row = _memory_row(conn, memory_id)
                prepared_snapshot = _row_snapshot(prepared_row)
                prepared_should_delete = _truth_requires_delete(
                    prepared_row, should_index_row
                )
                prepared_record: dict[str, Any] | None = None
                if not prepared_should_delete:
                    assert prepared_row is not None
                    prepared_vector = embedder.embed(
                        vector_text(
                            str(prepared_row["summary"] or ""),
                            str(prepared_row["content"] or ""),
                        )
                    )
                    prepared_record = _record_for_row(
                        prepared_row,
                        vector=prepared_vector,
                        default_scope_id=default_scope_id,
                    )

                retry_preparation = False
                with mutation_context():
                    # The preflight fence is intentionally short: it validates
                    # the prepared payload, then releases SQLite before any
                    # external physical mutation begins.
                    with _held(db_lock):
                        if conn.in_transaction:
                            raise RuntimeError(
                                "vector replay causal fence requires an idle SQLite connection"
                            )
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            newer_event = conn.execute(
                                """
                                SELECT 1
                                FROM vector_outbox
                                WHERE generation_id = ? AND memory_id = ? AND id > ?
                                LIMIT 1
                                """,
                                (resolved_generation, memory_id, event_id),
                            ).fetchone()
                            if newer_event is not None:
                                complete_vector_event(
                                    conn,
                                    event_id,
                                    worker_id=resolved_worker,
                                )
                                conn.commit()
                                event_applied = True
                            else:
                                current_row = _memory_row(conn, memory_id)
                                current_should_delete = _truth_requires_delete(
                                    current_row, should_index_row
                                )
                                retry_preparation = (
                                    _row_snapshot(current_row) != prepared_snapshot
                                    or current_should_delete != prepared_should_delete
                                    or (
                                        not current_should_delete
                                        and prepared_record is None
                                    )
                                )
                                conn.rollback()
                        except Exception:
                            conn.rollback()
                            raise

                    if not event_applied and not retry_preparation:
                        existed = None
                        with _held(db_lock):
                            ledger_ready = membership_is_ready(
                                conn, resolved_generation
                            )
                        if not ledger_ready and store_id_lookup_is_indexed(
                            vector_store
                        ):
                            existed = store_contains_id(vector_store, memory_id)
                        if prepared_should_delete:
                            vector_store.delete_by_ids([memory_id])
                            operation = "delete"
                        else:
                            assert prepared_record is not None
                            vector_store.upsert_records([prepared_record])
                            operation = "upsert"
                        if on_physical_mutation is not None:
                            on_physical_mutation(operation, memory_id, existed)

                        # A writer may have committed while physical I/O was in
                        # flight. Complete this lease under a short fence, but
                        # leave any higher event pending to repair the companion.
                        with _held(db_lock):
                            if conn.in_transaction:
                                raise RuntimeError(
                                    "vector replay completion fence requires an idle SQLite connection"
                                )
                            conn.execute("BEGIN IMMEDIATE")
                            try:
                                newer_event = conn.execute(
                                    """
                                    SELECT 1
                                    FROM vector_outbox
                                    WHERE generation_id = ? AND memory_id = ? AND id > ?
                                    LIMIT 1
                                    """,
                                    (resolved_generation, memory_id, event_id),
                                ).fetchone()
                                current_row = _memory_row(conn, memory_id)
                                if (
                                    newer_event is None
                                    and _row_snapshot(current_row)
                                    != prepared_snapshot
                                ):
                                    conn.rollback()
                                    retry_preparation = True
                                else:
                                    complete_vector_event(
                                        conn,
                                        event_id,
                                        worker_id=resolved_worker,
                                    )
                                    conn.commit()
                                    event_applied = True
                            except Exception:
                                conn.rollback()
                                raise

                if event_applied:
                    break
                if retry_preparation and (
                    prepare_attempt + 1 < _MAX_SNAPSHOT_PREPARE_ATTEMPTS
                ):
                    continue
                raise RuntimeError(
                    "vector truth changed repeatedly during causal replay preparation"
                )
            if not event_applied:  # pragma: no cover - defensive loop invariant
                raise RuntimeError(
                    "vector replay preparation exhausted without a result"
                )
            completed += 1
        except Exception as exc:
            safe_error = sanitize_report_text(str(exc))[:2000]
            with _held(db_lock):
                try:
                    if conn.in_transaction:
                        conn.rollback()
                    fail_vector_event(
                        conn,
                        event_id,
                        worker_id=resolved_worker,
                        error=safe_error,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
            failed += 1
            if on_failure is not None:
                on_failure(safe_error)
            break

    if claimed > 0 and after_replay is not None:
        after_replay()
    return {"claimed": claimed, "completed": completed, "failed": failed}


__all__ = ["ReplayResult", "replay_committed_vector_events"]
