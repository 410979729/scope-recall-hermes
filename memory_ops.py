from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .capture import store_now
from .capture_filters import sanitize_report_text
from .gating import compact_text
from .graph import clamp_float, compact_context_lines, load_metadata, normalize_entity
from .governance import classify_memory, is_conflicting, merge_memory_text, semantic_similarity
from .models import recall_scope_mode
from .sql_store import curated_recall_item_id, delete_rows, exact_duplicate_groups, iter_curated_entries, update_row
from .storage_views import _curated_memory_allowed
from .vector_runtime import mark_vector_needs_repair, refresh_vector_audit, setup_vector_layer, upsert_vector_record


def _scope_params(provider: Any, *, writable: bool = False) -> list[str]:
    attr = "_writable_scope_ids" if writable else "_accessible_scope_ids"
    scopes = getattr(provider, attr, []) or []
    return [str(scope_id) for scope_id in scopes if str(scope_id)]


def _scope_placeholders(provider: Any, *, writable: bool = False) -> str:
    params = _scope_params(provider, writable=writable)
    return ",".join("?" for _ in params) or "NULL"


def _accessible_scope_params(provider: Any) -> list[str]:
    return _scope_params(provider, writable=False)


def _writable_scope_params(provider: Any) -> list[str]:
    return _scope_params(provider, writable=True)


def _normalized_scope_mode(provider: Any, target: str, source: str = "", scope_mode: str | None = None) -> str:
    requested = str(scope_mode or "").strip().lower().replace("-", "_")
    if requested in {"shared", "local", "shared_pool"}:
        return requested
    return provider._scope_mode_for(target, source) if hasattr(provider, "_scope_mode_for") else recall_scope_mode(target, source)


def store_memory_now(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
    semantic_merge: bool = True,
    scope_mode: str | None = None,
) -> tuple[str, bool, str]:
    resolved_scope_mode = _normalized_scope_mode(provider, target, source, scope_mode)
    if semantic_merge and not allow_duplicate and target in {"user", "ops", "project"}:
        merge_id, merged_content = find_semantic_merge_candidate(provider, content, target, scope_mode=resolved_scope_mode)
        if merge_id:
            if merged_content.strip() == content.strip():
                return merge_id, False, "duplicate"
            update_memory(provider, merge_id, merged_content, target)
            return merge_id, False, "merged"
    memory_id, inserted = store_now(
        provider,
        content=content,
        source=source,
        target=target,
        session_id=session_id,
        metadata=metadata,
        allow_duplicate=allow_duplicate,
        scope_mode=resolved_scope_mode,
    )
    if inserted:
        _mark_conflicts_for_memory(provider, memory_id=memory_id, content=content, target=target)
    outcome = "stored" if inserted else "duplicate" if memory_id else "skipped"
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
    return sorted({str(row["peer_id"]) for row in rows if str(row["peer_id"]) and str(row["peer_id"]) != memory_id})


def _sync_conflict_metadata(conn: Any, memory_id: str) -> None:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return
    metadata_payload = load_metadata(row["metadata"] if row is not None else "{}")
    conflict_ids = _conflict_peer_ids(conn, memory_id)
    relation_types = metadata_payload.get("relation_types")
    if not isinstance(relation_types, list):
        relation_types = []
    relation_types = [str(item) for item in relation_types if str(item) and str(item) != "contradicts"]
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
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True), memory_id),
    )


def _sync_conflict_metadata_for_ids(conn: Any, memory_ids: set[str]) -> None:
    for related_id in sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)}):
        _sync_conflict_metadata(conn, related_id)


def _mark_conflicts_for_memory(provider: Any, *, memory_id: str, content: str, target: str, rebuild_existing: bool = False) -> int:
    """Record deterministic contradiction edges for a memory and keep conflict metadata current."""

    conn = provider._require_conn()
    now = datetime.now(timezone.utc).isoformat()
    with provider._lock:
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
            SELECT id, content
            FROM memories
            WHERE id != ? AND target = ? AND scope_id IN ({})
            ORDER BY updated_at DESC
            LIMIT 50
            """.format(_scope_placeholders(provider, writable=True)),
            [memory_id, target, *_writable_scope_params(provider)],
        ).fetchall()
        conflicting_ids = [str(row["id"]) for row in rows if is_conflicting(str(row["content"]), content)]
        for target_id in conflicting_ids:
            affected_ids.add(target_id)
            for source_id, related_id in ((memory_id, target_id), (target_id, memory_id)):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                    VALUES (?, ?, 'contradicts', ?, ?, ?)
                    """,
                    (source_id, related_id, 0.74, f"conflict-review: contradicts memory {related_id}", now),
                )
        if rebuild_existing or conflicting_ids:
            _sync_conflict_metadata_for_ids(conn, affected_ids)
            conn.commit()
    return len(conflicting_ids)


