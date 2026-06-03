from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .capture import store_now
from .graph import clamp_float, compact_context_lines, load_metadata, normalize_entity
from .governance import classify_memory, is_conflicting, merge_memory_text, semantic_similarity
from .models import recall_scope_mode
from .sql_store import delete_rows, exact_duplicate_groups, iter_curated_entries, update_row
from .vector_runtime import mark_vector_needs_repair, setup_vector_layer, upsert_vector_record


def _scope_placeholders(provider: Any) -> str:
    return ",".join("?" for _ in provider._accessible_scope_ids)


def _accessible_scope_params(provider: Any) -> list[str]:
    return [str(scope_id) for scope_id in provider._accessible_scope_ids]


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
) -> tuple[str, bool, str]:
    if semantic_merge and not allow_duplicate and target in {"user", "ops", "project"}:
        merge_id, merged_content = find_semantic_merge_candidate(provider, content, target)
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
    )
    if inserted:
        _mark_conflicts_for_memory(provider, memory_id=memory_id, content=content, target=target)
    outcome = "stored" if inserted else "duplicate" if memory_id else "skipped"
    return memory_id, inserted, outcome


def _mark_conflicts_for_memory(provider: Any, *, memory_id: str, content: str, target: str) -> int:
    """Record deterministic contradiction edges for a newly inserted memory."""

    conn = provider._require_conn()
    now = datetime.now(timezone.utc).isoformat()
    with provider._lock:
        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE id != ? AND target = ? AND scope_id IN ({})
            ORDER BY updated_at DESC
            LIMIT 50
            """.format(_scope_placeholders(provider)),
            [memory_id, target, *_accessible_scope_params(provider)],
        ).fetchall()
        conflicting_ids = [str(row["id"]) for row in rows if is_conflicting(str(row["content"]), content)]
        if not conflicting_ids:
            return 0
        for target_id in conflicting_ids:
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                VALUES (?, ?, 'contradicts', ?, ?, ?)
                """,
                (memory_id, target_id, 0.74, f"conflict: contradicts memory {target_id}", now),
            )
            conn.execute(
                "INSERT INTO memory_feedback(memory_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
                (memory_id, -1, f"conflict: contradicts memory {target_id}", now),
            )
        row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
        metadata_payload = load_metadata(row["metadata"] if row is not None else "{}")
        relation_types = metadata_payload.get("relation_types")
        if not isinstance(relation_types, list):
            relation_types = []
        if "contradicts" not in relation_types:
            relation_types.append("contradicts")
        metadata_payload["relation_types"] = relation_types
        metadata_payload["conflict_count"] = int(metadata_payload.get("conflict_count") or 0) + len(conflicting_ids)
        metadata_payload["feedback_count"] = int(metadata_payload.get("feedback_count") or 0) + len(conflicting_ids)
        metadata_payload["unhelpful_count"] = int(metadata_payload.get("unhelpful_count") or 0) + len(conflicting_ids)
        metadata_payload["trust"] = clamp_float(float(metadata_payload.get("trust") or 0.5) - 0.08, default=0.5)
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True), memory_id),
        )
        conn.commit()
    return len(conflicting_ids)


