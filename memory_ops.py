"""High-level memory operations behind Scope Recall tools: store, search, update, merge, forget, govern, and explain.

This layer coordinates SQLite truth, vector companion state, graph evidence, and audit receipts; mutations must preserve rollback semantics."""

from __future__ import annotations

import json
import logging
import time  # noqa: F401
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from ._internal.application.memory_commands import DeleteMemoriesResult

from .capture import capture_mutation_barrier, store_now
from .writer_lease import sanitized_truth_writer_owner  # noqa: F401
from .capture_filters import sanitize_report_text, sanitize_structured_value
from .fact_repository import (
    FactMutationAuthorityError,
    fact_ownership_for_memories,
)
from .gating import compact_text
from .graph import (
    clamp_float,
    lifecycle_visible_sql,
    load_metadata,
    normalize_entity,  # noqa: F401
)
from .governance import (
    classify_memory,
    is_conflicting,
    merge_memory_text,
    semantic_similarity,
)
from .lifecycle_service import hard_delete_memories, transition_memory_lifecycle
from .lifecycle_policy import (
    PROFILE_HIDDEN_LIFECYCLES,
    ordinary_recall_lifecycle_visible_sql,
)
from .lifecycle_registry import (
    GOVERNANCE_CLASSIFY_METADATA,
    HARD_DELETE_DEDUPE,
    HARD_DELETE_EXPLICIT,
    HARD_DELETE_MERGE_SOURCE,
    SCOPE_FORGET_ARCHIVE,
)
from .models import recall_scope_mode, resolve_store_scope_mode  # noqa: F401
from .memory_text_merge import automatic_merge_is_safe
from .memory_mutation import MemoryMutationService
from .relation_extraction import sync_extracted_relations_for_memory
from .response_schemas import retention_response_contract
from .graph_relations import graph_relation_stats  # noqa: F401
from .sql_store import (
    exact_duplicate_groups,
    iter_curated_entries,  # noqa: F401
    record_governance_audit_event,
    update_row,
)
from .sqlite_params import chunked_sql_parameters
from .vector_generation import enqueue_current_vector_event
from .vector_runtime import (
    mark_vector_needs_repair,
    mark_vector_replay_degraded,
    refresh_vector_audit,  # noqa: F401
    replay_vector_outbox,
    replay_vector_outbox_events,
    setup_vector_layer,
    vector_delete_intent_required,
)

logger = logging.getLogger(__name__)

from .memory_queries import (  # noqa: E402
    benchmark_queries,  # noqa: F401
    context_payload,  # noqa: F401
    explain_query,  # noqa: F401
    export_memories,  # noqa: F401
    hygiene_report,  # noqa: F401
    inspect_memory,  # noqa: F401
    probe_entity,  # noqa: F401
    profile_payload,  # noqa: F401
    related_entities,  # noqa: F401
    stats_payload,
)


from ._internal.memory.scope import (  # noqa: E402
    accessible_scope_params as _accessible_scope_params,  # noqa: F401
    normalized_scope_mode as _normalized_scope_mode,
    payload_entities as _payload_entities,  # noqa: F401
    scope_params as _scope_params,  # noqa: F401
    writable_scope_params as _writable_scope_params,
)


def _require_command_method(provider: Any, name: str) -> Any:
    getter = getattr(provider, name, None)
    if callable(getter):
        return getter
    raise TypeError(f"MemoryCommandPort.{name} is required")


def _command_conn(provider: Any) -> Any:
    return _require_command_method(provider, "query_connection")()


def _command_lock(provider: Any) -> Any:
    return _require_command_method(provider, "query_lock")()


def _command_config(provider: Any) -> dict[str, Any]:
    raw = _require_command_method(provider, "config_view")()
    return dict(raw) if isinstance(raw, dict) else {}


def _command_config_value(provider: Any, key: str, default: Any = None) -> Any:
    return _require_command_method(provider, "config_value")(key, default)


def _command_clean_text(provider: Any, text: Any) -> str:
    return str(_require_command_method(provider, "clean_text")(text) or "")


def _command_scope_view(provider: Any) -> dict[str, Any]:
    raw = _require_command_method(provider, "query_scope_view")()
    return dict(raw) if isinstance(raw, dict) else {}


def _command_vector_status(provider: Any) -> dict[str, Any]:
    raw = _require_command_method(provider, "vector_status_view")()
    return dict(raw) if isinstance(raw, dict) else {}


def _domain_writable_scope_ids(provider: Any) -> list[str]:
    getter = getattr(provider, "writable_scope_ids", None)
    if callable(getter):
        raw = getter()
        values = list(raw) if isinstance(raw, (list, tuple)) else []
        return [str(item) for item in values if str(item)]
    return _writable_scope_params(provider)


def _domain_writable_placeholders(provider: Any) -> str:
    params = _domain_writable_scope_ids(provider)
    return ",".join("?" for _ in params) or "NULL"