def find_semantic_merge_candidate(provider: Any, content: str, target: str, *, scope_mode: str | None = None) -> tuple[str, str]:
    threshold = float(provider._config_value("semantic_merge_threshold", 0.72))
    conn = provider._require_conn()
    resolved_scope_mode = _normalized_scope_mode(provider, target, "tool-store", scope_mode)
    scope_id = _expected_scope_id_for_mode(provider, resolved_scope_mode)
    if not scope_id or scope_id not in _writable_scope_params(provider):
        return "", ""
    with provider._lock:
        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE scope_id = ? AND target = ?
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            [scope_id, target],
        ).fetchall()
    best_id = ""
    best_content = ""
    best_score = 0.0
    for row in rows:
        existing = str(row["content"])
        if existing.strip().lower() == content.strip().lower():
            return str(row["id"]), existing
        if is_conflicting(existing, content):
            continue
        score = semantic_similarity(existing, content)
        if score > best_score:
            best_id = str(row["id"])
            best_content = existing
            best_score = score
    if best_id and best_score >= threshold:
        return best_id, merge_memory_text(best_content, content)
    return "", ""


def _expected_scope_id_for_mode(provider: Any, mode: str) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_")
    if normalized == "shared_pool":
        return str(getattr(provider, "_shared_pool_scope_id", "") or "")
    if normalized == "shared":
        return str(getattr(provider, "_shared_scope_id", "") or "")
    return str(getattr(provider, "_scope_id", "") or "")


def _row_scope_mode(provider: Any, row: Any) -> str:
    scope_id = str(row["scope_id"])
    if scope_id and scope_id == str(getattr(provider, "_shared_pool_scope_id", "") or ""):
        return "shared_pool"
    return "shared" if scope_id == provider._shared_scope_id else "local"


def _target_scope_mode_for_existing(provider: Any, row: Any, target: str) -> str:
    existing_mode = _row_scope_mode(provider, row)
    default_mode = recall_scope_mode(target, str(row["source"]))
    if existing_mode == "shared_pool" and default_mode == "shared":
        return "shared_pool"
    return default_mode


def update_memory(provider: Any, memory_id: str, content: str, target: str | None = None) -> tuple[bool, str, str]:
    with provider._lock:
        placeholders = _scope_placeholders(provider, writable=True)
        scope_params = _writable_scope_params(provider)
        existing = provider._require_conn().execute(
            f"SELECT source, target, scope_id FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
            [memory_id, *scope_params],
        ).fetchone()
        if existing is None:
            return False, "", ""
        new_target = target or str(existing["target"])
        new_mode = _target_scope_mode_for_existing(provider, existing, new_target)
        if str(existing["scope_id"]) != _expected_scope_id_for_mode(provider, new_mode):
            return False, "target changes between shared durable and local scratch scopes are not allowed", ""
        updated, summary, updated_at = update_row(
            provider._require_conn(),
            memory_id=memory_id,
            content=content,
            target=target,
            scope_ids=scope_params,
        )
    if updated:
        placeholders = _scope_placeholders(provider, writable=True)
        row = provider._require_conn().execute(
            f"SELECT source, target, content, summary, updated_at, scope_id FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
            [memory_id, *_writable_scope_params(provider)],
        ).fetchone()
        if row is not None:
            _mark_conflicts_for_memory(
                provider,
                memory_id=memory_id,
                content=str(row["content"]),
                target=str(row["target"]),
                rebuild_existing=True,
            )
            upsert_vector_record(
                provider,
                id=memory_id,
                source=str(row["source"]),
                target=str(row["target"]),
                content=str(row["content"]),
                summary=str(row["summary"]),
                updated_at=str(row["updated_at"]),
                scope_id=str(row["scope_id"]),
            )
    return updated, summary, updated_at


