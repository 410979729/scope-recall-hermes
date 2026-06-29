from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
import logging
from typing import Any, cast

from .capture_filters import sanitize_report_text
from .embedders import build_embedder
from .gating import config_bool
from .graph import lifecycle_is_hidden, lifecycle_visible_sql, load_metadata
from .sqlite_vector_store import SQLiteBruteForceVectorStore
from .vector_store import LanceVectorStore, native_vector_dependency_status

logger = logging.getLogger(__name__)


def _vector_mutation_lock(provider: Any) -> AbstractContextManager[Any]:
    """Return the provider-level vector companion mutation lock.

    Older tests and ad-hoc repair runtimes may not define `_vector_lock`; fall
    back to the provider lock, then to a no-op context manager for compatibility.
    """
    lock = getattr(provider, "_vector_lock", None) or getattr(provider, "_lock", None)
    if hasattr(lock, "__enter__") and hasattr(lock, "__exit__"):
        return cast(AbstractContextManager[Any], lock)
    return nullcontext()


def mark_vector_needs_repair(provider: Any, exc: Exception | str) -> None:
    provider._vector_ready = False
    provider._vector_status = "needs_repair"
    provider._vector_message = sanitize_report_text(str(exc))


def _normalize_vector_backend(value: Any) -> str:
    backend = str(value or "lancedb").strip().lower()
    if backend == "sqlite":
        return "sqlite-bruteforce"
    return backend


def _append_vector_message(provider: Any, message: str) -> None:
    current = str(getattr(provider, "_vector_message", "") or "")
    provider._vector_message = f"{current}; {message}" if current else message


def _open_sqlite_vector_store(provider: Any, *, table_name: str, dimensions: int, metric: str) -> None:
    temp_store = SQLiteBruteForceVectorStore(
        provider._storage_dir / "vector.sqlite3",
        table_name=table_name,
        dimensions=dimensions,
        metric=metric,
    )
    temp_store.open()
    provider._vector_store = temp_store
    provider._vector_backend = "sqlite-bruteforce"


def _open_vector_store(provider: Any, *, dimensions: int) -> None:
    if provider._storage_dir is None:
        raise RuntimeError("storage not initialized")
    table_name = str((provider._vector_config or {}).get("table_name") or "memories")
    metric = str((provider._retrieval_config or {}).get("metric") or "cosine")
    backend = _normalize_vector_backend(getattr(provider, "_vector_backend", "") or "lancedb")
    try:
        old_store = provider._vector_store
        if old_store is not None:
            old_store.close()
    except Exception:
        pass

    if backend == "sqlite-bruteforce":
        _open_sqlite_vector_store(provider, table_name=table_name, dimensions=dimensions, metric=metric)
        return

    if backend != "lancedb":
        raise RuntimeError(f"unsupported backend {backend}")

    vector_dir = provider._storage_dir / "lancedb"
    temp_store = LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if not temp_store.is_available():
        fallback_backend = _normalize_vector_backend((provider._vector_config or {}).get("fallback_backend") or "")
        if fallback_backend == "sqlite-bruteforce":
            status = native_vector_dependency_status()
            detail = f"returncode={status.get('returncode')}" if status.get("returncode") is not None else str(status.get("stderr") or "not installed")
            message = f"lancedb unavailable or unsafe ({detail}); using sqlite-bruteforce fallback"
            _append_vector_message(provider, message)
            logger.warning("Scope Recall vector backend fallback: %s", message)
            _open_sqlite_vector_store(provider, table_name=table_name, dimensions=dimensions, metric=metric)
            return
        status = native_vector_dependency_status()
        detail = f"returncode={status.get('returncode')}" if status.get("returncode") is not None else str(status.get("stderr") or "not installed")
        raise RuntimeError(f"lancedb/pyarrow is not installed or unsafe ({detail})")
    temp_store.open()
    provider._vector_backend = "lancedb"

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
    old_store = getattr(provider, "_vector_store", None)
    if old_store is not None:
        try:
            old_store.close()
        except Exception:
            logger.debug("Scope Recall vector store close during setup failed", exc_info=True)
    provider._vector_enabled = config_bool(provider._vector_config or {}, "enabled", False)
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
    model_or_raise = getattr(provider._embedder, "_model_or_raise", None)
    if provider._embedder.provider == "sentence-transformers" and callable(model_or_raise):
        try:
            model_or_raise()
        except Exception as exc:
            provider._vector_status = "degraded"
            provider._vector_message = str(exc)
            provider._vector_store = None
            return

    try:
        _open_vector_store(provider, dimensions=provider._embedder.dimensions)
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
    with _vector_mutation_lock(provider):
        if not provider._vector_store:
            counts = {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0, "duplicate_ids": 0}
        else:
            counts = provider._vector_store.audit_counts()
        provider._vector_row_count = int(counts.get("physical_rows") or 0)
        provider._vector_unique_id_count = int(counts.get("unique_ids") or 0)
        provider._vector_duplicate_row_count = int(counts.get("duplicate_rows") or 0)
        return counts



def _should_index_target(provider: Any, target: str) -> bool:
    return str(target) != "general" or config_bool(provider._vector_config or {}, "index_general", False)


def sync_vector_index(provider: Any) -> int:
    if not provider._vector_store or not provider._embedder:
        return 0
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            f"SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories m WHERE {lifecycle_visible_sql('m')} ORDER BY updated_at ASC"
        ).fetchall()
    with _vector_mutation_lock(provider):
        if not rows:
            existing = provider._vector_store.list_ids()
            if existing:
                provider._vector_store.delete_by_ids(existing)
            refresh_vector_audit(provider)
            return 0

        desired = {str(row["id"]): row for row in rows if _should_index_target(provider, str(row["target"]))}
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
    metadata: dict[str, Any] | str | None = None,
) -> None:
    with _vector_mutation_lock(provider):
        if not provider._vector_ready or not provider._vector_store or not provider._embedder:
            return
        resolved_metadata = metadata
        if resolved_metadata is None:
            try:
                row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (id,)).fetchone()
                if row is not None:
                    resolved_metadata = row["metadata"]
            except Exception:
                resolved_metadata = None
        if lifecycle_is_hidden(load_metadata(resolved_metadata or {})):
            try:
                provider._vector_store.delete_by_ids([id])
                refresh_vector_audit(provider)
            except Exception as exc:
                mark_vector_needs_repair(provider, exc)
                logger.warning("Scope Recall vector lifecycle cleanup failed; SQLite truth row preserved and vector repair is needed: %s", exc)
            return
        if not _should_index_target(provider, target):
            try:
                provider._vector_store.delete_by_ids([id])
                refresh_vector_audit(provider)
            except Exception as exc:
                mark_vector_needs_repair(provider, exc)
                logger.warning("Scope Recall vector exclusion cleanup failed; SQLite truth row preserved and vector repair is needed: %s", exc)
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
