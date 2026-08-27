"""Atomic lifecycle transitions across SQLite truth and rebuildable companions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Mapping

from .capture_filters import sanitize_report_text, sanitize_structured_value
from .fact_repository import require_fact_mutation_authority
from .freshness import upsert_memory_freshness
from .graph import lifecycle_is_hidden, load_metadata, sync_memory_entities
from .graph_relations import evaluate_relation_policy, upsert_relation
from .lifecycle_compat import resolve_lifecycle_request
from .lifecycle_policy import ordinary_recall_lifecycle_visible
from .lifecycle_registry import (
    DELETED_STATE,
    HARD_DELETE_DEFAULT,
    validate_lifecycle_transition,
)
from .relation_frequency_index import sync_relation_frequency_memory
from .sql_store import delete_rows, now_iso, record_governance_audit_event
from .sqlite_params import chunked_sql_parameters
from .vector_generation import enqueue_vector_event


class LifecycleConflictError(RuntimeError):
    """The reviewed row version/lifecycle no longer matches SQLite truth."""

    def __init__(
        self,
        message: str,
        *,
        memory_id: str,
        expected_updated_at: str = "",
        current_updated_at: str = "",
        expected_lifecycle: str = "",
        current_lifecycle: str = "",
    ) -> None:
        super().__init__(message)
        self.memory_id = memory_id
        self.expected_updated_at = expected_updated_at
        self.current_updated_at = current_updated_at
        self.expected_lifecycle = expected_lifecycle
        self.current_lifecycle = current_lifecycle

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "conflict",
            "id": self.memory_id,
            "expected_updated_at": self.expected_updated_at,
            "current_updated_at": self.current_updated_at,
            "expected_lifecycle": self.expected_lifecycle,
            "current_lifecycle": self.current_lifecycle,
            "error": str(self),
        }


class LifecycleNotFoundError(LookupError):
    """The exact memory id does not exist at transition time."""


def hard_delete_memories(
    conn: sqlite3.Connection,
    *,
    memory_ids: Sequence[str],
    scope_ids: Sequence[str] | None = None,
    vector_delete: Callable[[list[str]], None] | None = None,
    require_vector_delete: bool = True,
    actor: str,
    reason: str,
    operation_id: str = "",
    event_type: str = "",
    batch_id: str = "",
    timestamp: str = "",
    fact_mutation_authority: str = "",
    commit: bool = True,
) -> dict[str, Any]:
    """Delete truth and persist audit/vector intent in one transaction.

    By default this service owns ``BEGIN IMMEDIATE`` and commits SQLite truth,
    audit, and vector intent atomically. ``commit=False`` joins an existing
    caller-owned transaction. ``vector_delete`` is retained only for source
    compatibility and is deliberately never invoked; physical companion writes
    belong exclusively to the committed-outbox executor.
    """

    operation = resolve_lifecycle_request(
        operation_id=operation_id,
        legacy_event_type=event_type,
        legacy_action="hard_delete" if event_type else "",
        default_operation_id=HARD_DELETE_DEFAULT,
    )
    if operation.target_state != DELETED_STATE:
        raise ValueError(f"{operation.operation_id} is not a hard-delete operation")
    clean_ids = list(
        dict.fromkeys(
            str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()
        )
    )
    if not clean_ids:
        return {
            "ok": True,
            "deleted": 0,
            "ids": [],
            "event_ids": [],
            "outbox_enqueued": 0,
        }
    owns_transaction = bool(commit)
    if owns_transaction:
        if conn.in_transaction:
            raise RuntimeError("hard delete requires a clean SQLite transaction boundary")
        conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        raise RuntimeError("transaction-neutral hard delete requires an owner transaction")
    savepoint = f"hard_delete_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    savepoint_active = True
    scoped_ids: list[str] = []
    try:
        clean_scope_ids = list(
            dict.fromkeys(
                str(scope_id).strip()
                for scope_id in (scope_ids or [])
                if str(scope_id).strip()
            )
        )
        if scope_ids is not None:
            if not clean_scope_ids:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                savepoint_active = False
                if owns_transaction:
                    conn.commit()
                return {
                    "ok": True,
                    "deleted": 0,
                    "ids": [],
                    "event_ids": [],
                    "outbox_enqueued": 0,
                }
        rows: list[Any] = []
        reserved = len(clean_scope_ids) if scope_ids is not None else 0
        for id_chunk in chunked_sql_parameters(
            conn,
            clean_ids,
            reserved=reserved,
        ):
            id_placeholders = ",".join("?" for _ in id_chunk)
            params: list[str] = list(id_chunk)
            where = f"id IN ({id_placeholders})"
            if scope_ids is not None:
                where += (
                    " AND scope_id IN ("
                    + ",".join("?" for _ in clean_scope_ids)
                    + ")"
                )
                params.extend(clean_scope_ids)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id, scope_id, source, target, content, summary, updated_at, metadata
                    FROM memories
                    WHERE {where}
                    ORDER BY id
                    """,
                    params,
                ).fetchall()
            )
        rows.sort(key=lambda row: str(row["id"]))
        scoped_ids = [str(row["id"]) for row in rows]
        if not scoped_ids:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if owns_transaction:
                conn.commit()
            return {
                "ok": True,
                "deleted": 0,
                "ids": [],
                "event_ids": [],
                "outbox_enqueued": 0,
                }
        for row in rows:
            validate_lifecycle_transition(
                operation,
                current_state=str(
                    load_metadata(row["metadata"] or "{}").get("lifecycle")
                    or "active"
                ),
                target_state=DELETED_STATE,
            )
        require_fact_mutation_authority(
            conn,
            scoped_ids,
            operation="legacy memory hard delete",
            authority=fact_mutation_authority,
        )

        at = timestamp or now_iso()
        generation_id = _current_generation_id_read_only(conn)
        event_ids: list[str] = []
        outbox_enqueued = 0
        safe_reason = sanitize_report_text(reason or "hard delete")
        for row in rows:
            memory_id = str(row["id"])
            before_metadata = load_metadata(row["metadata"] or "{}")
            before = _snapshot(
                row,
                metadata=before_metadata,
                relations=_relations(conn, memory_id),
            )
            event_id = f"gov_{uuid.uuid4().hex}"
            record_governance_audit_event(
                conn,
                event_id=event_id,
                event_type=operation.legacy_event_type,
                action=operation.legacy_action,
                scope_id=str(row["scope_id"] or ""),
                target_id=memory_id,
                batch_id=str(batch_id or ""),
                before=before,
                after={"id": memory_id, "deleted": True},
                reason=safe_reason,
                actor=str(actor or "scope-recall:hard-delete"),
                dry_run=False,
                created_at=at,
            )
            event_ids.append(event_id)
            if _enqueue_vector_transition(
                conn,
                generation_id=generation_id,
                memory_id=memory_id,
                operation="delete",
                updated_at=at,
                reason=safe_reason,
            ):
                outbox_enqueued += 1
        if require_vector_delete and outbox_enqueued != len(scoped_ids):
            raise RuntimeError(
                "hard delete requires one durable vector delete outbox event per truth row: "
                f"expected={len(scoped_ids)}, enqueued={outbox_enqueued}"
            )
        deleted = delete_rows(
            conn,
            scoped_ids,
            scope_ids=clean_scope_ids if scope_ids is not None else None,
            commit=False,
        )
        if deleted != len(scoped_ids):
            raise RuntimeError(
                f"hard delete row-count mismatch: expected={len(scoped_ids)}, deleted={deleted}"
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        savepoint_active = False
        if owns_transaction:
            conn.commit()
        # Compatibility callbacks must never bypass causal outbox replay. Keep
        # a local reference consumption so strict linters and API readers see
        # that the argument is intentionally ignored rather than forgotten.
        del vector_delete
        vector_status = "pending" if require_vector_delete else "not_required"
        return {
            "ok": True,
            "durable": bool(owns_transaction),
            "transaction_pending": not owns_transaction,
            "deleted": deleted,
            "ids": scoped_ids,
            "event_ids": event_ids,
            "generation_id": generation_id,
            "outbox_enqueued": outbox_enqueued,
            "vector_status": vector_status,
            "vector_pending": vector_status == "pending",
            "vector_error": "",
        }
    except Exception as exc:
        if savepoint_active and conn.in_transaction:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as rollback_exc:
                exc.add_note(
                    "hard-delete savepoint rollback failed: "
                    + sanitize_report_text(str(rollback_exc))
                )
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _relations(conn: sqlite3.Connection, memory_id: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "memory_relations"):
        return []
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type, confidence, note, created_at
        FROM memory_relations
        WHERE source_memory_id = ? OR target_memory_id = ?
        ORDER BY source_memory_id, target_memory_id, relation_type
        """,
        (memory_id, memory_id),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _snapshot(
    row: sqlite3.Row, *, metadata: Mapping[str, Any], relations: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "content": str(row["content"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": dict(metadata),
        "relations": relations,
    }


def _current_generation_id_read_only(conn: sqlite3.Connection) -> str:
    if not _table_exists(conn, "vector_generation_state"):
        return ""
    row = conn.execute(
        "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
    ).fetchone()
    return str(row[0] or "") if row else ""


def _enqueue_vector_transition(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    memory_id: str,
    operation: str,
    updated_at: str,
    reason: str,
) -> str:
    if not generation_id or not _table_exists(conn, "vector_outbox"):
        return ""
    event_material = "\x1f".join((generation_id, memory_id, operation, updated_at))
    event_key = hashlib.sha256(event_material.encode("utf-8")).hexdigest()
    enqueue_vector_event(
        conn,
        event_key=event_key,
        generation_id=generation_id,
        memory_id=memory_id,
        operation=operation,
        payload={
            "updated_at": updated_at,
            "reason": sanitize_report_text(reason)[:500],
        },
    )
    return event_key


def transition_memory_lifecycle(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    lifecycle: str,
    metadata_updates: Mapping[str, Any] | None = None,
    restore_relations: list[Mapping[str, Any]] | None = None,
    expected_updated_at: str = "",
    expected_lifecycle: str = "",
    actor: str,
    reason: str,
    operation_id: str = "",
    event_type: str = "",
    action: str = "",
    batch_id: str = "",
    timestamp: str = "",
    fact_mutation_authority: str = "",
    replace_metadata: bool = False,
) -> dict[str, Any]:
    """Apply one lifecycle transition under a savepoint without committing.

    The memory row, FTS, entities, bidirectional relations, freshness, audit
    event, and durable vector replay intent either all change or all roll back.
    The caller owns the outer transaction and commit boundary.

    ``metadata_updates`` merge into the current metadata by default. Pass
    ``replace_metadata=True`` only for evidence-backed restore that must write
    the supplied snapshot exactly; this flag stays default-off.
    """

    memory_id = str(memory_id or "").strip()
    lifecycle = str(lifecycle or "").strip().lower()
    if not memory_id or not lifecycle:
        raise ValueError("memory_id and lifecycle are required")
    operation = resolve_lifecycle_request(
        operation_id=operation_id,
        legacy_event_type=event_type,
        legacy_action=action,
    )
    if operation.target_state == DELETED_STATE:
        raise ValueError(
            f"{operation.operation_id} must use hard_delete_memories"
        )
    if operation.fact_authority_required and not str(fact_mutation_authority).strip():
        raise PermissionError(
            f"{operation.operation_id} requires explicit fact mutation authority"
        )
    started_outer_transaction = not bool(getattr(conn, "in_transaction", False))
    if started_outer_transaction:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = f"lifecycle_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        row = conn.execute(
            """
            SELECT id, scope_id, source, target, content, summary, updated_at, metadata
            FROM memories WHERE id = ?
            """,
            (memory_id,),
        ).fetchone()
        if row is None:
            raise LifecycleNotFoundError(f"memory not found: {memory_id}")
        before_metadata = load_metadata(row["metadata"] or "{}")
        current_updated_at = str(row["updated_at"] or "")
        current_lifecycle = (
            str(before_metadata.get("lifecycle") or "active").strip().lower()
        )
        validate_lifecycle_transition(
            operation,
            current_state=current_lifecycle,
            target_state=lifecycle,
        )
        if current_lifecycle != lifecycle:
            require_fact_mutation_authority(
                conn,
                [memory_id],
                operation=f"legacy lifecycle transition {current_lifecycle}->{lifecycle}",
                authority=fact_mutation_authority,
            )
        if expected_updated_at and current_updated_at != str(expected_updated_at):
            raise LifecycleConflictError(
                f"memory {memory_id} changed after review",
                memory_id=memory_id,
                expected_updated_at=str(expected_updated_at),
                current_updated_at=current_updated_at,
                expected_lifecycle=str(expected_lifecycle or ""),
                current_lifecycle=current_lifecycle,
            )
        if (
            expected_lifecycle
            and current_lifecycle != str(expected_lifecycle).strip().lower()
        ):
            raise LifecycleConflictError(
                f"memory {memory_id} lifecycle changed after review",
                memory_id=memory_id,
                expected_updated_at=str(expected_updated_at or current_updated_at),
                current_updated_at=current_updated_at,
                expected_lifecycle=str(expected_lifecycle),
                current_lifecycle=current_lifecycle,
            )
        before_relations = _relations(conn, memory_id)
        before = _snapshot(row, metadata=before_metadata, relations=before_relations)
        at = timestamp or now_iso()
        if replace_metadata:
            after_metadata = dict(metadata_updates or {})
            replacement_lifecycle = str(
                after_metadata.get("lifecycle") or "active"
            ).strip().lower()
            if replacement_lifecycle != lifecycle:
                raise ValueError(
                    "replacement metadata lifecycle does not match requested lifecycle"
                )
        else:
            after_metadata = dict(before_metadata)
            after_metadata.update(dict(metadata_updates or {}))
            if current_lifecycle != lifecycle:
                after_metadata.setdefault("previous_lifecycle", current_lifecycle)
            after_metadata["lifecycle"] = lifecycle
        safe_after_metadata, _ = sanitize_structured_value(after_metadata)
        after_metadata = (
            safe_after_metadata
            if isinstance(safe_after_metadata, dict)
            else {}
        )
        effective_lifecycle = str(
            after_metadata.get("lifecycle") or "active"
        ).strip().lower()
        if effective_lifecycle != lifecycle:
            raise ValueError(
                "sanitized replacement metadata lifecycle does not match requested lifecycle"
            )
        if after_metadata == before_metadata and not restore_relations:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return {
                "ok": True,
                "applied": False,
                "status": "no_change",
                "id": memory_id,
                "before": before,
                "after": before,
                "updated_at": current_updated_at,
                "event_id": "",
                "generation_id": "",
                "vector_operation": "none",
                "outbox_enqueued": False,
                "vector_outbox_key": "",
                "relation_restore": {
                    "requested": 0,
                    "restored": 0,
                    "skipped": [],
                },
            }
        cursor = conn.execute(
            "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
            (_json_dumps(after_metadata), at, memory_id, current_updated_at),
        )
        if cursor.rowcount != 1:
            fresh = conn.execute(
                "SELECT updated_at, metadata FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            fresh_updated_at = (
                str(fresh["updated_at"] or "") if fresh is not None else ""
            )
            fresh_lifecycle = str(
                load_metadata(fresh["metadata"] if fresh is not None else "{}").get(
                    "lifecycle"
                )
                or "active"
            )
            raise LifecycleConflictError(
                f"memory {memory_id} update CAS conflict",
                memory_id=memory_id,
                expected_updated_at=current_updated_at,
                current_updated_at=fresh_updated_at,
                expected_lifecycle=current_lifecycle,
                current_lifecycle=fresh_lifecycle,
            )

        relation_restore: dict[str, Any] = {
            "requested": len(restore_relations or []),
            "restored": 0,
            "skipped": [],
        }
        hidden = lifecycle_is_hidden(
            {**after_metadata, "lifecycle": effective_lifecycle}
        )
        if hidden:
            if _table_exists(conn, "memories_fts"):
                conn.execute(
                    "DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,)
                )
            if _table_exists(conn, "memory_entities"):
                conn.execute(
                    "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
                )
            if _table_exists(conn, "memory_relations"):
                conn.execute(
                    "DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?",
                    (memory_id, memory_id),
                )
            if _table_exists(conn, "fact_freshness"):
                conn.execute(
                    "DELETE FROM fact_freshness WHERE subject_type = 'memory' AND subject_id = ?",
                    (memory_id,),
                )
            vector_operation = "delete"
        else:
            if _table_exists(conn, "memories_fts"):
                conn.execute(
                    "DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,)
                )
                conn.execute(
                    "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
                    (memory_id, str(row["content"] or ""), str(row["summary"] or "")),
                )
            if _table_exists(conn, "memory_entities"):
                sync_memory_entities(
                    conn,
                    memory_id=memory_id,
                    content=str(row["content"] or ""),
                    target=str(row["target"] or ""),
                    metadata=after_metadata,
                )
            upsert_memory_freshness(
                conn,
                memory_id=memory_id,
                metadata=after_metadata,
                content=str(row["content"] or ""),
                commit=False,
            )
            if restore_relations and _table_exists(conn, "memory_relations"):
                skipped_relations = relation_restore["skipped"]
                assert isinstance(skipped_relations, list)
                for relation in restore_relations:
                    source_id = str(relation.get("source_memory_id") or "")
                    target_id = str(relation.get("target_memory_id") or "")
                    relation_type = str(relation.get("relation_type") or "")
                    if memory_id not in {source_id, target_id}:
                        skipped_relations.append(
                            {
                                "allowed": False,
                                "reason": "unrelated_endpoint",
                                "source_memory_id": source_id,
                                "target_memory_id": target_id,
                                "relation_type": relation_type,
                            }
                        )
                        continue
                    decision = evaluate_relation_policy(
                        conn,
                        source_memory_id=source_id,
                        target_memory_id=target_id,
                        relation_type=relation_type,
                        same_scope_only=True,
                        require_visible_endpoints=True,
                        reject_contradiction_conflicts=True,
                    )
                    if not decision["allowed"]:
                        skipped_relations.append(decision)
                        continue
                    inserted = upsert_relation(
                        conn,
                        source_memory_id=source_id,
                        target_memory_id=target_id,
                        relation_type=relation_type,
                        confidence=float(relation.get("confidence") or 0.5),
                        note=str(relation.get("note") or ""),
                        created_at=str(relation.get("created_at") or at),
                    )
                    if inserted:
                        relation_restore["restored"] = int(relation_restore["restored"]) + 1
                    else:
                        skipped_relations.append({**decision, "allowed": False, "reason": "already_exists"})
            vector_operation = "upsert"

        if not ordinary_recall_lifecycle_visible(
            lifecycle=effective_lifecycle,
            target=str(row["target"] or ""),
        ):
            vector_operation = "delete"

        sync_relation_frequency_memory(conn, memory_id)
        generation_id = _current_generation_id_read_only(conn)
        vector_outbox_key = _enqueue_vector_transition(
            conn,
            generation_id=generation_id,
            memory_id=memory_id,
            operation=vector_operation,
            updated_at=at,
            reason=reason,
        )
        outbox_enqueued = bool(vector_outbox_key)
        after_row = conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert after_row is not None
        after = _snapshot(after_row, metadata=after_metadata, relations=_relations(conn, memory_id))
        if relation_restore["requested"]:
            after["relation_restore"] = relation_restore
        event_id = f"gov_{uuid.uuid4().hex}"
        record_governance_audit_event(
            conn,
            event_id=event_id,
            event_type=operation.legacy_event_type,
            action=operation.legacy_action,
            scope_id=str(row["scope_id"] or ""),
            target_id=memory_id,
            batch_id=str(batch_id or ""),
            before=before,
            after=after,
            reason=str(reason or "lifecycle transition"),
            actor=str(actor or "scope-recall:lifecycle"),
            dry_run=False,
            created_at=at,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {
            "ok": True,
            "applied": True,
            "id": memory_id,
            "before": before,
            "after": after,
            "updated_at": at,
            "event_id": event_id,
            "generation_id": generation_id,
            "vector_operation": vector_operation,
            "outbox_enqueued": outbox_enqueued,
            "vector_outbox_key": vector_outbox_key,
            "relation_restore": relation_restore,
        }
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_outer_transaction:
            conn.rollback()
        raise