def merge_memories(provider: Any, target_id: str, source_ids: list[str], content: str | None = None, target: str | None = None) -> dict[str, Any]:
    source_ids = [str(memory_id) for memory_id in source_ids if str(memory_id).strip()]
    conn = provider._require_conn()
    with provider._lock:
        placeholders = _scope_placeholders(provider, writable=True)
        scope_params = _writable_scope_params(provider)
        target_row = conn.execute(f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({placeholders})", [target_id, *scope_params]).fetchone()
        source_rows = conn.execute(
            f"SELECT * FROM memories WHERE id IN ({','.join('?' for _ in source_ids)}) AND scope_id IN ({placeholders})" if source_ids else "SELECT * FROM memories WHERE 0",
            [*source_ids, *scope_params] if source_ids else [],
        ).fetchall()
    if target_row is None:
        return {"merged": False, "error": "target_id not found", "target_id": target_id, "deleted": 0, "target": "", "scope_mode": ""}
    found_source_ids = {str(row["id"]) for row in source_rows}
    missing_source_ids = [memory_id for memory_id in source_ids if memory_id not in found_source_ids]
    if missing_source_ids:
        return {
            "merged": False,
            "error": "source_id not found or not accessible",
            "target_id": target_id,
            "missing_source_ids": missing_source_ids,
            "deleted": 0,
        }
    if not source_rows and content is None:
        return {"merged": False, "error": "source_ids or content is required", "target_id": target_id, "deleted": 0, "target": str(target_row["target"]), "scope_mode": _row_scope_mode(provider, target_row)}
    target_scope_id = str(target_row["scope_id"])
    if any(str(row["scope_id"]) != target_scope_id for row in source_rows):
        return {
            "merged": False,
            "error": "merge cannot combine shared durable and local scratch scopes",
            "target_id": target_id,
            "deleted": 0,
        }
    requested_target = target or str(target_row["target"])
    requested_mode = _target_scope_mode_for_existing(provider, target_row, requested_target)
    if target_scope_id != _expected_scope_id_for_mode(provider, requested_mode):
        return {
            "merged": False,
            "error": "target changes between shared durable and local scratch scopes are not allowed",
            "target_id": target_id,
            "deleted": 0,
        }
    if content is None:
        merged = str(target_row["content"])
        for row in source_rows:
            merged = merge_memory_text(merged, str(row["content"]))
    else:
        merged = provider._clean_text(content)
    updated, summary, updated_at = update_memory(provider, target_id, merged, requested_target)
    if not updated:
        return {"merged": False, "error": "target update failed", "target_id": target_id, "deleted": 0}
    delete_ids = [str(row["id"]) for row in source_rows if str(row["id"]) != target_id]
    deleted = delete_memories(provider, delete_ids)
    return {
        "merged": True,
        "target_id": target_id,
        "id": target_id,
        "target": requested_target,
        "scope_mode": requested_mode,
        "source_ids": delete_ids,
        "deleted": deleted,
        "summary": summary,
        "updated_at": updated_at,
    }


def export_memories(provider: Any, *, fmt: str = "jsonl", scope_only: bool = True) -> dict[str, Any]:
    conn = provider._require_conn()
    if scope_only:
        where = f"WHERE scope_id IN ({_scope_placeholders(provider)})"
        params: tuple[Any, ...] = tuple(_accessible_scope_params(provider))
    else:
        where = ""
        params = ()
    with provider._lock:
        rows = conn.execute(
            f"""
            SELECT id, scope_id, source, target, content, summary, created_at, updated_at, metadata
            FROM memories
            {where}
            ORDER BY updated_at DESC, id DESC
            """,
            params,
        ).fetchall()
    records = [dict(row) for row in rows]
    if fmt.lower() == "json":
        data: Any = records
    else:
        fmt = "jsonl"
        data = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    return {"format": fmt.lower(), "scope_only": scope_only, "count": len(records), "data": data}


def govern_memories(provider: Any, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
    conn = provider._require_conn()
    if scope_only:
        where = f"WHERE scope_id IN ({_scope_placeholders(provider, writable=True)})"
        params: tuple[Any, ...] = tuple(_writable_scope_params(provider))
    else:
        where = ""
        params = ()
    with provider._lock:
        rows = conn.execute(
            f"SELECT id, source, target, content, updated_at, metadata FROM memories {where}",
            params,
        ).fetchall()

    now = datetime.now(timezone.utc)
    tiers = {"core": 0, "working": 0, "archive": 0}
    decay_candidates: list[str] = []
    review_candidates: list[dict[str, Any]] = []
    updates: list[tuple[str, str]] = []
    for row in rows:
        metadata: dict[str, Any] = {}
        try:
            metadata.update(json.loads(str(row["metadata"] or "{}")))
        except Exception:
            pass
        classified = classify_memory(str(row["content"]), str(row["target"]), str(row["source"] or ""))
        classified.update(metadata)
        tier = str(classified.get("tier") or "working")
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            updated_at = now
        age_days = (now - updated_at).days
        if tier == "working" and age_days >= int(provider._config_value("archive_after_days", 365)):
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
        updates.append((json.dumps(classified, ensure_ascii=False, sort_keys=True), str(row["id"])))
    if not dry_run:
        with provider._lock:
            if scope_only:
                placeholders = _scope_placeholders(provider, writable=True)
                conn.executemany(
                    f"UPDATE memories SET metadata = ? WHERE id = ? AND scope_id IN ({placeholders})",
                    [(*update, *_writable_scope_params(provider)) for update in updates],
                )
            else:
                conn.executemany("UPDATE memories SET metadata = ? WHERE id = ?", updates)
            conn.commit()
    review_candidates = sorted(review_candidates, key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    return {
        "dry_run": dry_run,
        "scope_only": scope_only,
        "total": len(rows),
        "tiers": tiers,
        "decay_candidates": decay_candidates,
        "review_candidate_count": len(review_candidates),
        "review_candidates": review_candidates[:50],
    }


def delete_memories(provider: Any, ids: list[str]) -> int:
    requested_ids = [str(memory_id) for memory_id in ids if str(memory_id).strip()]
    if not requested_ids:
        return 0
    placeholders = ",".join("?" for _ in requested_ids)
    with provider._lock:
        scoped_ids = [
            str(row["id"])
            for row in provider._require_conn()
            .execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders}) AND scope_id IN ({_scope_placeholders(provider, writable=True)})",
                [*requested_ids, *_writable_scope_params(provider)],
            )
            .fetchall()
        ]
        deleted_changes = delete_rows(provider._require_conn(), scoped_ids, scope_ids=_writable_scope_params(provider))
    if provider._vector_store and scoped_ids:
        try:
            provider._vector_store.delete_by_ids(scoped_ids)
        except Exception as exc:
            mark_vector_needs_repair(provider, exc)
    return deleted_changes


