"""Runtime setup and mutation helpers for vector companions.

Vector failures should mark repair-needed state and never silently delete or rewrite SQLite truth."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import sqlite3
from typing import Any, Sequence, cast
import uuid

from .capture_filters import sanitize_report_text
from .embedders import build_embedder
from .gating import config_bool
from .graph import load_metadata
from .lifecycle_policy import ordinary_recall_lifecycle_visible, ordinary_recall_lifecycle_visible_sql
from .vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    bootstrap_legacy_generation,
    claim_vector_events,
    complete_vector_event,
    current_generation,
    enqueue_vector_event,
    ensure_vector_generation_schema,
    fail_vector_event,
    resolve_generation_storage_root,
    validate_generation_compatibility,
)
from .vector_store import (
    LanceVectorStore,
    VectorStoreCompatibilityError,
    build_vector_store,
    native_vector_dependency_status,
    normalize_vector_backend,
)

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


_VECTOR_STATUS_MESSAGE_LIMIT = 300
_NATIVE_DEPENDENCY_DETAIL_LIMIT = 160


def _sanitize_vector_message(value: Exception | str, *, limit: int = _VECTOR_STATUS_MESSAGE_LIMIT) -> str:
    return sanitize_report_text(str(value))[:limit]


def mark_vector_needs_repair(provider: Any, exc: Exception | str) -> None:
    provider._vector_ready = False
    provider._vector_status = "needs_repair"
    provider._vector_message = _sanitize_vector_message(exc)


def _mark_vector_startup_degraded(provider: Any, exc: Exception | str) -> None:
    """Preserve startup's public degraded status while bounding safe detail."""

    provider._vector_ready = False
    provider._vector_status = "degraded"
    provider._vector_message = _sanitize_vector_message(exc)


def _normalize_vector_backend(value: Any) -> str:
    return normalize_vector_backend(value)


def _append_vector_message(provider: Any, message: str) -> str:
    """Append one bounded diagnostic and return the same safe text for logging."""

    safe_message = _sanitize_vector_message(message)
    current = _sanitize_vector_message(str(getattr(provider, "_vector_message", "") or ""))
    combined = f"{current}; {safe_message}" if current else safe_message
    provider._vector_message = _sanitize_vector_message(combined)
    return safe_message


def _native_dependency_detail(status: dict[str, Any]) -> str:
    returncode = status.get("returncode")
    if returncode is not None:
        return f"returncode={returncode}"
    return _sanitize_vector_message(
        str(status.get("stderr") or "not installed"),
        limit=_NATIVE_DEPENDENCY_DETAIL_LIMIT,
    )


def _delete_persisted_sqlite_vector_ids(vector_path: Path, ids: list[str]) -> dict[str, Any]:
    """Delete IDs without opening the dimension-sensitive SQLite wrapper."""

    try:
        conn = sqlite3.connect(vector_path, timeout=30.0)
        try:
            placeholders = ",".join("?" for _ in ids)
            cursor = conn.execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", ids)
            conn.commit()
            return {
                "status": "ok",
                "backend": "sqlite-bruteforce",
                "requested": len(ids),
                "deleted": max(0, int(cursor.rowcount)),
            }
        finally:
            conn.close()
    except Exception as exc:
        return {
            "status": "needs_repair",
            "backend": "sqlite-bruteforce",
            "requested": len(ids),
            "deleted": 0,
            "error": sanitize_report_text(str(exc))[:300],
        }