def find_semantic_merge_candidate(provider: Any, content: str, target: str) -> tuple[str, str]:
    threshold = float(provider._config_value("semantic_merge_threshold", 0.72))
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE scope_id IN ({}) AND target = ?
            ORDER BY updated_at DESC
            LIMIT 50
            """.format(_scope_placeholders(provider)),
            [*_accessible_scope_params(provider), target],
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
    return provider._shared_scope_id if mode == "shared" else provider._scope_id


def _row_scope_mode(provider: Any, row: Any) -> str:
    return "shared" if str(row["scope_id"]) == provider._shared_scope_id else "local"


def update_memory(provider: Any, memory_id: str, content: str, target: str | None = None) -> tuple[bool, str, str]:
    with provider._lock:
        placeholders = _scope_placeholders(provider)
        scope_params = _accessible_scope_params(provider)
        existing = provider._require_conn().execute(
            f"SELECT source, target, scope_id FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
            [memory_id, *scope_params],
        ).fetchone()
        if existing is None:
            return False, "", ""
        new_target = target or str(existing["target"])
        new_mode = recall_scope_mode(new_target, str(existing["source"]))
        if str(existing["scope_id"]) != _expected_scope_id_for_mode(provider, new_mode):
            return False, "target changes between shared durable and local scratch scopes are not allowed", ""
        updated, summary, updated_at = update_row(
            provider._require_conn(),
            memory_id=memory_id,
            content=content,
            target=target,
            scope_ids=provider._accessible_scope_ids,
        )
    if updated:
        placeholders = _scope_placeholders(provider)
        row = provider._require_conn().execute(
            f"SELECT source, target, content, summary, updated_at, scope_id FROM memories WHERE id = ? AND scope_id IN ({placeholders})",
            [memory_id, *_accessible_scope_params(provider)],
        ).fetchone()
        if row is not None:
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
        placeholders = _scope_placeholders(provider)
        scope_params = _accessible_scope_params(provider)
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
    requested_mode = recall_scope_mode(requested_target, str(target_row["source"]))
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
        where = f"WHERE scope_id IN ({_scope_placeholders(provider)})"
        params: tuple[Any, ...] = tuple(_accessible_scope_params(provider))
    else:
        where = ""
        params = ()
    with provider._lock:
        rows = conn.execute(
            f"SELECT id, target, content, updated_at, metadata FROM memories {where}",
            params,
        ).fetchall()

    now = datetime.now(timezone.utc)
    tiers = {"core": 0, "working": 0, "archive": 0}
    decay_candidates: list[str] = []
    updates: list[tuple[str, str]] = []
    for row in rows:
        metadata: dict[str, Any] = {}
        try:
            metadata.update(json.loads(str(row["metadata"] or "{}")))
        except Exception:
            pass
        classified = dict(metadata)
        classified.update(classify_memory(str(row["content"]), str(row["target"])))
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
        tiers[tier] = tiers.get(tier, 0) + 1
        updates.append((json.dumps(classified, ensure_ascii=False, sort_keys=True), str(row["id"])))
    if not dry_run:
        with provider._lock:
            if scope_only:
                placeholders = _scope_placeholders(provider)
                conn.executemany(
                    f"UPDATE memories SET metadata = ? WHERE id = ? AND scope_id IN ({placeholders})",
                    [(*update, *_accessible_scope_params(provider)) for update in updates],
                )
            else:
                conn.executemany("UPDATE memories SET metadata = ? WHERE id = ?", updates)
            conn.commit()
    return {"dry_run": dry_run, "scope_only": scope_only, "total": len(rows), "tiers": tiers, "decay_candidates": decay_candidates}


def delete_memories(provider: Any, ids: list[str]) -> int:
    requested_ids = [str(memory_id) for memory_id in ids if str(memory_id).strip()]
    if not requested_ids:
        return 0
    placeholders = ",".join("?" for _ in requested_ids)
    with provider._lock:
        scoped_ids = [
            str(row["id"])
            for row in provider._require_conn()
            .execute(f"SELECT id FROM memories WHERE id IN ({placeholders}) AND scope_id IN ({_scope_placeholders(provider)})", [*requested_ids, *_accessible_scope_params(provider)])
            .fetchall()
        ]
        deleted_changes = delete_rows(provider._require_conn(), scoped_ids, scope_ids=provider._accessible_scope_ids)
    if provider._vector_store and scoped_ids:
        try:
            provider._vector_store.delete_by_ids(scoped_ids)
        except Exception as exc:
            mark_vector_needs_repair(provider, exc)
    return deleted_changes


def dedupe_memories(provider: Any, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
    groups = exact_duplicate_groups(provider._require_conn(), scope_ids=provider._accessible_scope_ids if scope_only else None)
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


def context_payload(provider: Any, *, query: str, limit: int = 5, max_chars: int = 900) -> dict[str, Any]:
    results = provider._recall_service.search_memories(query, limit=max(1, min(20, limit)))
    records: list[dict[str, Any]] = []
    entity_counts: dict[str, int] = {}
    for item in results:
        metadata = load_metadata(item.metadata or {})
        entities = metadata.get("entities") if isinstance(metadata.get("entities"), list) else []
        for entity in entities:
            entity_counts[str(entity)] = entity_counts.get(str(entity), 0) + 1
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
            f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({_scope_placeholders(provider)})",
            [memory_id, *_accessible_scope_params(provider)],
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
        conn.execute(
            "INSERT INTO memory_feedback(memory_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
            (memory_id, rating_value, note[:240], datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (metadata_json, memory_id))
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
    for item in results:
        metadata = dict(item.metadata or {})
        components = {
            "lexical_score": float(metadata.get("lexical_score") or 0.0),
            "bm25_score": float(metadata.get("bm25_score") or 0.0),
            "vector_score": float(metadata.get("vector_score") or 0.0),
            "base_score": float(metadata.get("base_score") or item.score or 0.0),
            "temporal_decay_multiplier": float(metadata.get("temporal_decay_multiplier") or 1.0),
            "trust": clamp_float(metadata.get("trust"), default=0.5),
        }
        payload_results.append(
            {
                "id": item.id,
                "target": item.target,
                "source": item.source,
                "summary": item.summary,
                "score": round(item.score, 4),
                "updated_at": item.updated_at,
                "components": components,
            }
        )
    return {"query": query, "count": len(payload_results), "results": payload_results}


def benchmark_queries(provider: Any, *, queries: list[str], limit: int = 5) -> dict[str, Any]:
    normalized_queries = [str(query).strip() for query in queries if str(query).strip()]
    rows: list[dict[str, Any]] = []
    for query in normalized_queries:
        started = time.perf_counter()
        results = provider._recall_service.search_memories(query, limit=max(1, min(20, limit)))
        latency_ms = (time.perf_counter() - started) * 1000.0
        rows.append(
            {
                "query": query,
                "count": len(results),
                "latency_ms": round(latency_ms, 3),
                "top_score": round(results[0].score, 4) if results else 0.0,
            }
        )
    return {"query_count": len(normalized_queries), "limit": max(1, min(20, limit)), "results": rows}


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