def dedupe_memories(provider: Any, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
    groups = exact_duplicate_groups(provider._require_conn(), scope_ids=_writable_scope_params(provider) if scope_only else None)
    delete_ids = [memory_id for group in groups for memory_id in group["delete_ids"]]
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
    if scope_only:
        payload["deleted"] = delete_memories(provider, delete_ids)
    else:
        with provider._lock:
            payload["deleted"] = delete_rows(provider._require_conn(), delete_ids)
        if provider._vector_store and delete_ids:
            try:
                provider._vector_store.delete_by_ids(delete_ids)
            except Exception as exc:
                mark_vector_needs_repair(provider, exc)
    return payload


def repair_vector(provider: Any) -> dict[str, Any]:
    setup_vector_layer(provider)
    return {"repaired": provider._vector_status == "ready", "vector": stats_payload(provider)["vector"]}


def hygiene_report(provider: Any, *, limit: int = 200) -> dict[str, Any]:
    from .hygiene import build_hygiene_report

    with provider._lock:
        return build_hygiene_report(provider._require_conn(), vector_store=provider._vector_store, limit=limit)


def _row_payload(row: Any) -> dict[str, Any]:
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "source": str(row["source"]),
        "target": str(row["target"]),
        "content": str(row["content"]),
        "summary": str(row["summary"]),
        "updated_at": str(row["updated_at"]),
        "memory_type": str(metadata.get("memory_type") or ""),
        "confidence": clamp_float(metadata.get("confidence"), default=0.5),
        "trust": clamp_float(metadata.get("trust"), default=0.5),
        "importance": clamp_float(metadata.get("importance"), default=0.5),
        "entities": metadata.get("entities") if isinstance(metadata.get("entities"), list) else [],
        "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
    }


def _profile_targets(targets: list[str] | None, *, include_general: bool) -> list[str]:
    allowed = ["user", "memory", "project", "ops", "general"]
    if targets:
        output = [target for target in targets if target in allowed]
    else:
        output = ["user", "memory", "project", "ops"]
    if include_general and "general" not in output:
        output.append("general")
    if not include_general and targets is None and "general" in output:
        output.remove("general")
    deduped: list[str] = []
    for target in output:
        if target not in deduped:
            deduped.append(target)
    return deduped


def _profile_row_payload(row: Any) -> dict[str, Any]:
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    return {
        "id": str(row["id"]),
        "target": str(row["target"]),
        "source": str(row["source"]),
        "summary": str(row["summary"]),
        "content": compact_text(str(row["content"]), 360),
        "updated_at": str(row["updated_at"]),
        "scope_mode": str(metadata.get("scope_mode") or recall_scope_mode(str(row["target"]), str(row["source"]))),
        "memory_type": str(metadata.get("memory_type") or metadata.get("category") or ""),
        "trust": clamp_float(metadata.get("trust"), default=0.5),
        "importance": clamp_float(metadata.get("importance"), default=0.5),
        "confidence": clamp_float(metadata.get("confidence"), default=0.5),
        "entities": metadata.get("entities") if isinstance(metadata.get("entities"), list) else [],
    }


def _profile_curated_items(provider: Any, *, targets: list[str], limit: int) -> list[dict[str, Any]]:
    if not _curated_memory_allowed(provider):
        return []
    items: list[dict[str, Any]] = []
    for target, content, updated_at in iter_curated_entries(provider._hermes_home):
        if target not in targets:
            continue
        metadata = classify_memory(content, target, "builtin-curated")
        items.append(
            {
                "id": curated_recall_item_id(target, content),
                "target": target,
                "source": "builtin-curated",
                "summary": compact_text(content, 220),
                "content": compact_text(content, 360),
                "updated_at": updated_at,
                "scope_mode": "curated-live",
                "memory_type": str(metadata.get("memory_type") or metadata.get("category") or ""),
                "trust": clamp_float(metadata.get("trust"), default=0.5),
                "importance": clamp_float(metadata.get("importance"), default=0.5),
                "confidence": clamp_float(metadata.get("confidence"), default=0.5),
                "entities": metadata.get("entities") if isinstance(metadata.get("entities"), list) else [],
            }
        )
    return items[: max(1, limit)]


def _profile_relevant_ids(provider: Any, *, query: str, entity: str, limit: int) -> set[str]:
    relevant: set[str] = set()
    if query:
        for item in provider._recall_service.search_memories(query, limit=max(10, min(50, limit * 4))):
            relevant.add(str(item.id))
    normalized_entity = normalize_entity(entity)
    if normalized_entity:
        with provider._lock:
            rows = provider._require_conn().execute(
                f"""
                SELECT m.id
                FROM memory_entities e
                JOIN memories m ON m.id = e.memory_id
                WHERE e.entity = ? AND m.scope_id IN ({_scope_placeholders(provider)})
                LIMIT ?
                """,
                [normalized_entity, *_accessible_scope_params(provider), max(10, min(100, limit * 8))],
            ).fetchall()
        relevant.update(str(row["id"]) for row in rows)
    return relevant