def _current_generation_manifest_read_only(storage_dir: Path) -> dict[str, Any] | None:
    """Read the active generation manifest without initializing schema."""

    db_path = Path(storage_dir) / "memory.sqlite3"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not {"vector_generation_state", "vector_generations"} <= tables:
            return None
        row = conn.execute(
            """
            SELECT g.*
            FROM vector_generation_state AS s
            JOIN vector_generations AS g ON g.generation_id = s.value
            WHERE s.key = 'current_generation'
            """
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _cleanup_persisted_vector_root(
    storage_dir: Path,
    *,
    ids: list[str],
    vector_config: dict[str, Any],
    retrieval_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Delete IDs from one physical vector storage root."""

    backend = normalize_vector_backend(vector_config.get("backend") or "lancedb")
    local_results: list[dict[str, Any]] = []
    vector_path = storage_dir / "vector.sqlite3"
    if vector_path.exists():
        sqlite_result = _delete_persisted_sqlite_vector_ids(vector_path, ids)
        if sqlite_result.get("status") != "ok":
            return sqlite_result
        local_results.append(sqlite_result)
        if backend == "sqlite-bruteforce":
            return sqlite_result

    if not config_bool(vector_config, "enabled", False):
        return local_results[0] if local_results else {
            "status": "disabled",
            "requested": len(ids),
            "deleted": 0,
        }
    if backend == "sqlite-bruteforce":
        return {"status": "absent", "backend": backend, "requested": len(ids), "deleted": 0}
    if backend == "lancedb" and not (storage_dir / "lancedb").exists():
        return local_results[0] if local_results else {
            "status": "absent",
            "backend": backend,
            "requested": len(ids),
            "deleted": 0,
        }

    embedder_config = dict(vector_config.get("embedder") or {})
    dimensions = int(embedder_config.get("dimensions") or 256)
    metric = str((retrieval_config or {}).get("metric") or "cosine")
    table_name = str(vector_config.get("table_name") or "memories")
    store = None
    try:
        store = build_vector_store(
            backend,
            storage_dir=storage_dir,
            table_name=table_name,
            dimensions=dimensions,
            metric=metric,
            config=vector_config,
        )
        if not store.is_available():
            raise RuntimeError(f"vector backend unavailable: {backend}")
        store.open()
        primary_deleted = int(store.delete(ids))
        if not local_results:
            return {"status": "ok", "backend": backend, "requested": len(ids), "deleted": primary_deleted}
        primary_result = {
            "status": "ok",
            "backend": backend,
            "requested": len(ids),
            "deleted": primary_deleted,
        }
        return {
            "status": "ok",
            "backend": "multiple",
            "requested": len(ids),
            "deleted": primary_deleted + sum(int(item.get("deleted") or 0) for item in local_results),
            "companions": [*local_results, primary_result],
        }
    except Exception as exc:
        result = {
            "status": "needs_repair",
            "backend": backend,
            "requested": len(ids),
            "deleted": sum(int(item.get("deleted") or 0) for item in local_results),
            "error": sanitize_report_text(str(exc))[:300],
        }
        if local_results:
            result["companions_cleaned"] = local_results
        return result
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def cleanup_persisted_vector_companions(
    storage_dir: Path,
    *,
    memory_ids: Sequence[str],
    vector_config: dict[str, Any],
    retrieval_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delete hidden truth IDs from active-generation and legacy companions.

    Truth must already be committed before this helper runs. The manifest is
    authoritative for the active physical root; legacy root companions are also
    inspected so an upgrade cannot strand sensitive hidden rows.
    """

    ids = list(dict.fromkeys(str(item) for item in memory_ids if str(item)))
    if not ids:
        return {"status": "not_needed", "requested": 0, "deleted": 0}
    storage_dir = Path(storage_dir).expanduser().resolve()
    targets: list[tuple[Path, dict[str, Any], dict[str, Any] | None, str]] = []
    manifest_error = ""
    try:
        manifest = _current_generation_manifest_read_only(storage_dir)
        if manifest is not None:
            generation_root = resolve_generation_storage_root(
                storage_dir,
                manifest.get("storage_path"),
            )
            generation_config = dict(vector_config)
            generation_config.update(
                {
                    "enabled": True,
                    "backend": str(manifest.get("backend") or vector_config.get("backend") or "lancedb"),
                    "table_name": str(manifest.get("table_name") or vector_config.get("table_name") or "memories"),
                    "embedder": {
                        **dict(vector_config.get("embedder") or {}),
                        "dimensions": int(manifest.get("dimensions") or 0),
                    },
                }
            )
            generation_retrieval = {
                **dict(retrieval_config or {}),
                "metric": str(manifest.get("metric") or (retrieval_config or {}).get("metric") or "cosine"),
            }
            targets.append((generation_root, generation_config, generation_retrieval, "active_generation"))
    except Exception as exc:
        manifest_error = sanitize_report_text(str(exc))[:300]

    # Keep scanning the pre-generation root. It may hold a fallback or retired
    # local companion even after the active pointer moved to a shadow directory.
    targets.append((storage_dir, dict(vector_config), dict(retrieval_config or {}), "legacy_root"))
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for root, config, retrieval, source in targets:
        key = (
            str(root.resolve()),
            normalize_vector_backend(config.get("backend") or "lancedb"),
            str(config.get("table_name") or "memories"),
        )
        if key in seen:
            continue
        seen.add(key)
        item = _cleanup_persisted_vector_root(
            root,
            ids=ids,
            vector_config=config,
            retrieval_config=retrieval,
        )
        if source != "legacy_root" or len(targets) > 1:
            item["storage_root"] = str(root)
            item["source"] = source
        results.append(item)

    if manifest_error:
        results.append(
            {
                "status": "needs_repair",
                "source": "active_generation_manifest",
                "requested": len(ids),
                "deleted": 0,
                "error": manifest_error,
            }
        )
    failed = [item for item in results if item.get("status") == "needs_repair"]
    deleted = sum(int(item.get("deleted") or 0) for item in results)
    successful = [item for item in results if item.get("status") == "ok"]
    if len(results) == 1:
        return results[0]
    return {
        "status": "needs_repair" if failed else "ok" if successful else "absent",
        "backend": "multiple",
        "requested": len(ids),
        "deleted": deleted,
        "companions": results,
    }


def _configured_generation_identity(
    provider: Any,
    embedder: Any,
    embedder_config: dict[str, Any] | None = None,
) -> GenerationIdentity:
    """Return the exact embedding-space identity requested by runtime config."""

    vector_config = dict(provider._vector_config or {})
    resolved_embedder_config = dict(embedder_config or vector_config.get("embedder") or {})
    return GenerationIdentity(
        backend=_normalize_vector_backend(vector_config.get("backend") or getattr(provider, "_vector_backend", "lancedb")),
        provider=str(embedder.provider or resolved_embedder_config.get("provider") or "unknown"),
        model=str(embedder.model or resolved_embedder_config.get("model") or "unknown"),
        dimensions=int(embedder.dimensions),
        metric=str((provider._retrieval_config or {}).get("metric") or "cosine"),
        prompt_profile=str(resolved_embedder_config.get("prompt_profile") or "default-v1"),
        document_prefix=str(resolved_embedder_config.get("document_prefix") or ""),
        query_prefix=str(resolved_embedder_config.get("query_prefix") or ""),
        request_dimensions=bool(resolved_embedder_config.get("request_dimensions", False)),
        table_name=str(vector_config.get("table_name") or "memories"),
    )


def _select_generation_storage(provider: Any, identity: GenerationIdentity) -> dict[str, Any] | None:
    """Resolve the active generation without changing its immutable backend.

    A first startup may register an explicitly configured fallback backend. On
    later startups that manifest is authoritative: reopening it is safe, while
    silently switching back to the configured primary backend would access a
    different physical generation without migration/activation.
    """

    conn = provider._require_conn()
    ensure_vector_generation_schema(conn)
    manifest = current_generation(conn)
    provider._vector_generation_id = str((manifest or {}).get("generation_id") or "")
    if manifest is None:
        provider._vector_storage_dir = provider._storage_dir
        return None
    configured_backend = _normalize_vector_backend(identity.backend)
    manifest_backend = _normalize_vector_backend(manifest.get("backend") or "")
    raw_fallback_backend = str((provider._vector_config or {}).get("fallback_backend") or "").strip()
    fallback_backend = _normalize_vector_backend(raw_fallback_backend) if raw_fallback_backend else ""
    selected_identity = identity
    if manifest_backend != configured_backend:
        if manifest_backend != fallback_backend:
            raise GenerationCompatibilityError(
                "active vector generation backend does not match the configured backend or explicit fallback: "
                f"current={manifest_backend!r}, configured={configured_backend!r}, fallback={fallback_backend!r}"
            )
        selected_identity = replace(identity, backend=manifest_backend)
    validate_generation_compatibility(manifest, selected_identity)
    if str(manifest.get("status") or "") != "active":
        raise GenerationCompatibilityError(
            f"current vector generation {manifest.get('generation_id')} is {manifest.get('status')!r}, expected 'active'"
        )
    generation_root = resolve_generation_storage_root(
        Path(provider._storage_dir),
        manifest.get("storage_path"),
    )
    provider._vector_backend = manifest_backend
    provider._vector_storage_dir = generation_root
    if manifest_backend != configured_backend:
        _append_vector_message(
            provider,
            f"using active {manifest_backend} fallback generation; switching to {configured_backend} requires explicit activation",
        )
    return manifest


def _register_open_legacy_generation(provider: Any, identity: GenerationIdentity) -> dict[str, Any]:
    """Adopt a compatible legacy companion in place after it opened safely."""

    conn = provider._require_conn()
    counts = provider._vector_store.audit_counts() if provider._vector_store is not None else {}
    manifest = bootstrap_legacy_generation(
        conn,
        identity=identity,
        storage_path=".",
        row_count=int(counts.get("physical_rows") or 0),
        unique_id_count=int(counts.get("unique_ids") or 0),
    )
    conn.commit()
    provider._vector_generation_id = str(manifest["generation_id"])
    provider._vector_storage_dir = provider._storage_dir
    return manifest


def _same_embedding_space(left: GenerationIdentity, right: GenerationIdentity) -> bool:
    try:
        validate_generation_compatibility(left.canonical(), right)
    except GenerationCompatibilityError:
        return False
    return True


def _open_sqlite_vector_store(provider: Any, *, table_name: str, dimensions: int, metric: str) -> None:
    storage_root = Path(getattr(provider, "_vector_storage_dir", None) or provider._storage_dir)
    temp_store = build_vector_store(
        "sqlite-bruteforce",
        storage_dir=storage_root,
        table_name=table_name,
        dimensions=dimensions,
        metric=metric,
    )
    temp_store.open()
    provider._vector_store = temp_store
    provider._vector_backend = "sqlite-bruteforce"


def _open_vector_store(provider: Any, *, dimensions: int, allow_backend_fallback: bool = True) -> None:
    if provider._storage_dir is None:
        raise RuntimeError("storage not initialized")
    storage_root = Path(getattr(provider, "_vector_storage_dir", None) or provider._storage_dir)
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

    fallback_backend = _normalize_vector_backend((provider._vector_config or {}).get("fallback_backend") or "")

    if backend == "pgvector":
        temp_store = build_vector_store(
            backend,
            storage_dir=storage_root,
            table_name=table_name,
            dimensions=dimensions,
            metric=metric,
            config=provider._vector_config or {},
        )
        if not temp_store.is_available():
            if allow_backend_fallback and fallback_backend == "sqlite-bruteforce":
                message = "pgvector unavailable or not configured; using sqlite-bruteforce fallback"
                message = _append_vector_message(provider, message)
                logger.warning("Scope Recall vector backend fallback: %s", message)
                _open_sqlite_vector_store(provider, table_name=table_name, dimensions=dimensions, metric=metric)
                return
            raise RuntimeError("pgvector dependencies or DSN are not configured")
        temp_store.open()
        provider._vector_backend = "pgvector"
        provider._vector_store = temp_store
        return

    if backend != "lancedb":
        raise RuntimeError(f"unsupported backend {backend}")

    vector_dir = storage_root / "lancedb"
    temp_store = LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if not temp_store.is_available():
        if allow_backend_fallback and fallback_backend == "sqlite-bruteforce":
            status = native_vector_dependency_status()
            detail = _native_dependency_detail(status)
            message = f"lancedb unavailable or unsafe ({detail}); using sqlite-bruteforce fallback"
            message = _append_vector_message(provider, message)
            logger.warning("Scope Recall vector backend fallback: %s", message)
            _open_sqlite_vector_store(provider, table_name=table_name, dimensions=dimensions, metric=metric)
            return
        status = native_vector_dependency_status()
        detail = _native_dependency_detail(status)
        raise RuntimeError(f"lancedb/pyarrow is not installed or unsafe ({detail})")
    try:
        temp_store.open()
    except VectorStoreCompatibilityError:
        temp_store.close()
        raise
    provider._vector_backend = "lancedb"
    provider._vector_store = temp_store



def setup_vector_layer(provider: Any) -> None:
    """Initialize the configured vector companion for a provider instance.

    Setup records readiness and repair status without blocking SQLite-only operation when vector support is intentionally disabled."""
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
    provider._vector_generation_id = ""
    provider._vector_storage_dir = provider._storage_dir
    if not provider._vector_enabled:
        return
    if provider._storage_dir is None:
        provider._vector_status = "error"
        provider._vector_message = "storage not initialized"
        return

    embedder_cfg = dict((provider._vector_config or {}).get("embedder") or {})
    fallback_cfg = dict((provider._vector_config or {}).get("fallback_embedder") or {})

    primary_embedder = build_embedder(embedder_cfg)
    primary_identity = _configured_generation_identity(provider, primary_embedder, embedder_cfg)
    provider._embedder = primary_embedder
    try:
        existing_manifest = _select_generation_storage(provider, primary_identity)
    except Exception as exc:
        _mark_vector_startup_degraded(provider, exc)
        return

    if not primary_embedder.is_available() and fallback_cfg:
        fallback_embedder = build_embedder(fallback_cfg)
        fallback_identity = _configured_generation_identity(provider, fallback_embedder, fallback_cfg)
        if fallback_embedder.is_available() and _same_embedding_space(primary_identity, fallback_identity):
            provider._embedder = fallback_embedder
            provider._vector_message = "primary embedder unavailable; using an explicitly compatible endpoint for the same embedding space"
        elif fallback_embedder.is_available():
            provider._vector_message = (
                f"primary embedder {primary_identity.model} unavailable; fallback {fallback_identity.model} "
                "is a different embedding space and was not allowed to access the current generation"
            )

    if not provider._embedder.is_available():
        provider._vector_status = "degraded"
        provider._vector_message = provider._vector_message or f"embedder {provider._embedder.provider} unavailable"
        return
    model_or_raise = getattr(provider._embedder, "_model_or_raise", None)
    if provider._embedder.provider == "sentence-transformers" and callable(model_or_raise):
        try:
            model_or_raise()
        except Exception as exc:
            _mark_vector_startup_degraded(provider, exc)
            provider._vector_store = None
            return

    try:
        _open_vector_store(
            provider,
            dimensions=provider._embedder.dimensions,
            allow_backend_fallback=existing_manifest is None,
        )
        opened_identity = replace(
            primary_identity,
            backend=_normalize_vector_backend(provider._vector_backend),
        )
        if existing_manifest is None:
            _register_open_legacy_generation(provider, opened_identity)
        else:
            validate_generation_compatibility(existing_manifest, opened_identity)
        # Startup sync may reconcile missing/stale rows inside the already
        # compatible generation, but it must never rebuild its schema or
        # embedding space.
        provider._vector_row_count = sync_vector_index(provider)
        refresh_vector_audit(provider)
        replay_result = replay_vector_outbox(provider)
        if replay_result["completed"]:
            provider._vector_message = f"replayed {replay_result['completed']} durable vector event(s)"
    except Exception as exc:
        _mark_vector_startup_degraded(provider, exc)
        if provider._vector_store is not None:
            try:
                provider._vector_store.close()
            except Exception:
                pass
        provider._vector_store = None
        return

    provider._vector_ready = True
    provider._vector_status = "ready"
    if not provider._vector_message:
        provider._vector_message = ""


def vector_delete_intent_required(provider: Any) -> bool:
    """Return whether hard delete must persist a vector outbox intent.

    Merely configuring a vector backend does not mean a companion generation
    has ever existed. A fresh runtime can be ``degraded`` before opening any
    store (for example, when its primary embedder credentials are absent). In
    that state there is no vector row to delete and therefore no generation to
    key a durable event to.

    Existing stores, active generation state, and ambiguous repair/error states
    remain fail-closed. The SQLite lookup also covers a runtime where vectors
    are currently disabled and setup did not hydrate ``_vector_generation_id``.
    """

    if getattr(provider, "_vector_store", None) is not None:
        return True
    if str(getattr(provider, "_vector_generation_id", "") or "").strip():
        return True

    try:
        conn = provider._require_conn()
        state_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generation_state'"
        ).fetchone()
        if state_table is not None:
            row = conn.execute(
                "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
            ).fetchone()
            if row is not None and str(row[0] or "").strip():
                return True
    except Exception:
        # An unreadable truth boundary is not evidence that no companion exists.
        return True

    status = str(getattr(provider, "_vector_status", "") or "").strip().lower()
    enabled = bool(getattr(provider, "_vector_enabled", False))
    if status == "degraded":
        return False
    if not enabled and status in {"", "disabled"}:
        return False
    return status not in {"disabled", "degraded"}


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


def _should_index_row(provider: Any, target: str, metadata: dict[str, Any] | str | None) -> bool:
    """Apply one lifecycle/target contract to every vector mutation path."""

    payload = load_metadata(metadata or {})
    lifecycle = str(payload.get("lifecycle") or "")
    return _should_index_target(provider, target) and ordinary_recall_lifecycle_visible(
        lifecycle=lifecycle,
        target=str(target),
    )


def sync_vector_index(provider: Any) -> int:
    """Synchronize vector companion rows for a bounded set of SQLite memories.

    Sync is rebuildable work: it should report failures clearly and never mutate the underlying memory text."""
    if not provider._vector_store or not provider._embedder:
        return 0
    conn = provider._require_conn()
    with provider._lock:
        rows = conn.execute(
            f"SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories m WHERE {ordinary_recall_lifecycle_visible_sql('m')} ORDER BY updated_at ASC"
        ).fetchall()
    with _vector_mutation_lock(provider):
        if not rows:
            # No visible SQLite rows means any remaining vector records are
            # stale companion data under the same lifecycle filter.
            existing = provider._vector_store.list_ids()
            if existing:
                provider._vector_store.delete_by_ids(existing)
            refresh_vector_audit(provider)
            return 0

        desired = {
            str(row["id"]): row
            for row in rows
            if _should_index_row(provider, str(row["target"]), row["metadata"])
        }
        existing_records = provider._vector_store.list_records()
        existing_ids = set(existing_records.keys())
        desired_ids = set(desired.keys())

        audit = refresh_vector_audit(provider)
        duplicate_rows = int(audit.get("duplicate_rows") or 0)
        if duplicate_rows > 0:
            raise RuntimeError(
                f"active vector generation has {duplicate_rows} duplicate row(s); "
                "build an explicit shadow generation instead of rewriting the active table during startup"
            )
        stale_ids = sorted(existing_ids - desired_ids)
        if stale_ids:
            provider._vector_store.delete_by_ids(stale_ids)

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



def _enqueue_vector_repair_event(
    provider: Any,
    *,
    memory_id: str,
    operation: str,
    updated_at: str,
    reason: str,
) -> bool:
    """Persist idempotent replay intent without copying memory content into the outbox."""

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id:
        return False
    material = "\x1f".join((generation_id, memory_id, operation, str(updated_at or "")))
    event_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    try:
        conn = provider._require_conn()
        with provider._lock:
            enqueue_vector_event(
                conn,
                event_key=event_key,
                generation_id=generation_id,
                memory_id=memory_id,
                operation=operation,
                payload={"updated_at": str(updated_at or ""), "reason": sanitize_report_text(reason)[:500]},
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("Scope Recall could not persist vector replay event for %s: %s", memory_id, exc)
        return False


def replay_vector_outbox(provider: Any, *, limit: int = 200) -> dict[str, int]:
    """Replay durable events one-by-one into the current compatible generation.

    Claiming a single event at a time prevents a failed first mutation from
    stranding an entire claimed batch in ``processing`` until its lease expires.
    """

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id or not provider._vector_store or not provider._embedder:
        return {"claimed": 0, "completed": 0, "failed": 0}
    conn = provider._require_conn()
    worker_id = f"runtime-{uuid.uuid4().hex}"
    max_events = max(1, int(limit))
    claimed = 0
    completed = 0
    failed = 0
    while claimed < max_events:
        with provider._lock:
            events = claim_vector_events(
                conn,
                generation_id=generation_id,
                worker_id=worker_id,
                limit=1,
            )
            conn.commit()
        if not events:
            break
        event = events[0]
        claimed += 1
        try:
            with provider._lock:
                row = conn.execute(
                    "SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories WHERE id = ?",
                    (event["memory_id"],),
                ).fetchone()
            should_delete = str(event["operation"] or "") == "delete" or row is None
            if row is not None:
                should_delete = should_delete or not _should_index_row(
                    provider,
                    str(row["target"] or ""),
                    row["metadata"],
                )
            with _vector_mutation_lock(provider):
                if should_delete:
                    provider._vector_store.delete_by_ids([str(event["memory_id"])])
                else:
                    assert row is not None
                    vector = provider._embedder.embed(provider._vector_text(row["summary"], row["content"]))
                    provider._vector_store.upsert_records(
                        [
                            {
                                "id": str(row["id"]),
                                "scope_id": str(row["scope_id"] or provider._scope_id),
                                "source": str(row["source"] or ""),
                                "target": str(row["target"] or ""),
                                "content": str(row["content"] or ""),
                                "summary": str(row["summary"] or ""),
                                "updated_at": str(row["updated_at"] or ""),
                                "vector": vector,
                            }
                        ]
                    )
            with provider._lock:
                complete_vector_event(conn, int(event["id"]), worker_id=worker_id)
                conn.commit()
            completed += 1
        except Exception as exc:
            safe_error = sanitize_report_text(str(exc))
            with provider._lock:
                try:
                    fail_vector_event(conn, int(event["id"]), worker_id=worker_id, error=safe_error)
                    conn.commit()
                except Exception:
                    conn.rollback()
            failed += 1
            mark_vector_needs_repair(provider, safe_error)
            logger.warning("Scope Recall vector replay failed for %s: %s", event["memory_id"], safe_error)
            break
    if provider._vector_store is not None:
        refresh_vector_audit(provider)
    return {"claimed": claimed, "completed": completed, "failed": failed}


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
    """Upsert one vector companion record for a SQLite memory row.

    Failures mark vector repair-needed status so callers do not mistake a companion write failure for durable storage success."""
    resolved_metadata = metadata
    if resolved_metadata is None:
        try:
            row = provider._require_conn().execute("SELECT metadata FROM memories WHERE id = ?", (id,)).fetchone()
            if row is not None:
                resolved_metadata = row["metadata"]
        except Exception:
            resolved_metadata = None
    operation = "upsert" if _should_index_row(provider, target, resolved_metadata) else "delete"
    with _vector_mutation_lock(provider):
        if not provider._vector_ready or not provider._vector_store or not provider._embedder:
            _enqueue_vector_repair_event(
                provider,
                memory_id=id,
                operation=operation,
                updated_at=updated_at,
                reason=str(getattr(provider, "_vector_message", "") or "vector companion unavailable"),
            )
            return
        if operation == "delete":
            try:
                provider._vector_store.delete_by_ids([id])
                refresh_vector_audit(provider)
            except Exception as exc:
                _enqueue_vector_repair_event(
                    provider,
                    memory_id=id,
                    operation="delete",
                    updated_at=updated_at,
                    reason=str(exc),
                )
                mark_vector_needs_repair(provider, exc)
                logger.warning("Scope Recall vector lifecycle cleanup failed; SQLite truth row preserved and replay was queued: %s", exc)
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
            _enqueue_vector_repair_event(
                provider,
                memory_id=id,
                operation="upsert",
                updated_at=updated_at,
                reason=str(exc),
            )
            mark_vector_needs_repair(provider, exc)
            logger.warning("Scope Recall vector upsert failed; SQLite truth row preserved and replay was queued: %s", exc)
