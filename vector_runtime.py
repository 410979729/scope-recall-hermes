from __future__ import annotations

import logging
from typing import Any

from .embedders import build_embedder
from .vector_store import LanceVectorStore

logger = logging.getLogger(__name__)


def mark_vector_needs_repair(provider: Any, exc: Exception | str) -> None:
    provider._vector_ready = False
    provider._vector_status = "needs_repair"
    provider._vector_message = str(exc)


def _rebuild_vector_store(provider: Any, *, dimensions: int) -> None:
    if provider._storage_dir is None:
        raise RuntimeError("storage not initialized")
    table_name = str((provider._vector_config or {}).get("table_name") or "memories")
    metric = str((provider._retrieval_config or {}).get("metric") or "cosine")
    vector_dir = provider._storage_dir / "lancedb"
    try:
        old_store = provider._vector_store
        if old_store is not None:
            old_store.close()
    except Exception:
        pass

    temp_store = LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if not temp_store.is_available():
        raise RuntimeError("lancedb is not installed")
    temp_store.open()

    schema_dimensions = 0
    try:
        table = temp_store._require_table()
        vector_field = table.schema.field("vector")
        if hasattr(vector_field.type, "list_size"):
            schema_dimensions = int(vector_field.type.list_size)
    except Exception:
        schema_dimensions = 0

    if schema_dimensions and schema_dimensions != dimensions:
        if temp_store._db is not None and hasattr(temp_store._db, "drop_table"):
            temp_store._db.drop_table(table_name, ignore_missing=True)
        temp_store.close()
        temp_store = LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
        temp_store.open()
    provider._vector_store = temp_store



def setup_vector_layer(provider: Any) -> None:
    provider._vector_enabled = bool((provider._vector_config or {}).get("enabled", False))
    provider._vector_backend = str((provider._vector_config or {}).get("backend") or "lancedb")
    provider._vector_ready = False
    provider._vector_status = "disabled"
    provider._vector_message = ""
    provider._vector_row_count = 0
    provider._vector_unique_id_count = 0
    provider._vector_duplicate_row_count = 0
    provider._embedder = None
    provider._vector_store = None
    if not provider._vector_enabled:
        return
    if provider._storage_dir is None:
        provider._vector_status = "error"
        provider._vector_message = "storage not initialized"
        return

    embedder_cfg = dict((provider._vector_config or {}).get("embedder") or {})
    fallback_cfg = dict((provider._vector_config or {}).get("fallback_embedder") or {})

    provider._embedder = build_embedder(embedder_cfg)
    if not provider._embedder.is_available() and fallback_cfg:
        fallback_embedder = build_embedder(fallback_cfg)
        if fallback_embedder.is_available():
            provider._embedder = fallback_embedder
            provider._vector_message = f"primary embedder {embedder_cfg.get('provider') or 'unknown'} unavailable; using fallback {fallback_embedder.provider}"

    if not provider._embedder.is_available():
        provider._vector_status = "degraded"
        provider._vector_message = provider._vector_message or f"embedder {provider._embedder.provider} unavailable"
        return
    if provider._vector_backend != "lancedb":
        provider._vector_status = "degraded"
        provider._vector_message = f"unsupported backend {provider._vector_backend}"
        return

    if provider._embedder.provider == "sentence-transformers" and hasattr(provider._embedder, "_model_or_raise"):
        try:
            provider._embedder._model_or_raise()
        except Exception as exc:
            provider._vector_status = "degraded"
            provider._vector_message = str(exc)
            provider._vector_store = None
            return

    try:
        _rebuild_vector_store(provider, dimensions=provider._embedder.dimensions)
        provider._vector_row_count = sync_vector_index(provider)
        refresh_vector_audit(provider)
    except Exception as exc:
        provider._vector_status = "degraded"
        provider._vector_message = str(exc)
        provider._vector_store = None
        return

    provider._vector_ready = True
    provider._vector_status = "ready"
    if not provider._vector_message:
        provider._vector_message = ""



def refresh_vector_audit(provider: Any) -> dict[str, int]:
    if not provider._vector_store:
        counts = {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0, "duplicate_ids": 0}
    else:
        counts = provider._vector_store.audit_counts()
    provider._vector_row_count = int(counts.get("physical_rows") or 0)
    provider._vector_unique_id_count = int(counts.get("unique_ids") or 0)
    provider._vector_duplicate_row_count = int(counts.get("duplicate_rows") or 0)
    return counts



def sync_vector_index(provider: Any) -> int:
    if not provider._vector_store or not provider._embedder:
        return 0
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at FROM memories ORDER BY updated_at ASC"
        ).fetchall()
    if not rows:
        existing = provider._vector_store.list_ids()
        if existing:
            provider._vector_store.delete_by_ids(existing)
        refresh_vector_audit(provider)
        return 0

    desired = {str(row["id"]): row for row in rows}
    existing_records = provider._vector_store.list_records()
    existing_ids = set(existing_records.keys())
    desired_ids = set(desired.keys())

    audit = refresh_vector_audit(provider)
    stale_ids = sorted(existing_ids - desired_ids)
    if stale_ids:
        provider._vector_store.delete_by_ids(stale_ids)

    if stale_ids or int(audit.get("duplicate_rows") or 0) > 0:
        provider._vector_store.repair_records({memory_id: dict(row) for memory_id, row in desired.items()})
        existing_records = provider._vector_store.list_records()
        existing_ids = set(existing_records.keys())

    changed_rows = []
    for memory_id, row in desired.items():
        current = existing_records.get(memory_id)
        if current is None:
            changed_rows.append(row)
            continue
        if str(current.get("updated_at") or "") != str(row["updated_at"] or ""):
            changed_rows.append(row)

    if changed_rows:
        texts = [provider._vector_text(row["summary"], row["content"]) for row in changed_rows]
        vectors = provider._embedder.embed_texts(texts)
        payload = []
        for row, vector in zip(changed_rows, vectors):
            payload.append(
                {
                    "id": row["id"],
                    "scope_id": row["scope_id"],
                    "source": row["source"],
                    "target": row["target"],
                    "content": row["content"],
                    "summary": row["summary"],
                    "updated_at": row["updated_at"],
                    "vector": vector,
                }
            )
        provider._vector_store.upsert_records(payload)
    refresh_vector_audit(provider)
    return len(desired)



def upsert_vector_record(
    provider: Any,
    *,
    id: str,
    source: str,
    target: str,
    content: str,
    summary: str,
    updated_at: str,
    scope_id: str | None = None,
) -> None:
    if not provider._vector_ready or not provider._vector_store or not provider._embedder:
        return
    try:
        vector = provider._embedder.embed(provider._vector_text(summary, content))
        provider._vector_store.upsert_records(
            [
                {
                    "id": id,
                    "scope_id": scope_id or provider._scope_id,
                    "source": source,
                    "target": target,
                    "content": content,
                    "summary": summary,
                    "updated_at": updated_at,
                    "vector": vector,
                }
            ]
        )
        refresh_vector_audit(provider)
    except Exception as exc:
        mark_vector_needs_repair(provider, exc)
        logger.warning("Scope Recall vector upsert failed; SQLite truth row preserved and vector repair is needed: %s", exc)