def _profile_rows_for_target(
    provider: Any,
    *,
    target: str,
    limit: int,
    relevant_ids: set[str],
    filter_to_relevance: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = [target, *_accessible_scope_params(provider)]
    relevance_clause = ""
    if filter_to_relevance:
        if not relevant_ids:
            return []
        relevance_clause = f" AND id IN ({','.join('?' for _ in relevant_ids)})"
        params.extend(sorted(relevant_ids))
    params.append(max(1, limit))
    with provider._lock:
        rows = provider._require_conn().execute(
            f"""
            SELECT *
            FROM memories
            WHERE target = ? AND scope_id IN ({_scope_placeholders(provider)}){relevance_clause}
            ORDER BY
                CASE source
                    WHEN 'tool-store' THEN 0
                    WHEN 'journal-digest' THEN 1
                    WHEN 'nightly-digest' THEN 2
                    ELSE 3
                END,
                updated_at DESC,
                id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_profile_row_payload(row) for row in rows]


def profile_payload(
    provider: Any,
    *,
    query: str = "",
    entity: str = "",
    targets: list[str] | None = None,
    include_general: bool = False,
    include_curated: bool = True,
    limit: int = 5,
    max_chars: int = 1200,
) -> dict[str, Any]:
    limit = max(1, min(20, int(limit or 5)))
    max_chars = max(120, min(4000, int(max_chars or 1200)))
    selected_targets = _profile_targets(targets, include_general=include_general)
    relevant_ids = _profile_relevant_ids(provider, query=query, entity=entity, limit=limit)
    relevance_requested = bool(query.strip() or entity.strip())

    sections: dict[str, dict[str, Any]] = {
        target: {"count": 0, "items": []}
        for target in ["user", "memory", "project", "ops", "general"]
    }
    all_items: list[dict[str, Any]] = []
    for target in selected_targets:
        filter_to_relevance = relevance_requested and target in {"project", "ops"}
        items = _profile_rows_for_target(
            provider,
            target=target,
            limit=limit,
            relevant_ids=relevant_ids,
            filter_to_relevance=filter_to_relevance,
        )
        sections[target] = {"count": len(items), "items": items}
        all_items.extend(items)

    curated_items: list[dict[str, Any]] = []
    if include_curated:
        curated_items = _profile_curated_items(provider, targets=selected_targets, limit=limit)
        for item in curated_items:
            target = str(item.get("target") or "memory")
            section_items = sections.setdefault(target, {"count": 0, "items": []})["items"]
            section_items.append(item)
            sections[target]["count"] = len(section_items)
        all_items = [*curated_items, *all_items]

    context = compact_context_lines(all_items, max_chars=max_chars)
    rendered_count = len([line for line in context.splitlines() if line.strip()])
    return {
        "provider": provider.name,
        "surface": "profile",
        "query": query,
        "entity": normalize_entity(entity),
        "targets": selected_targets,
        "include_general": bool(include_general or (targets is not None and "general" in selected_targets)),
        "context": context,
        "sections": sections,
        "curated": {"count": len(curated_items), "items": curated_items},
        "scope": {
            "scope_id": provider._scope_id,
            "shared_scope_id": provider._shared_scope_id,
            "accessible_scope_count": len(provider._accessible_scope_ids),
        },
        "budget": {
            "limit_per_section": limit,
            "max_chars": max_chars,
            "rendered_chars": len(context),
            "rendered_items": rendered_count,
            "candidate_items": len(all_items),
            "truncated": rendered_count < len(all_items),
        },
        "notes": [
            "SQLite memories are read from the current accessible scope set only.",
            "Hermes curated USER.md/MEMORY.md entries are live-read when policy allows; they are not copied into SQLite.",
            "Raw journal rows are not exposed by this profile surface.",
        ],
    }


def context_payload(provider: Any, *, query: str, limit: int = 5, max_chars: int = 900) -> dict[str, Any]:
    results = provider._recall_service.search_memories(query, limit=max(1, min(20, limit)))
    records: list[dict[str, Any]] = []
    entity_counts: dict[str, int] = {}
    for item in results:
        metadata = load_metadata(item.metadata or {})
        raw_entities = metadata.get("entities")
        entities = [str(entity) for entity in raw_entities] if isinstance(raw_entities, list) else []
        for entity in entities:
            entity_counts[entity] = entity_counts.get(entity, 0) + 1
        records.append(
            {
                "id": item.id,
                "target": item.target,
                "source": item.source,
                "content": item.content,
                "summary": item.summary,
                "score": round(item.score, 4),
                "updated_at": item.updated_at,
                "memory_type": str(metadata.get("memory_type") or ""),
                "entities": entities,
            }
        )
    top_entities = [
        {"entity": entity, "count": count}
        for entity, count in sorted(entity_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    ]
    return {
        "query": query,
        "count": len(records),
        "context": compact_context_lines(records, max_chars=max(120, min(4000, max_chars))),
        "entities": top_entities,
        "results": records,
    }


def probe_entity(provider: Any, *, entity: str, limit: int = 10) -> dict[str, Any]:
    normalized = normalize_entity(entity)
    if not normalized:
        return {"entity": "", "count": 0, "results": []}
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM memory_entities e
            JOIN memories m ON m.id = e.memory_id
            WHERE e.entity = ? AND m.scope_id IN ({_scope_placeholders(provider)})
            ORDER BY
                CASE m.target
                    WHEN 'user' THEN 0
                    WHEN 'project' THEN 1
                    WHEN 'ops' THEN 2
                    WHEN 'memory' THEN 3
                    ELSE 4
                END,
                m.updated_at DESC
            LIMIT ?
            """,
            [normalized, *_accessible_scope_params(provider), max(1, min(50, limit))],
        ).fetchall()
    return {"entity": normalized, "count": len(rows), "results": [_row_payload(row) for row in rows]}


def related_entities(provider: Any, *, entity: str, limit: int = 12) -> dict[str, Any]:
    normalized = normalize_entity(entity)
    if not normalized:
        return {"entity": "", "count": 0, "related": []}
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            f"""
            WITH matched AS (
                SELECT e.memory_id
                FROM memory_entities e
                JOIN memories m ON m.id = e.memory_id
                WHERE e.entity = ? AND m.scope_id IN ({_scope_placeholders(provider)})
            )
            SELECT e.entity, COUNT(*) AS count
            FROM memory_entities e
            JOIN matched ON matched.memory_id = e.memory_id
            WHERE e.entity != ?
            GROUP BY e.entity
            ORDER BY count DESC, e.entity ASC
            LIMIT ?
            """,
            [normalized, *_accessible_scope_params(provider), normalized, max(1, min(50, limit))],
        ).fetchall()
    related = [{"entity": str(row["entity"]), "count": int(row["count"])} for row in rows]
    return {"entity": normalized, "count": len(related), "related": related}