def fact_owned_memory_ids(provider: Any, ids: list[str]) -> list[str]:
    """Return fact-owned IDs only when they are writable in this provider scope."""

    requested = sorted({str(memory_id).strip() for memory_id in ids if str(memory_id).strip()})
    if not requested:
        return []
    with _command_lock(provider):
        conn = _command_conn(provider)
        scope_params = _domain_writable_scope_ids(provider)
        rows: list[Any] = []
        for id_chunk in chunked_sql_parameters(
            conn,
            requested,
            reserved=len(scope_params),
        ):
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id FROM memories
                    WHERE id IN ({','.join('?' for _ in id_chunk)})
                      AND scope_id IN ({_domain_writable_placeholders(provider)})
                    ORDER BY id
                    """,
                    [*id_chunk, *scope_params],
                ).fetchall()
            )
        scoped_ids = [str(row["id"]) for row in rows]
        return sorted(fact_ownership_for_memories(conn, scoped_ids))


def _fact_mutation_error_payload(
    conn: Any,
    ids: list[str],
    *,
    operation: str,
) -> dict[str, Any] | None:
    ownership = fact_ownership_for_memories(conn, ids)
    if not ownership:
        return None
    return FactMutationAuthorityError(operation, ownership).as_dict()


def _relation_pair_budget(provider: Any) -> int:
    try:
        return max(
            1,
            min(
                5000,
                int(
                    _command_config(provider).get("relation_extraction_max_pairs", 1000)
                    or 1000
                ),
            ),
        )
    except (TypeError, ValueError):
        return 1000


def _relation_local_neighbor_limit(provider: Any) -> int:
    pair_budget = _relation_pair_budget(provider)
    try:
        configured = int(_command_config(provider).get("relation_sync_neighbor_limit", 32) or 32)
    except (TypeError, ValueError):
        configured = 32
    return max(1, min(pair_budget, configured, 256))


def _rollback_provider_conn_after_error(provider: Any, context: str) -> None:
    rollback = getattr(provider, "rollback_conn_after_error", None)
    if callable(rollback):
        rollback(context)
        return
    rollback = getattr(provider, "rollback_conn_after_error", None)
    if callable(rollback):
        rollback(context)


def _record_relation_sync_debt(
    provider: Any,
    *,
    memory_id: str,
    scope_id: str,
    batch_id: str,
    result: dict[str, Any],
) -> bool:
    """Persist a bounded, redacted receipt when graph work is deferred."""

    if not bool(result.get("deferred")):
        return False
    after = {
        key: result.get(key)
        for key in (
            "candidate_count",
            "max_candidates",
            "compared_pair_count",
            "total_peer_count",
            "selected_peer_count",
            "inserted",
            "deleted",
        )
    }
    reason = str(result.get("deferred_reason") or "relation rebuild queued")
    try:
        with _command_lock(provider):
            conn = _command_conn(provider)
            record_governance_audit_event(
                conn,
                event_id=uuid.uuid4().hex,
                event_type="relation_extraction",
                action="sync_deferred",
                scope_id=scope_id,
                target_id=memory_id,
                batch_id=f"relation-sync:{batch_id}",
                after=after,
                reason=reason,
                actor="scope-recall-relation-sync",
            )
            conn.commit()
    except Exception:
        _rollback_provider_conn_after_error(
            provider, "relation extraction deferred audit"
        )
        logger.exception("Scope Recall relation extraction deferred audit failed")
    logger.info(
        "Scope Recall relation rebuild deferred for memory %s: %s",
        memory_id,
        reason,
    )
    return True


def store_memory_now(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
    semantic_merge: bool = False,
    scope_mode: str | None = None,
) -> tuple[str, bool, str]:
    resolved_scope_mode = _normalized_scope_mode(provider, target, source, scope_mode)
    if semantic_merge and not allow_duplicate and target in {"user", "ops", "project"}:
        merge_id, existing_content, merged_content = find_semantic_merge_candidate(
            provider, content, target, scope_mode=resolved_scope_mode
        )
        if merge_id:
            if (
                existing_content.strip().casefold() == content.strip().casefold()
                or merged_content.strip() == existing_content.strip()
            ):
                return merge_id, False, "duplicate"
            update_result = update_memory(
                provider,
                merge_id,
                merged_content,
                target,
            )
            updated = (
                bool(update_result[0])
                if isinstance(update_result, tuple)
                else bool(update_result)
            )
            if not updated:
                raise RuntimeError(
                    f"semantic merge update failed for memory {merge_id}"
                )
            return merge_id, False, "merged"
    relation_sync_enabled = bool(
        _command_config(provider).get("relation_extraction_enabled", True)
    )
    relation_scope_id = _expected_scope_id_for_mode(provider, resolved_scope_mode)

    def before_store_commit(conn: Any, stored_memory_id: str) -> dict[str, Any] | None:
        _mark_conflicts_for_memory(
            provider,
            memory_id=stored_memory_id,
            content=content,
            target=target,
            commit=False,
        )
        if not relation_sync_enabled:
            return None
        return sync_extracted_relations_for_memory(
            conn,
            memory_id=stored_memory_id,
            scope_ids=[relation_scope_id],
            batch_id="store",
            max_pairs=_relation_pair_budget(provider),
            local_peer_limit=_relation_local_neighbor_limit(provider),
            commit=False,
        )

    memory_id, inserted, relation_result = store_now(
        provider,
        content=content,
        source=source,
        target=target,
        session_id=session_id,
        metadata=metadata,
        allow_duplicate=allow_duplicate,
        scope_mode=resolved_scope_mode,
        before_commit=before_store_commit,
    )
    relation_sync_deferred = False
    if inserted and relation_result is not None:
        relation_sync_deferred = _record_relation_sync_debt(
            provider,
            memory_id=memory_id,
            scope_id=relation_scope_id,
            batch_id="store",
            result=relation_result,
        )
    if not inserted:
        outcome = "duplicate" if memory_id else "skipped"
    elif relation_sync_deferred:
        outcome = "stored_relation_sync_deferred"
    elif relation_sync_enabled:
        outcome = "stored"
    else:
        outcome = "stored_relation_sync_disabled"
    return memory_id, inserted, outcome


def _conflict_peer_ids(conn: Any, memory_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT target_memory_id AS peer_id
        FROM memory_relations
        WHERE source_memory_id = ? AND relation_type = 'contradicts'
        UNION
        SELECT source_memory_id AS peer_id
        FROM memory_relations
        WHERE target_memory_id = ? AND relation_type = 'contradicts'
        """,
        (memory_id, memory_id),
    ).fetchall()
    return sorted(
        {
            str(row["peer_id"])
            for row in rows
            if str(row["peer_id"]) and str(row["peer_id"]) != memory_id
        }
    )


def _sync_conflict_metadata(conn: Any, memory_id: str) -> None:
    row = conn.execute(
        "SELECT metadata FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return
    metadata_payload = load_metadata(row["metadata"] if row is not None else "{}")
    conflict_ids = _conflict_peer_ids(conn, memory_id)
    relation_types = metadata_payload.get("relation_types")
    if not isinstance(relation_types, list):
        relation_types = []
    relation_types = [
        str(item) for item in relation_types if str(item) and str(item) != "contradicts"
    ]
    if conflict_ids:
        relation_types.append("contradicts")
        metadata_payload["conflict_review_ids"] = conflict_ids
        metadata_payload["conflict_count"] = len(conflict_ids)
        metadata_payload["conflict_review_count"] = len(conflict_ids)
        metadata_payload["needs_conflict_review"] = True
    else:
        metadata_payload["conflict_review_ids"] = []
        metadata_payload["conflict_count"] = 0
        metadata_payload["conflict_review_count"] = 0
        metadata_payload["needs_conflict_review"] = False
    metadata_payload["relation_types"] = relation_types
    safe_metadata, _ = sanitize_structured_value(metadata_payload)
    metadata_payload = safe_metadata if isinstance(safe_metadata, dict) else {}
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True), memory_id),
    )


def _sync_conflict_metadata_for_ids(conn: Any, memory_ids: set[str]) -> None:
    for related_id in sorted(
        {str(memory_id) for memory_id in memory_ids if str(memory_id)}
    ):
        _sync_conflict_metadata(conn, related_id)


def _mark_conflicts_for_memory(
    provider: Any,
    *,
    memory_id: str,
    content: str,
    target: str,
    rebuild_existing: bool = False,
    commit: bool = True,
) -> int:
    """Record deterministic contradiction edges for a memory and keep conflict metadata current."""

    conn = _command_conn(provider)
    now = datetime.now(timezone.utc).isoformat()
    with _command_lock(provider):
        affected_ids: set[str] = {memory_id}
        if rebuild_existing:
            affected_ids.update(_conflict_peer_ids(conn, memory_id))
            conn.execute(
                """
                DELETE FROM memory_relations
                WHERE relation_type = 'contradicts'
                  AND (source_memory_id = ? OR target_memory_id = ?)
                """,
                (memory_id, memory_id),
            )
        rows = conn.execute(
            """
            SELECT m.id, m.content
            FROM memories m
            WHERE m.id != ?
              AND m.target = ?
              AND m.scope_id IN ({})
              AND {}
            ORDER BY m.updated_at DESC
            LIMIT 50
            """.format(
                _domain_writable_placeholders(provider), lifecycle_visible_sql("m")
            ),
            [memory_id, target, *_domain_writable_scope_ids(provider)],
        ).fetchall()
        conflicting_ids = [
            str(row["id"])
            for row in rows
            if is_conflicting(str(row["content"]), content)
        ]
        for target_id in conflicting_ids:
            affected_ids.add(target_id)
            for source_id, related_id in (
                (memory_id, target_id),
                (target_id, memory_id),
            ):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                    VALUES (?, ?, 'contradicts', ?, ?, ?)
                    """,
                    (
                        source_id,
                        related_id,
                        0.74,
                        f"conflict-review: contradicts memory {related_id}",
                        now,
                    ),
                )
        if rebuild_existing or conflicting_ids:
            _sync_conflict_metadata_for_ids(conn, affected_ids)
            if commit:
                conn.commit()
    return len(conflicting_ids)


def find_semantic_merge_candidate(
    provider: Any, content: str, target: str, *, scope_mode: str | None = None
) -> tuple[str, str, str]:
    threshold = float(_command_config_value(provider,"semantic_merge_threshold", 0.72))
    conn = _command_conn(provider)
    resolved_scope_mode = _normalized_scope_mode(
        provider, target, "tool-store", scope_mode
    )
    scope_id = _expected_scope_id_for_mode(provider, resolved_scope_mode)
    if not scope_id or scope_id not in _domain_writable_scope_ids(provider):
        return "", "", ""
    with _command_lock(provider):
        rows = conn.execute(
            f"""
            SELECT m.id, m.content
            FROM memories m
            WHERE m.scope_id = ?
              AND m.target = ?
              AND {ordinary_recall_lifecycle_visible_sql("m")}
            ORDER BY m.updated_at DESC
            LIMIT 50
            """,
            [scope_id, target],
        ).fetchall()
    best_id = ""
    best_content = ""
    best_score = 0.0
    for row in rows:
        existing = str(row["content"])
        if existing.strip().casefold() == content.strip().casefold():
            return str(row["id"]), existing, existing
        if is_conflicting(existing, content):
            continue
        score = semantic_similarity(existing, content)
        if score > best_score:
            best_id = str(row["id"])
            best_content = existing
            best_score = score
    if (
        best_id
        and best_score >= threshold
        and automatic_merge_is_safe(best_content, content)
    ):
        return best_id, best_content, merge_memory_text(best_content, content)
    return "", "", ""


def _expected_scope_id_for_mode(provider: Any, mode: str) -> str:
    view = _command_scope_view(provider)
    normalized = str(mode or "").strip().lower().replace("-", "_")
    if normalized == "shared_pool":
        return str(view.get("shared_pool_scope_id") or "")
    if normalized == "shared":
        return str(view.get("shared_scope_id") or "")
    return str(view.get("scope_id") or "")


def _row_scope_mode(provider: Any, row: Any) -> str:
    view = _command_scope_view(provider)
    scope_id = str(row["scope_id"])
    if scope_id and scope_id == str(view.get("shared_pool_scope_id") or ""):
        return "shared_pool"
    return "shared" if scope_id == str(view.get("shared_scope_id") or "") else "local"


def _target_scope_mode_for_existing(provider: Any, row: Any, target: str) -> str:
    existing_mode = _row_scope_mode(provider, row)
    default_mode = recall_scope_mode(target, str(row["source"]))
    if existing_mode == "shared_pool" and default_mode == "shared":
        return "shared_pool"
    return default_mode


def update_memory(
    provider: Any, memory_id: str, content: str, target: str | None = None
) -> tuple[bool, str, str]:
    """Atomically update truth, SQLite companions, and durable vector intent.

    The write reservation is acquired before ownership/lifecycle reads. Conflict
    metadata, extracted relations, FTS/entities (via ``update_row``), and vector
    outbox intent share the same commit boundary. External vector I/O is replayed
    only after truth commits.
    """

    mutation = MemoryMutationService(provider)
    updated = False
    summary = ""
    updated_at = ""
    try:
        with mutation.transaction() as conn:
            placeholders = _domain_writable_placeholders(provider)
            scope_params = _domain_writable_scope_ids(provider)
            existing = conn.execute(
                f"SELECT source, target, scope_id, metadata FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
                [memory_id, *scope_params],
            ).fetchone()
            if existing is None:
                MemoryMutationService.abort(conn)
                return False, "", ""
            mutation_error = _fact_mutation_error_payload(
                conn,
                [memory_id],
                operation="legacy memory update",
            )
            if mutation_error is not None:
                MemoryMutationService.abort(conn)
                return False, str(mutation_error["error"]), ""
            lifecycle = (
                str(load_metadata(existing["metadata"]).get("lifecycle") or "")
                .strip()
                .lower()
            )
            non_editable_lifecycles = PROFILE_HIDDEN_LIFECYCLES - {"scratch"}
            if lifecycle in non_editable_lifecycles:
                MemoryMutationService.abort(conn)
                return (
                    False,
                    f"memory lifecycle '{lifecycle}' requires explicit restore or review",
                    "",
                )
            new_target = target or str(existing["target"])
            new_mode = _target_scope_mode_for_existing(provider, existing, new_target)
            if str(existing["scope_id"]) != _expected_scope_id_for_mode(
                provider, new_mode
            ):
                MemoryMutationService.abort(conn)
                return (
                    False,
                    "target changes between shared durable and local scratch scopes are not allowed",
                    "",
                )
            updated, summary, updated_at = update_row(
                conn,
                memory_id=memory_id,
                content=content,
                target=target,
                scope_ids=scope_params,
                enqueue_vector_intent=False,
            )
            if not updated:
                MemoryMutationService.abort(conn)
                return False, summary, updated_at
            row = conn.execute(
                f"SELECT source, target, content, summary, updated_at, scope_id FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
                [memory_id, *scope_params],
            ).fetchone()
            if row is None:
                raise RuntimeError("updated memory disappeared before transaction commit")
            _mark_conflicts_for_memory(
                provider,
                memory_id=memory_id,
                content=str(row["content"]),
                target=str(row["target"]),
                rebuild_existing=True,
                commit=False,
            )
            if bool(_command_config(provider).get("relation_extraction_enabled", True)):
                sync_extracted_relations_for_memory(
                    conn,
                    memory_id=memory_id,
                    scope_ids=[str(row["scope_id"])],
                    batch_id="update",
                    max_pairs=_relation_pair_budget(provider),
                    local_peer_limit=_relation_local_neighbor_limit(provider),
                    commit=False,
                )
            enqueue_current_vector_event(
                conn,
                memory_id=memory_id,
                operation="upsert",
                updated_at=str(row["updated_at"]),
                reason="atomic memory update",
            )
    except Exception as exc:
        safe_error = sanitize_report_text(str(exc))
        logger.warning("Scope Recall atomic update rolled back: %s", safe_error)
        return False, safe_error, ""

    if updated:
        try:
            replay_vector_outbox(provider)
        except Exception as exc:
            mark_vector_replay_degraded(provider, exc)
            logger.warning(
                "Scope Recall vector outbox replay failed after atomic update: %s",
                exc,
            )
    return updated, summary, updated_at


def merge_memories(
    provider: Any,
    target_id: str,
    source_ids: list[str],
    content: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Atomically merge sources into a target under one mutation transaction."""

    source_ids = list(
        dict.fromkeys(
            str(memory_id) for memory_id in source_ids if str(memory_id).strip()
        )
    )
    mutation = MemoryMutationService(provider)
    summary = ""
    updated_at = ""
    requested_target = ""
    requested_mode = ""
    delete_ids: list[str] = []
    try:
        with mutation.transaction() as conn:
            placeholders = _domain_writable_placeholders(provider)
            scope_params = _domain_writable_scope_ids(provider)
            target_row = conn.execute(
                f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
                [target_id, *scope_params],
            ).fetchone()
            source_rows: list[Any] = []
            for id_chunk in chunked_sql_parameters(
                conn,
                source_ids,
                reserved=len(scope_params),
            ):
                source_rows.extend(
                    conn.execute(
                        f"SELECT * FROM memories "
                        f"WHERE id IN ({','.join('?' for _ in id_chunk)}) "
                        f"AND scope_id IN ({placeholders})",
                        [*id_chunk, *scope_params],
                    ).fetchall()
                )
            source_rows_by_id = {str(row["id"]): row for row in source_rows}
            source_rows = [
                source_rows_by_id[memory_id]
                for memory_id in source_ids
                if memory_id in source_rows_by_id
            ]
            if target_row is None:
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "error": "target_id not found",
                    "target_id": target_id,
                    "deleted": 0,
                    "target": "",
                    "scope_mode": "",
                }
            found_source_ids = {str(row["id"]) for row in source_rows}
            missing_source_ids = [
                memory_id for memory_id in source_ids if memory_id not in found_source_ids
            ]
            if missing_source_ids:
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "error": "source_id not found or not accessible",
                    "target_id": target_id,
                    "missing_source_ids": missing_source_ids,
                    "deleted": 0,
                }
            mutation_error = _fact_mutation_error_payload(
                conn,
                [target_id, *found_source_ids],
                operation="legacy memory merge",
            )
            if mutation_error is not None:
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "target_id": target_id,
                    "deleted": 0,
                    **mutation_error,
                }
            if not source_rows and content is None:
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "error": "source_ids or content is required",
                    "target_id": target_id,
                    "deleted": 0,
                    "target": str(target_row["target"]),
                    "scope_mode": _row_scope_mode(provider, target_row),
                }
            target_scope_id = str(target_row["scope_id"])
            if any(str(row["scope_id"]) != target_scope_id for row in source_rows):
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "error": "merge cannot combine shared durable and local scratch scopes",
                    "target_id": target_id,
                    "deleted": 0,
                }
            requested_target = target or str(target_row["target"])
            requested_mode = _target_scope_mode_for_existing(
                provider, target_row, requested_target
            )
            if target_scope_id != _expected_scope_id_for_mode(
                provider, requested_mode
            ):
                MemoryMutationService.abort(conn)
                return {
                    "merged": False,
                    "error": "target changes between shared durable and local scratch scopes are not allowed",
                    "target_id": target_id,
                    "deleted": 0,
                }
            if content is None:
                merged = str(target_row["content"])
                for row in source_rows:
                    if str(row["id"]) != target_id:
                        merged = merge_memory_text(merged, str(row["content"]))
            else:
                merged = _command_clean_text(provider, content)
            updated, summary, updated_at = update_row(
                conn,
                memory_id=target_id,
                content=merged,
                target=requested_target,
                scope_ids=scope_params,
                enqueue_vector_intent=False,
            )
            if not updated:
                raise RuntimeError("target update failed inside merge transaction")
            updated_row = conn.execute(
                "SELECT source, target, content, summary, updated_at, scope_id "
                "FROM memories WHERE id = ?",
                (target_id,),
            ).fetchone()
            if updated_row is None:
                raise RuntimeError("merge target disappeared before commit")
            _mark_conflicts_for_memory(
                provider,
                memory_id=target_id,
                content=str(updated_row["content"]),
                target=str(updated_row["target"]),
                rebuild_existing=True,
                commit=False,
            )
            if bool(_command_config(provider).get("relation_extraction_enabled", True)):
                sync_extracted_relations_for_memory(
                    conn,
                    memory_id=target_id,
                    scope_ids=[target_scope_id],
                    batch_id="merge",
                    max_pairs=_relation_pair_budget(provider),
                    local_peer_limit=_relation_local_neighbor_limit(provider),
                    commit=False,
                )
            enqueue_current_vector_event(
                conn,
                memory_id=target_id,
                operation="upsert",
                updated_at=str(updated_row["updated_at"]),
                reason="atomic memory merge target update",
            )
            delete_ids = [
                str(row["id"])
                for row in source_rows
                if str(row["id"]) != target_id
            ]
            delete_result = delete_memories_result(
                provider,
                delete_ids,
                transaction_conn=conn,
            )
            if delete_result.deleted_count != len(delete_ids):
                raise RuntimeError(
                    "merge source delete row-count mismatch: "
                    f"expected={len(delete_ids)}, "
                    f"deleted={delete_result.deleted_count}"
                )
    except Exception:
        # The transaction context has already rolled back every companion write.
        # Preserve the established merge API: callers receive the original fault.
        raise

    try:
        replay_vector_outbox(provider)
    except Exception as exc:
        mark_vector_replay_degraded(provider, exc)
        logger.warning(
            "Scope Recall vector outbox replay failed after atomic merge: %s",
            exc,
        )
    return {
        "merged": True,
        "target_id": target_id,
        "id": target_id,
        "target": requested_target,
        "scope_mode": requested_mode,
        "source_ids": delete_ids,
        "deleted": len(delete_ids),
        "summary": summary,
        "updated_at": updated_at,
    }