def feedback_memory(provider: Any, *, memory_id: str, rating: str, note: str = "") -> dict[str, Any]:
    rating_text = str(rating or "").strip().lower()
    if rating_text in {"helpful", "up", "+1", "1", "true", "yes"}:
        rating_value = 1
    elif rating_text in {"unhelpful", "down", "-1", "0", "false", "no"}:
        rating_value = -1
    else:
        return {"updated": False, "error": "rating must be helpful or unhelpful", "id": memory_id}

    conn = provider._require_conn()
    with provider._lock:
        row = conn.execute(
            f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({_scope_placeholders(provider, writable=True)})",
            [memory_id, *_writable_scope_params(provider)],
        ).fetchone()
        if row is None:
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
        metadata["trust"] = clamp_float(old_trust + (0.08 if rating_value > 0 else -0.12), default=old_trust)
        metadata["feedback_count"] = feedback_count
        metadata["helpful_count"] = helpful_count
        metadata["unhelpful_count"] = unhelpful_count
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        safe_note = sanitize_report_text(note)[:240]
        conn.execute(
            "INSERT INTO memory_feedback(memory_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
            (memory_id, rating_value, safe_note, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            f"UPDATE memories SET metadata = ? WHERE id = ? AND scope_id IN ({_scope_placeholders(provider, writable=True)})",
            (metadata_json, memory_id, *_writable_scope_params(provider)),
        )
        conn.commit()
    return {
        "updated": True,
        "id": memory_id,
        "rating": "helpful" if rating_value > 0 else "unhelpful",
        "trust": metadata["trust"],
        "feedback_count": feedback_count,
    }


def inspect_memory(provider: Any, *, memory_id: str) -> dict[str, Any]:
    conn = provider._require_conn()
    with provider._lock:
        row = conn.execute(
            f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({_scope_placeholders(provider)})",
            [memory_id, *_accessible_scope_params(provider)],
        ).fetchone()
        if row is None:
            return {"found": False, "id": memory_id, "memory": None, "feedback": {"count": 0, "items": []}, "relations": {"count": 0, "items": []}}
        feedback_rows = conn.execute(
            "SELECT rating, note, created_at FROM memory_feedback WHERE memory_id = ? ORDER BY created_at DESC",
            (memory_id,),
        ).fetchall()
        relation_rows = conn.execute(
            """
            SELECT source_memory_id, target_memory_id, relation_type, confidence, note, created_at
            FROM memory_relations
            WHERE source_memory_id = ? OR target_memory_id = ?
            ORDER BY created_at DESC
            """,
            (memory_id, memory_id),
        ).fetchall()
    metadata = load_metadata(row["metadata"])
    memory = {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "source": str(row["source"]),
        "target": str(row["target"]),
        "content": str(row["content"]),
        "summary": str(row["summary"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "metadata": metadata,
    }
    feedback = [dict(item) for item in feedback_rows]
    relations = [dict(item) for item in relation_rows]
    return {
        "found": True,
        "id": memory_id,
        "memory": memory,
        "feedback": {"count": len(feedback), "items": feedback},
        "relations": {"count": len(relations), "items": relations},
    }


def explain_query(provider: Any, *, query: str, limit: int = 5) -> dict[str, Any]:
    results = provider._recall_service.search_memories(query, limit=max(1, min(20, limit)))
    payload_results: list[dict[str, Any]] = []

    def _component_float(metadata: dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            return float(metadata.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    component_keys = (
        "lexical_score",
        "bm25_score",
        "vector_score",
        "rrf_score",
        "pre_quality_score",
        "quality_weight_applied",
        "metadata_weight",
        "entity_overlap_bonus",
        "entity_distance_score",
        "entity_distance_weight",
        "entity_distance_bonus",
        "relation_evidence_count",
        "relation_rerank_bonus",
        "pre_decay_score",
        "base_score",
        "temporal_decay_multiplier",
        "temporal_decay_weight",
        "temporal_policy_weight",
        "recency_bonus",
        "final_score",
        "general_weight",
        "trust",
        "importance",
        "confidence",
        "min_score",
        "vector_only_min_score",
    )
    def _payload_for_item(item: Any, rank: int) -> dict[str, Any]:
        metadata = dict(item.metadata or {})
        components: dict[str, Any] = {
            key: _component_float(metadata, key, 1.0 if key in {"temporal_decay_multiplier", "general_weight"} else 0.0)
            for key in component_keys
        }
        components["final_score"] = float(item.score or components.get("final_score") or 0.0)
        components["relation_evidence_types"] = metadata.get("relation_evidence_types") if isinstance(metadata.get("relation_evidence_types"), list) else []
        components["relation_evidence_ids"] = metadata.get("relation_evidence_ids") if isinstance(metadata.get("relation_evidence_ids"), list) else []
        components["relation_rerank_enabled"] = bool(metadata.get("relation_rerank_enabled") or False)
        components["temporal_policy_class"] = str(metadata.get("temporal_policy_class") or "")
        components["rejected_reason"] = str(metadata.get("rejected_reason") or "")
        return {
            "rank": rank,
            "id": item.id,
            "target": item.target,
            "source": item.source,
            "summary": item.summary,
            "score": round(item.score, 4),
            "updated_at": item.updated_at,
            "components": components,
        }

    for rank, item in enumerate(results, start=1):
        payload_results.append(_payload_for_item(item, rank))
    rejected_candidates = list(getattr(provider._recall_service, "last_rejected_candidates", []) or [])
    rejected_payload = [_payload_for_item(item, rank) for rank, item in enumerate(rejected_candidates[: max(1, min(20, limit))], start=1)]
    return {
        "query": query,
        "count": len(payload_results),
        "results": payload_results,
        "rejected_count": len(rejected_candidates),
        "rejected_candidates": rejected_payload,
        "funnel_trace": dict(getattr(provider._recall_service, "last_funnel_trace", {}) or {}),
    }


def _benchmark_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    output: list[str] = []
    for item in candidates:
        clean = str(item or "").strip()
        if clean and clean not in output:
            output.append(clean)
    return output


def _benchmark_cases(queries: list[str] | None, cases: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if cases:
        normalized: list[dict[str, Any]] = []
        for case in cases:
            if not isinstance(case, dict):
                continue
            query = str(case.get("query") or "").strip()
            if not query:
                continue
            normalized.append(dict(case, query=query))
        return normalized
    return [{"query": str(query).strip()} for query in (queries or []) if str(query).strip()]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = int(index)
    upper = min(len(ordered) - 1, lower + 1)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction, 3)


def _merge_filter_counts(total: dict[str, int], trace: dict[str, Any]) -> None:
    filters = trace.get("filters") if isinstance(trace, dict) else {}
    if not isinstance(filters, dict):
        return
    for key, value in filters.items():
        try:
            total[str(key)] = int(total.get(str(key), 0)) + int(value or 0)
        except (TypeError, ValueError):
            continue


def benchmark_queries(
    provider: Any,
    *,
    queries: list[str] | None = None,
    cases: list[dict[str, Any]] | None = None,
    limit: int = 5,
    auto_explain_on_fail: bool = False,
    include_trace: bool = False,
    prompt_budget_chars: int = 0,
) -> dict[str, Any]:
    normalized_cases = _benchmark_cases(queries, cases)
    rows: list[dict[str, Any]] = []
    aggregate_failures: list[str] = []
    bounded_limit = max(1, min(20, limit))
    latencies: list[float] = []
    filter_counts: dict[str, int] = {}
    expected_total = 0
    expected_found = 0
    cases_with_expected = 0
    cases_with_expected_hit = 0
    forbidden_violations = 0
    prompt_budget_checked = 0
    prompt_budget_hits = 0
    for case in normalized_cases:
        query = str(case.get("query") or "").strip()
        started = time.perf_counter()
        results = provider._recall_service.search_memories(query, limit=bounded_limit)
        latency_ms = (time.perf_counter() - started) * 1000.0
        latencies.append(latency_ms)
        trace = dict(getattr(provider._recall_service, "last_funnel_trace", {}) or {})
        _merge_filter_counts(filter_counts, trace)
        ids = [str(item.id) for item in results]
        ranks = {memory_id: index for index, memory_id in enumerate(ids, start=1)}
        failures: list[str] = []
        expected_ids = _benchmark_id_list(case.get("expected_ids"))
        forbidden_ids = _benchmark_id_list(case.get("forbidden_ids"))
        min_rank_raw = case.get("min_rank")
        try:
            min_rank = int(min_rank_raw) if min_rank_raw is not None else 0
        except (TypeError, ValueError):
            min_rank = 0
        min_top_score_raw = case.get("min_top_score")
        try:
            min_top_score = float(min_top_score_raw) if min_top_score_raw is not None else 0.0
        except (TypeError, ValueError):
            min_top_score = 0.0
        raw_top_score = float(results[0].score) if results else 0.0
        top_score = round(raw_top_score, 4)
        if expected_ids:
            cases_with_expected += 1
        case_expected_hit = False
        for expected_id in expected_ids:
            expected_total += 1
            rank = ranks.get(expected_id)
            if rank is None:
                failures.append(f"expected_id_missing:{expected_id}")
            else:
                expected_found += 1
                case_expected_hit = True
                if min_rank > 0 and rank > min_rank:
                    failures.append(f"expected_id_rank_too_low:{expected_id}:rank={rank}:min_rank={min_rank}")
        if case_expected_hit:
            cases_with_expected_hit += 1
        for forbidden_id in forbidden_ids:
            if forbidden_id in ranks:
                forbidden_violations += 1
                failures.append(f"forbidden_id_present:{forbidden_id}:rank={ranks[forbidden_id]}")
        if min_top_score_raw is not None and top_score < min_top_score:
            failures.append(f"top_score_below_min:{top_score}:min_top_score={min_top_score}")
        row: dict[str, Any] = {
            "query": query,
            "count": len(results),
            "latency_ms": round(latency_ms, 3),
            "top_score": top_score,
            "raw_top_score": raw_top_score,
            "ids": ids,
            "passed": not failures,
            "failures": failures,
        }
        if prompt_budget_chars > 0:
            prompt_budget_checked += 1
            returned_chars = int(((trace.get("final") or {}) if isinstance(trace.get("final"), dict) else {}).get("returned_chars") or 0)
            row["prompt_budget_chars"] = prompt_budget_chars
            row["returned_chars"] = returned_chars
            row["prompt_budget_hit"] = returned_chars <= prompt_budget_chars
            if row["prompt_budget_hit"]:
                prompt_budget_hits += 1
        if include_trace:
            row["funnel_trace"] = trace
        if failures and auto_explain_on_fail:
            row["explain"] = explain_query(provider, query=query, limit=bounded_limit)
        rows.append(row)
        aggregate_failures.extend(f"{query}: {failure}" for failure in failures)
    metrics: dict[str, Any] = {
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "known_answer_recall": round(expected_found / expected_total, 4) if expected_total else None,
        "top_k_accuracy": round(cases_with_expected_hit / cases_with_expected, 4) if cases_with_expected else None,
        "expected_total": expected_total,
        "expected_found": expected_found,
        "forbidden_violations": forbidden_violations,
        "filter_counts": filter_counts,
    }
    if prompt_budget_checked:
        metrics["prompt_budget_hit_rate"] = round(prompt_budget_hits / prompt_budget_checked, 4)
        metrics["prompt_budget_checked"] = prompt_budget_checked
    return {
        "query_count": len(normalized_cases),
        "limit": bounded_limit,
        "passed": not aggregate_failures,
        "failures": aggregate_failures,
        "metrics": metrics,
        "results": rows,
    }


def stats_payload(provider: Any) -> dict[str, Any]:
    conn = provider._require_conn()
    with provider._lock:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        scoped = conn.execute(f"SELECT COUNT(*) FROM memories WHERE scope_id IN ({_scope_placeholders(provider)})", _accessible_scope_params(provider)).fetchone()[0]
        local = conn.execute("SELECT COUNT(*) FROM memories WHERE scope_id = ?", (provider._scope_id,)).fetchone()[0]
        shared = conn.execute("SELECT COUNT(*) FROM memories WHERE scope_id = ?", (provider._shared_scope_id,)).fetchone()[0]
        shared_pool_scope_id = str(getattr(provider, "_shared_pool_scope_id", "") or "")
        shared_pool = conn.execute("SELECT COUNT(*) FROM memories WHERE scope_id = ?", (shared_pool_scope_id,)).fetchone()[0] if shared_pool_scope_id else 0
        entities = conn.execute(
            f"""
            SELECT COUNT(DISTINCT e.entity)
            FROM memory_entities e
            JOIN memories m ON m.id = e.memory_id
            WHERE m.scope_id IN ({_scope_placeholders(provider)})
            """,
            _accessible_scope_params(provider),
        ).fetchone()[0]
        feedback_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM memory_feedback f
            JOIN memories m ON m.id = f.memory_id
            WHERE m.scope_id IN ({})
            """.format(_scope_placeholders(provider)),
            _accessible_scope_params(provider),
        ).fetchone()[0]
    vector_path = ""
    vector_table = ""
    vector_embedder: dict[str, Any] = {}
    if provider._vector_store is not None:
        try:
            refresh_vector_audit(provider)
        except Exception:
            pass
        vector_path = str(provider._vector_store.db_path)
        vector_table = provider._vector_store.table_name
    if provider._embedder is not None:
        vector_embedder = provider._embedder.describe()
    return {
        "provider": provider.name,
        "db_path": str(provider._db_path) if provider._db_path else "",
        "scope_id": provider._scope_id,
        "shared_scope_id": provider._shared_scope_id,
        "accessible_scope_ids": list(provider._accessible_scope_ids),
        "total_memories": total,
        "scope_memories": scoped,
        "local_scope_memories": local,
        "shared_scope_memories": shared,
        "shared_pool_scope_memories": shared_pool,
        "shared_pool": {
            "enabled": bool(getattr(provider, "_shared_pool_enabled", False)),
            "write_enabled": bool(getattr(provider, "_shared_pool_write_enabled", False)),
            "pool_id": str(getattr(provider, "_shared_pool_id", "") or ""),
            "scope_id": shared_pool_scope_id,
            "memories": shared_pool,
        },
        "scope_entities": entities,
        "scope_feedback_rows": feedback_rows,
        "curated_memories": len(iter_curated_entries(provider._hermes_home)),
        "migration": dict(provider._migration_info),
        "vector": {
            "enabled": provider._vector_enabled,
            "ready": provider._vector_ready,
            "status": provider._vector_status,
            "message": provider._vector_message,
            "backend": provider._vector_backend,
            "path": vector_path,
            "table": vector_table,
            "row_count": provider._vector_row_count,
            "unique_id_count": provider._vector_unique_id_count,
            "duplicate_row_count": provider._vector_duplicate_row_count,
            "sync_mode": str((provider._vector_config or {}).get("sync_mode") or "incremental"),
            "embedder": vector_embedder,
            "fallback_embedder": dict(((provider._vector_config or {}).get("fallback_embedder") or {})),
        },
        "retrieval": {
            "mode": str((provider._retrieval_config or {}).get("mode") or "lexical"),
            "lexical_weight": float((provider._retrieval_config or {}).get("lexical_weight") or 1.0),
            "vector_weight": float((provider._retrieval_config or {}).get("vector_weight") or 0.0),
        },
    }