def govern_memories(
    provider: Any, *, dry_run: bool = True, scope_only: bool = True
) -> dict[str, Any]:
    """Build governance action plans for active memories.

    The function keeps classification, review surfaces, and optional apply behavior together so operator tools can expose the exact proposed mutation set."""
    conn = _command_conn(provider)
    if scope_only:
        where = f"WHERE scope_id IN ({_domain_writable_placeholders(provider)})"
        params: tuple[Any, ...] = tuple(_domain_writable_scope_ids(provider))
    else:
        where = ""
        params = ()
    with _command_lock(provider):
        rows = conn.execute(
            f"SELECT id, source, target, content, updated_at, metadata FROM memories {where}",
            params,
        ).fetchall()

    now = datetime.now(timezone.utc)
    tiers = {"core": 0, "working": 0, "archive": 0}
    decay_candidates: list[str] = []
    review_candidates: list[dict[str, Any]] = []
    updates: list[tuple[str, str, str, str]] = []
    for row in rows:
        metadata: dict[str, Any] = {}
        try:
            metadata.update(json.loads(str(row["metadata"] or "{}")))
        except Exception:
            pass
        classified = classify_memory(
            str(row["content"]), str(row["target"]), str(row["source"] or "")
        )
        classified.update(metadata)
        tier = str(classified.get("tier") or "working")
        try:
            updated_at = datetime.fromisoformat(
                str(row["updated_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except Exception:
            updated_at = now
        age_days = (now - updated_at).days
        if tier == "working" and age_days >= int(
            _command_config_value(provider,"archive_after_days", 365)
        ):
            tier = "archive"
            decay_candidates.append(str(row["id"]))
            classified["tier"] = "archive"
        reasons: list[str] = []
        lifecycle = str(classified.get("lifecycle") or "").strip().lower()
        if lifecycle in {"superseded", "obsolete", "rejected"}:
            reasons.append(f"lifecycle:{lifecycle}")
        if bool(classified.get("needs_conflict_review")):
            reasons.append("conflict-review")
        if str(row["target"] or "").strip().lower() == "general":
            reasons.append("local-scratch")
        source = str(row["source"] or "").strip().lower()
        if source in {"turn-user", "turn-assistant"}:
            reasons.append(f"raw-source:{source}")
        if tier == "archive":
            reasons.append("archive-candidate")
        try:
            confidence = float(classified.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence and confidence < 0.45:
            reasons.append("low-confidence")
        if reasons:
            review_candidates.append(
                {
                    "id": str(row["id"]),
                    "target": str(row["target"]),
                    "source": str(row["source"]),
                    "tier": tier,
                    "lifecycle": lifecycle or str(classified.get("lifecycle") or ""),
                    "reasons": sorted(set(reasons)),
                    "updated_at": str(row["updated_at"]),
                    "summary": compact_text(str(row["content"]), 160),
                }
            )
        tiers[tier] = tiers.get(tier, 0) + 1
        updates.append(
            (
                json.dumps(classified, ensure_ascii=False, sort_keys=True),
                str(row["id"]),
                str(row["updated_at"] or ""),
                str(metadata.get("lifecycle") or "active").strip().lower(),
            )
        )
    if not dry_run:
        write_conn = _command_conn(provider)
        if write_conn.in_transaction:
            write_conn.rollback()
        with MemoryMutationService(provider).transaction() as write_conn:
            for (
                metadata_json,
                memory_id,
                expected_updated_at,
                expected_lifecycle,
            ) in updates:
                metadata = load_metadata(metadata_json)
                transition_memory_lifecycle(
                    write_conn,
                    memory_id=memory_id,
                    lifecycle=str(metadata.get("lifecycle") or "active"),
                    metadata_updates=metadata,
                    expected_updated_at=expected_updated_at,
                    expected_lifecycle=expected_lifecycle,
                    actor="scope-recall-governance",
                    reason="governance metadata classification",
                    operation_id=GOVERNANCE_CLASSIFY_METADATA,
                )
    review_candidates = sorted(
        review_candidates,
        key=lambda item: (item["updated_at"], item["id"]),
        reverse=True,
    )
    return {
        "dry_run": dry_run,
        "scope_only": scope_only,
        "total": len(rows),
        "tiers": tiers,
        "decay_candidates": decay_candidates,
        "review_candidate_count": len(review_candidates),
        "review_candidates": review_candidates[:50],
    }


def _delete_memories_result(
    requested_ids: list[str],
    result: Mapping[str, Any],
) -> DeleteMemoriesResult:
    deleted_ids = tuple(
        dict.fromkeys(
            str(memory_id).strip()
            for memory_id in (result.get("ids") or [])
            if str(memory_id).strip()
        )
    )
    deleted_id_set = set(deleted_ids)
    normalized_requested = tuple(dict.fromkeys(requested_ids))
    unexpected_ids = deleted_id_set - set(normalized_requested)
    if unexpected_ids:
        raise RuntimeError("hard delete result included an unrequested identity")
    skipped_ids = tuple(
        memory_id
        for memory_id in normalized_requested
        if memory_id not in deleted_id_set
    )
    deleted_count = int(result.get("deleted") or 0)
    if deleted_count != len(deleted_ids):
        raise RuntimeError(
            "hard delete result count/identity mismatch: "
            f"deleted={deleted_count}, ids={len(deleted_ids)}"
        )
    vector_pending = bool(result.get("vector_pending"))
    companion_erasure_pending = bool(
        vector_pending or result.get("companion_erasure_pending")
    )
    return DeleteMemoriesResult(
        requested_ids=normalized_requested,
        deleted_ids=deleted_ids,
        skipped_ids=skipped_ids,
        deleted_count=deleted_count,
        vector_pending=vector_pending,
        companion_erasure_pending=companion_erasure_pending,
        data_retained=bool(skipped_ids or companion_erasure_pending),
        mutation_applied=deleted_count > 0,
    )


def delete_memories_result(
    provider: Any,
    ids: list[str],
    *,
    transaction_conn: Any | None = None,
) -> DeleteMemoriesResult:
    requested_ids = list(
        dict.fromkeys(
            str(memory_id).strip()
            for memory_id in ids
            if str(memory_id).strip()
        )
    )
    if not requested_ids:
        return _delete_memories_result([], {"deleted": 0, "ids": []})
    if transaction_conn is not None:
        require_vector_delete = vector_delete_intent_required(provider)
        result = hard_delete_memories(
            transaction_conn,
            memory_ids=requested_ids,
            scope_ids=_domain_writable_scope_ids(provider),
            vector_delete=None,
            require_vector_delete=require_vector_delete,
            actor="scope-recall-memory-ops",
            reason="atomic memory merge source delete",
            operation_id=HARD_DELETE_MERGE_SOURCE,
            batch_id=f"merge_delete_{uuid.uuid4().hex}",
            commit=False,
        )
        return _delete_memories_result(requested_ids, result)
    result = _hard_delete_provider_memories(
        provider,
        requested_ids,
        scope_ids=_domain_writable_scope_ids(provider),
        reason="explicit memory hard delete",
        operation_id=HARD_DELETE_EXPLICIT,
    )
    return _delete_memories_result(requested_ids, result)


def delete_memories(
    provider: Any,
    ids: list[str],
    *,
    transaction_conn: Any | None = None,
) -> int:
    """Legacy count-only wrapper retained through the 2.0.x window."""

    return delete_memories_result(
        provider,
        ids,
        transaction_conn=transaction_conn,
    ).deleted_count


def _hard_delete_provider_memories(
    provider: Any,
    ids: list[str],
    *,
    scope_ids: list[str] | None,
    reason: str,
    operation_id: str,
) -> dict[str, Any]:
    """Commit hard-delete truth plus outbox and expose companion status."""

    with capture_mutation_barrier(provider):
        return _hard_delete_provider_memories_after_capture_flush(
            provider,
            ids,
            scope_ids=scope_ids,
            reason=reason,
            operation_id=operation_id,
        )


def _hard_delete_provider_memories_after_capture_flush(
    provider: Any,
    ids: list[str],
    *,
    scope_ids: list[str] | None,
    reason: str,
    operation_id: str,
) -> dict[str, Any]:
    """Perform hard delete while the caller excludes new capture enqueue."""

    with _command_lock(provider):
        require_vector_delete = vector_delete_intent_required(provider)

        result = hard_delete_memories(
            _command_conn(provider),
            memory_ids=ids,
            scope_ids=scope_ids,
            vector_delete=None,
            require_vector_delete=require_vector_delete,
            actor="scope-recall-memory-ops",
            reason=reason,
            operation_id=operation_id,
            batch_id=f"hard_delete_{uuid.uuid4().hex}",
        )
    replay_result = (
        replay_vector_outbox(provider)
        if require_vector_delete
        else {"claimed": 0, "completed": 0, "failed": 0}
    )
    result["vector_replay"] = replay_result
    if int(replay_result.get("failed") or 0) > 0:
        result["vector_status"] = "pending"
        result["vector_pending"] = True
        result["vector_error"] = sanitize_report_text(
            str(_command_vector_status(provider).get("message") or "vector delete replay failed")
        )
    elif int(replay_result.get("completed") or 0) >= int(result.get("outbox_enqueued") or 0):
        result["vector_status"] = "completed" if require_vector_delete else "not_required"
        result["vector_pending"] = False
    else:
        result["vector_status"] = "pending"
        result["vector_pending"] = True
    requested_count = len(set(str(memory_id) for memory_id in ids if str(memory_id)))
    result.update(
        retention_response_contract(
            mode="hard_delete",
            data_retained=int(result.get("deleted") or 0) < requested_count,
            mutation_applied=int(result.get("deleted") or 0) > 0,
            companion_erasure_pending=bool(result.get("vector_pending")),
        )
    )
    return result


def _forget_snapshot(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": metadata,
    }


def _archive_memories_truth(
    provider: Any,
    ids: list[str],
    *,
    reason: str = "scope_recall_forget",
    actor: str = "scope_recall_forget",
    batch_id: str = "",
) -> dict[str, Any]:
    """Commit soft-archive truth, governance audit, and vector intent.

    Archiving preserves SQLite truth and rollback metadata while removing rows from ordinary recall surfaces."""
    requested_ids = list(
        dict.fromkeys(
            str(memory_id) for memory_id in ids if str(memory_id).strip()
        )
    )
    batch = batch_id or f"scope_recall_forget_{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "archived": 0,
        "deleted": 0,
        "ids": [],
        "skipped": requested_ids,
        "outbox_enqueued": 0,
        "vector_outbox_keys": [],
        "batch_id": batch,
        "receipt": {
            "action": "soft_archive",
            "batch_id": batch,
            "restore_path": f"python3 scripts/governance.cleanup.py --rollback-batch --batch-id {batch} --apply",
        },
        **retention_response_contract(
            mode="archive",
            data_retained=True,
            mutation_applied=False,
        ),
    }
    if not requested_ids:
        return payload
    now = datetime.now(timezone.utc).isoformat()
    try:
        with MemoryMutationService(provider).transaction() as conn:
            scope_params = _domain_writable_scope_ids(provider)
            rows: list[Any] = []
            for id_chunk in chunked_sql_parameters(
                conn,
                requested_ids,
                reserved=len(scope_params),
            ):
                placeholders = ",".join("?" for _ in id_chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
                        FROM memories
                        WHERE id IN ({placeholders})
                          AND scope_id IN ({_domain_writable_placeholders(provider)})
                        """,
                        [*id_chunk, *scope_params],
                    ).fetchall()
                )
            rows_by_id = {str(row["id"]): row for row in rows}
            rows = [
                rows_by_id[memory_id]
                for memory_id in requested_ids
                if memory_id in rows_by_id
            ]
            scoped_ids = [str(row["id"]) for row in rows]
            scoped_id_set = set(scoped_ids)
            payload["skipped"] = [
                memory_id for memory_id in requested_ids if memory_id not in scoped_id_set
            ]
            if not scoped_ids:
                MemoryMutationService.abort(conn)
                return payload
            mutation_error = _fact_mutation_error_payload(
                conn,
                scoped_ids,
                operation="legacy memory archive",
            )
            if mutation_error is not None:
                payload.update(mutation_error)
                payload["blocked_fact_ids"] = sorted(mutation_error["blocked_fact_ids"])
                payload["skipped"] = scoped_ids
                payload["receipt"].update(
                    {
                        "action": "soft_archive_blocked",
                        "reason": "fact mutation requires structured Fact Evolution/review",
                    }
                )
                MemoryMutationService.abort(conn)
                return payload
            archived_ids: list[str] = []
            vector_outbox_keys: list[str] = []
            for row in rows:
                memory_id = str(row["id"])
                metadata = load_metadata(row["metadata"])
                if str(metadata.get("lifecycle") or "").strip().lower() == "archived":
                    continue
                transition = transition_memory_lifecycle(
                    conn,
                    memory_id=memory_id,
                    lifecycle="archived",
                    metadata_updates={
                        "archived_at": now,
                        "archived_reason": sanitize_report_text(
                            reason or "scope_recall_forget"
                        ),
                        "archived_by": sanitize_report_text(
                            actor or "scope_recall_forget"
                        ),
                        "archived_batch_id": batch,
                    },
                    expected_updated_at=str(row["updated_at"] or ""),
                    actor=actor or "scope_recall_forget",
                    reason=reason or "scope_recall_forget",
                    operation_id=SCOPE_FORGET_ARCHIVE,
                    batch_id=batch,
                    timestamp=now,
                )
                archived_ids.append(memory_id)
                vector_outbox_key = str(
                    transition.get("vector_outbox_key") or ""
                ).strip()
                if transition.get("outbox_enqueued") and vector_outbox_key:
                    vector_outbox_keys.append(vector_outbox_key)
    except Exception as exc:
        safe_error = sanitize_report_text(str(exc))
        payload["archived"] = 0
        payload["ids"] = []
        payload["skipped"] = requested_ids
        payload["error"] = safe_error
        payload["receipt"].update(
            {
                "action": "soft_archive_failed",
                "ids": [],
                "reason": sanitize_report_text(reason or "scope_recall_forget"),
            }
        )
        return payload
    payload["archived"] = len(archived_ids)
    payload["mutation_applied"] = bool(archived_ids)
    payload["ids"] = archived_ids
    payload["vector_outbox_keys"] = vector_outbox_keys
    payload["outbox_enqueued"] = len(vector_outbox_keys)
    archived_id_set = set(archived_ids)
    payload["skipped"] = [
        memory_id for memory_id in requested_ids if memory_id not in archived_id_set
    ]
    payload["receipt"].update(
        {
            "ids": archived_ids,
            "reason": sanitize_report_text(reason or "scope_recall_forget"),
        }
    )
    return payload


def archive_memories(
    provider: Any,
    ids: list[str],
    *,
    reason: str = "scope_recall_forget",
    actor: str = "scope_recall_forget",
    batch_id: str = "",
) -> dict[str, Any]:
    """Soft-archive memories, then replay companion intent outside truth lock."""

    with capture_mutation_barrier(provider):
        return _archive_memories_after_capture_flush(
            provider,
            ids,
            reason=reason,
            actor=actor,
            batch_id=batch_id,
        )


def _archive_memories_after_capture_flush(
    provider: Any,
    ids: list[str],
    *,
    reason: str,
    actor: str,
    batch_id: str,
) -> dict[str, Any]:
    """Perform archive and vector replay under the capture mutation barrier."""

    payload = _archive_memories_truth(
        provider,
        ids,
        reason=reason,
        actor=actor,
        batch_id=batch_id,
    )
    if not payload.get("ids"):
        return payload
    event_keys = [
        str(event_key)
        for event_key in payload.get("vector_outbox_keys", [])
        if str(event_key).strip()
    ]
    if not event_keys:
        payload["vector_replay"] = {"claimed": 0, "completed": 0, "failed": 0}
        payload["vector_status"] = "not_required"
        payload["vector_pending"] = False
        payload.update(
            retention_response_contract(
                mode="archive",
                data_retained=True,
                mutation_applied=bool(payload.get("ids")),
                companion_erasure_pending=False,
            )
        )
        return payload
    try:
        before_rows = _archive_vector_outbox_rows(provider, event_keys)
        event_ids = [int(row["id"]) for row in before_rows if int(row["id"]) > 0]
        replay_result = replay_vector_outbox_events(provider, event_ids=event_ids)
        payload["vector_replay"] = replay_result
        final_rows = _archive_vector_outbox_rows(provider, event_keys)
        status_counts: dict[str, int] = {}
        for row in final_rows:
            status = str(row["status"] or "missing")
            status_counts[status] = status_counts.get(status, 0) + 1
        payload["vector_outbox_status_counts"] = status_counts
        payload["vector_outbox_event_ids"] = [
            int(row["id"]) for row in final_rows if int(row["id"]) > 0
        ]
        vector_pending = any(
            str(row["status"] or "missing") != "completed" for row in final_rows
        )
        if status_counts.get("dead_letter", 0):
            mark_vector_needs_repair(
                provider,
                "archive vector delete intent reached dead-letter status",
                reason_code="archive_outbox_dead_letter",
            )
    except Exception as exc:
        safe_error = sanitize_report_text(str(exc))
        mark_vector_replay_degraded(provider, safe_error)
        payload["vector_replay"] = {
            "completed": 0,
            "failed": 1,
            "pending": True,
            "error": safe_error,
        }
        vector_pending = True
        payload["vector_error"] = safe_error
    payload["vector_pending"] = vector_pending
    payload["vector_status"] = "pending" if vector_pending else "completed"
    payload.update(
        retention_response_contract(
            mode="archive",
            data_retained=True,
            mutation_applied=bool(payload.get("ids")),
            companion_erasure_pending=vector_pending,
        )
    )
    return payload


def _archive_vector_outbox_rows(
    provider: Any,
    event_keys: list[str],
) -> list[dict[str, Any]]:
    """Read the exact durable vector intents created by one archive command."""

    resolved_keys = list(
        dict.fromkeys(str(event_key).strip() for event_key in event_keys if str(event_key).strip())
    )
    if not resolved_keys:
        return []
    rows: list[Any] = []
    with _command_lock(provider):
        conn = _command_conn(provider)
        for key_chunk in chunked_sql_parameters(conn, resolved_keys):
            placeholders = ",".join("?" for _ in key_chunk)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id, event_key, generation_id, memory_id, operation, status
                    FROM vector_outbox
                    WHERE event_key IN ({placeholders})
                    """,
                    key_chunk,
                ).fetchall()
            )
    by_key = {
        str(row["event_key"]): {
            "id": int(row["id"]),
            "event_key": str(row["event_key"]),
            "generation_id": str(row["generation_id"]),
            "memory_id": str(row["memory_id"]),
            "operation": str(row["operation"]),
            "status": str(row["status"]),
        }
        for row in rows
    }
    return [
        by_key.get(
            event_key,
            {
                "id": 0,
                "event_key": event_key,
                "generation_id": "",
                "memory_id": "",
                "operation": "",
                "status": "missing",
            },
        )
        for event_key in resolved_keys
    ]


def dedupe_memories(provider: Any, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
    with _command_lock(provider):
        groups = exact_duplicate_groups(
            _command_conn(provider),
            scope_ids=_domain_writable_scope_ids(provider) if scope_only else None,
        )
        delete_ids = [
            memory_id for group in groups for memory_id in group["delete_ids"]
        ]
        payload: dict[str, Any] = {
            "dry_run": dry_run,
            "scope_only": scope_only,
            "duplicate_groups": len(groups),
            "duplicates": len(delete_ids),
            "groups": groups[:20],
        }
        if dry_run:
            payload["deleted"] = 0
            return payload
    result = _hard_delete_provider_memories(
        provider,
        delete_ids,
        scope_ids=_domain_writable_scope_ids(provider) if scope_only else None,
        reason="exact duplicate cleanup",
        operation_id=HARD_DELETE_DEDUPE,
    )
    payload.update(
        {
            "ok": bool(result.get("ok")),
            "deleted": int(result.get("deleted") or 0),
            "vector_status": str(result.get("vector_status") or ""),
            "vector_pending": bool(result.get("vector_pending")),
            "vector_error": str(result.get("vector_error") or ""),
        }
    )
    return payload


def repair_vector(provider: Any) -> dict[str, Any]:
    target = provider
    setup_vector_layer(target)
    return {
        "repaired": _command_vector_status(provider).get("status") == "ready",
        "vector": stats_payload(target)["vector"],
    }




def feedback_memory(
    provider: Any, *, memory_id: str, rating: str, note: str = ""
) -> dict[str, Any]:
    rating_text = str(rating or "").strip().lower()
    if rating_text in {"helpful", "up", "+1", "1", "true", "yes"}:
        rating_value = 1
    elif rating_text in {"unhelpful", "down", "-1", "0", "false", "no"}:
        rating_value = -1
    else:
        return {
            "updated": False,
            "error": "rating must be helpful or unhelpful",
            "id": memory_id,
        }

    with MemoryMutationService(provider).transaction() as conn:
        row = conn.execute(
            f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({_domain_writable_placeholders(provider)})",
            [memory_id, *_domain_writable_scope_ids(provider)],
        ).fetchone()
        if row is None:
            MemoryMutationService.abort(conn)
            return {"updated": False, "error": "id not found", "id": memory_id}
        metadata = load_metadata(row["metadata"])
        feedback_count = int(metadata.get("feedback_count") or 0) + 1
        helpful_count = int(metadata.get("helpful_count") or 0)
        unhelpful_count = int(metadata.get("unhelpful_count") or 0)
        if rating_value > 0:
            helpful_count += 1
        else:
            unhelpful_count += 1
        old_trust = clamp_float(metadata.get("trust"), default=0.5)
        metadata["trust"] = clamp_float(
            old_trust + (0.08 if rating_value > 0 else -0.12), default=old_trust
        )
        metadata["feedback_count"] = feedback_count
        metadata["helpful_count"] = helpful_count
        metadata["unhelpful_count"] = unhelpful_count
        safe_metadata, _ = sanitize_structured_value(metadata)
        metadata = safe_metadata if isinstance(safe_metadata, dict) else {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        safe_note = sanitize_report_text(note)[:240]
        conn.execute(
            "INSERT INTO memory_feedback(memory_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
            (
                memory_id,
                rating_value,
                safe_note,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            f"UPDATE memories SET metadata = ? WHERE id = ? AND scope_id IN ({_domain_writable_placeholders(provider)})",
            (metadata_json, memory_id, *_domain_writable_scope_ids(provider)),
        )
    return {
        "updated": True,
        "id": memory_id,
        "rating": "helpful" if rating_value > 0 else "unhelpful",
        "trust": metadata["trust"],
        "feedback_count": feedback_count,
    }
