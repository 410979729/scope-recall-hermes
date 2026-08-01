"""Runtime setup and mutation helpers for vector companions.

Vector failures should mark repair-needed state and never silently delete or rewrite SQLite truth."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
from typing import Any, Sequence

from .capture_filters import sanitize_report_text
from .embedders import build_embedder
from .gating import config_bool
from .graph import load_metadata
from .lifecycle_policy import ordinary_recall_lifecycle_visible, ordinary_recall_lifecycle_visible_sql
from .vector_bootstrap import bootstrap_fresh_vector_companion
from .vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    current_generation,
    enqueue_vector_event,
    ensure_vector_generation_schema,
    prune_completed_vector_outbox,
    resolve_generation_storage_root,
    update_generation_cardinality,
    validate_generation_compatibility,
)
from .vector_outbox_replay import replay_committed_vector_events
from .vector_reconciliation import (
    prepare_vector_reconciliation_page,
    vector_outbox_backlog_status,
    vector_reconciliation_state,
)
from .vector_mutation_guard import vector_mutation_guard
from .vector_store import (
    LanceVectorStore,
    VectorStoreCompatibilityError,
    build_vector_store,
    native_vector_dependency_status,
    normalize_vector_backend,
)

logger = logging.getLogger(__name__)


def queued_vector_outbox_receipt(memory_ids: Sequence[str]) -> dict[str, Any]:
    """Return a stable receipt for committed causal companion intents."""

    ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
    return {
        "status": "queued" if ids else "not_needed",
        "executor": "vector_outbox",
        "requested": len(ids),
        "deleted": 0,
    }


def _vector_mutation_lock(provider: Any) -> AbstractContextManager[Any]:
    """Serialize physical mutations across provider threads and processes."""

    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        db_path = getattr(provider, "_db_path", None)
        storage_dir = Path(db_path).parent if db_path else None
    # A path guard already serializes both threads and processes.  Retain the
    # legacy provider lock only for ad-hoc runtimes that do not expose storage.
    thread_lock = (
        None
        if storage_dir is not None
        else getattr(provider, "_vector_lock", None)
        or getattr(provider, "_lock", None)
    )
    return vector_mutation_guard(thread_lock=thread_lock, storage_dir=storage_dir)


_VECTOR_STATUS_MESSAGE_LIMIT = 300
_NATIVE_DEPENDENCY_DETAIL_LIMIT = 160
_DEFAULT_STARTUP_RECONCILE_PAGE_SIZE = 200
_DEFAULT_STARTUP_RECONCILE_INTERVAL_SECONDS = 86_400


def _bounded_config_int(
    config: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Read one bounded integer without letting malformed config break startup."""

    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def vector_write_replay_limit(provider: Any) -> int:
    """Return the bounded batch drained after each committed vector intent.

    Replaying one event per write can preserve an old backlog forever because
    each write also enqueues one event.  A small default batch lets ordinary
    traffic converge while retaining a strict upper bound.
    """

    config = getattr(provider, "_vector_config", {})
    if not isinstance(config, dict):
        config = {}
    return _bounded_config_int(
        config,
        "write_outbox_replay_limit",
        default=20,
        minimum=1,
        maximum=2000,
    )


def _prune_completed_outbox(
    provider: Any,
    conn: Any,
    *,
    retention_days: int,
    keep_per_generation: int,
) -> dict[str, Any]:
    """Run one isolated terminal-event retention transaction."""

    with provider._lock:
        conn.execute("SAVEPOINT scope_recall_vector_outbox_retention")
        try:
            receipt = prune_completed_vector_outbox(
                conn,
                retention_days=retention_days,
                keep_per_generation=keep_per_generation,
            )
            conn.execute("RELEASE SAVEPOINT scope_recall_vector_outbox_retention")
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT scope_recall_vector_outbox_retention")
            conn.execute("RELEASE SAVEPOINT scope_recall_vector_outbox_retention")
            safe_error = _sanitize_vector_message(exc)
            logger.warning("Scope Recall vector outbox retention failed: %s", safe_error)
            return {
                "status": "failed",
                "enabled": retention_days > 0,
                "deleted": 0,
                "error": safe_error,
            }
    return {
        "status": "pruned" if int(receipt.get("deleted") or 0) else "unchanged",
        **receipt,
    }


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


def _open_sqlite_vector_store(provider: Any, *, table_name: str, dimensions: int, metric: str) -> None:
    storage_root = Path(getattr(provider, "_vector_storage_dir", None) or provider._storage_dir)
    temp_store = build_vector_store(
        "sqlite-bruteforce",
        storage_dir=storage_root,
        table_name=table_name,
        dimensions=dimensions,
        metric=metric,
    )
    temp_store.open_existing_for_update()
    provider._vector_store = temp_store
    provider._vector_backend = "sqlite-bruteforce"


def _open_vector_store(provider: Any, *, dimensions: int) -> None:
    """Open only the physical store named by the active generation.

    Backend fallback is a fresh-bootstrap decision. Once a manifest is active,
    switching backend here would cross an embedding-space boundary and is
    therefore forbidden.
    """
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
            raise RuntimeError("pgvector dependencies or DSN are not configured")
        temp_store.open_existing_for_update()
        provider._vector_backend = "pgvector"
        provider._vector_store = temp_store
        return

    if backend != "lancedb":
        raise RuntimeError(f"unsupported backend {backend}")

    vector_dir = storage_root / "lancedb"
    temp_store = LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if not temp_store.is_available():
        status = native_vector_dependency_status()
        detail = _native_dependency_detail(status)
        raise RuntimeError(f"lancedb/pyarrow is not installed or unsafe ({detail})")
    try:
        temp_store.open_existing_for_update()
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
    provider._vector_reconciliation = None
    provider._vector_storage_dir = provider._storage_dir
    if not provider._vector_enabled:
        return
    if provider._storage_dir is None:
        provider._vector_status = "error"
        provider._vector_message = "storage not initialized"
        return

    conn = provider._require_conn()
    try:
        manifest_hint = current_generation(conn)
    except Exception as exc:
        _mark_vector_startup_degraded(provider, exc)
        return
    if manifest_hint is None:
        runtime_config = getattr(provider, "_config", None)
        if not isinstance(runtime_config, dict):
            runtime_config = {
                "vector": dict(provider._vector_config or {}),
                "retrieval": dict(provider._retrieval_config or {}),
            }
        try:
            bootstrap_receipt = bootstrap_fresh_vector_companion(
                Path(provider._storage_dir),
                runtime_config,
                truth_conn=conn,
            )
        except Exception as exc:
            _mark_vector_startup_degraded(provider, exc)
            return
        bootstrap_status = str(bootstrap_receipt.get("status") or "")
        if bootstrap_status not in {"ready", "existing"}:
            reason = str(bootstrap_receipt.get("reason") or "bootstrap unavailable")
            _mark_vector_startup_degraded(
                provider,
                f"vector generation bootstrap unavailable: {reason}",
            )
            return
        if bootstrap_status == "ready":
            selection = str(bootstrap_receipt.get("selection") or "primary")
            _append_vector_message(
                provider,
                f"initialized fresh generation with {selection}",
            )
        manifest_hint = current_generation(conn)
        if manifest_hint is None:
            _mark_vector_startup_degraded(
                provider,
                "vector bootstrap returned ready without an active generation manifest",
            )
            return

    embedder_cfg = dict((provider._vector_config or {}).get("embedder") or {})
    fallback_cfg = dict((provider._vector_config or {}).get("fallback_embedder") or {})
    manifest_backend = _normalize_vector_backend(manifest_hint.get("backend") or "")
    candidate_specs = [("primary", embedder_cfg)]
    if fallback_cfg:
        candidate_specs.append(("fallback", fallback_cfg))

    selected_embedder: Any | None = None
    selected_identity: GenerationIdentity | None = None
    candidate_failures: list[str] = []
    fallback_incompatible = False
    for label, candidate_config in candidate_specs:
        try:
            candidate_embedder = build_embedder(candidate_config)
        except Exception as exc:
            candidate_failures.append(
                f"{label}_embedder_build_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        try:
            available = bool(candidate_embedder.is_available())
        except Exception as exc:
            candidate_failures.append(
                f"{label}_embedder_probe_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        if not available:
            candidate_failures.append(f"{label}_embedder_unavailable")
            continue
        try:
            provisional_identity = _configured_generation_identity(
                provider,
                candidate_embedder,
                candidate_config,
            )
        except Exception as exc:
            candidate_failures.append(
                f"{label}_embedder_identity_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        # Readiness can discover a different dimension, but provider/model and
        # prompt-space fields are stable configuration.  Skip an obviously
        # different space before loading or downloading an unused model.
        manifest_dimensions = int(
            manifest_hint.get("dimensions") or provisional_identity.dimensions
        )
        try:
            validate_generation_compatibility(
                manifest_hint,
                replace(
                    provisional_identity,
                    backend=manifest_backend,
                    dimensions=manifest_dimensions,
                ),
            )
        except GenerationCompatibilityError as exc:
            candidate_failures.append(
                f"{label}_embedding_space_incompatible:"
                f"{_sanitize_vector_message(exc)}"
            )
            fallback_incompatible = fallback_incompatible or label == "fallback"
            continue
        try:
            candidate_embedder.probe_readiness()
        except Exception as exc:
            candidate_failures.append(
                f"{label}_embedder_readiness_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        try:
            candidate_identity = _configured_generation_identity(
                provider,
                candidate_embedder,
                candidate_config,
            )
        except Exception as exc:
            candidate_failures.append(
                f"{label}_embedder_identity_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        try:
            validate_generation_compatibility(
                manifest_hint,
                replace(candidate_identity, backend=manifest_backend),
            )
        except GenerationCompatibilityError as exc:
            candidate_failures.append(
                f"{label}_embedding_space_incompatible:"
                f"{_sanitize_vector_message(exc)}"
            )
            fallback_incompatible = fallback_incompatible or label == "fallback"
            continue
        selected_embedder = candidate_embedder
        selected_identity = candidate_identity
        if label == "fallback" and candidate_failures:
            _append_vector_message(
                provider,
                "primary embedder unavailable or incompatible; "
                "using compatible fallback embedder",
            )
        break

    if selected_embedder is None or selected_identity is None:
        if fallback_incompatible:
            reason = (
                "configured fallback is a different embedding space and was not "
                "allowed to access the current generation"
            )
        else:
            reason = "no ready configured embedder matches the active vector generation"
        if candidate_failures:
            reason = f"{reason}: {';'.join(candidate_failures)}"
        _mark_vector_startup_degraded(provider, reason)
        return

    provider._embedder = selected_embedder
    try:
        existing_manifest = _select_generation_storage(provider, selected_identity)
    except Exception as exc:
        _mark_vector_startup_degraded(provider, exc)
        return

    if existing_manifest is None:
        _mark_vector_startup_degraded(
            provider,
            "active vector generation disappeared before physical open",
        )
        return

    try:
        _open_vector_store(
            provider,
            dimensions=provider._embedder.dimensions,
        )
        opened_identity = replace(
            selected_identity,
            backend=_normalize_vector_backend(provider._vector_backend),
        )
        validate_generation_compatibility(existing_manifest, opened_identity)
        provider._vector_row_count = int(existing_manifest.get("row_count") or 0)
        provider._vector_unique_id_count = int(
            existing_manifest.get("unique_id_count") or 0
        )
        provider._vector_duplicate_row_count = 0
        _refresh_vector_row_count_only(provider)
        # Ordinary startup is outbox-first and strictly bounded.  Full stale-row
        # sweeps remain explicit repair/doctor work; they are never hidden here.
        reconciliation = run_bounded_vector_reconciliation(provider)
        provider._vector_reconciliation = reconciliation
        if int(reconciliation.get("failed") or 0) > 0:
            raise RuntimeError("bounded vector outbox replay failed during startup")
        if int(reconciliation.get("dead_letter") or 0) > 0:
            raise RuntimeError(
                "vector outbox contains dead-lettered startup debt; inspect with "
                "python scripts/requeue.vector_dead_letter.py --dry-run"
            )
        replayed = int(reconciliation.get("completed") or 0)
        planned = int(reconciliation.get("planned") or 0)
        if replayed or planned:
            _append_vector_message(
                provider,
                f"bounded vector startup planned {planned} and replayed {replayed} event(s)",
            )
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


def _persist_vector_cardinality(
    provider: Any,
    *,
    physical_rows: int,
    unique_ids: int,
) -> None:
    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id:
        return
    conn = provider._require_conn()
    with provider._lock:
        owns_transaction = not bool(getattr(conn, "in_transaction", False))
        changed = update_generation_cardinality(
            conn,
            generation_id=generation_id,
            row_count=physical_rows,
            unique_id_count=unique_ids,
        )
        if changed and owns_transaction:
            conn.commit()


def refresh_vector_audit(provider: Any) -> dict[str, int]:
    with _vector_mutation_lock(provider):
        if not provider._vector_store:
            counts = {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0, "duplicate_ids": 0}
        else:
            counts = provider._vector_store.audit_counts()
    provider._vector_row_count = int(counts.get("physical_rows") or 0)
    provider._vector_unique_id_count = int(counts.get("unique_ids") or 0)
    provider._vector_duplicate_row_count = int(counts.get("duplicate_rows") or 0)
    _persist_vector_cardinality(
        provider,
        physical_rows=provider._vector_row_count,
        unique_ids=provider._vector_unique_id_count,
    )
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


def _refresh_vector_row_count_only(provider: Any) -> None:
    """Refresh only the backend's bounded physical-row counter.

    Unlike ``refresh_vector_audit`` this never enumerates ids or records and does
    not attempt duplicate discovery.  It keeps status counters accurate after a
    bounded outbox replay without reintroducing cardinality-bound startup work.
    """

    store = getattr(provider, "_vector_store", None)
    counter = getattr(store, "count_rows", None) if store is not None else None
    if not callable(counter):
        return
    with _vector_mutation_lock(provider):
        value = counter()
    if isinstance(value, (int, str)):
        provider._vector_row_count = int(value)
        duplicate_rows = max(0, int(getattr(provider, "_vector_duplicate_row_count", 0) or 0))
        provider._vector_unique_id_count = max(0, provider._vector_row_count - duplicate_rows)
        _persist_vector_cardinality(
            provider,
            physical_rows=provider._vector_row_count,
            unique_ids=provider._vector_unique_id_count,
        )


def run_bounded_vector_reconciliation(provider: Any) -> dict[str, Any]:
    """Serialize the complete bounded truth/outbox/physical reconciliation."""

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id or not provider._vector_store or not provider._embedder:
        return {
            "status": "unavailable",
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "planned": 0,
            "replayable": 0,
            "dead_letter": 0,
        }
    with _vector_mutation_lock(provider):
        return _run_bounded_vector_reconciliation_guarded(provider)


def _run_bounded_vector_reconciliation_guarded(provider: Any) -> dict[str, Any]:
    """Replay debt first, then plan and replay at most one truth page.

    The startup/background contract is intentionally independent of total truth
    or physical-vector cardinality.  A full stale-row/duplicate sweep remains an
    explicit operator repair action via :func:`sync_vector_index` and doctor.
    """

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id or not provider._vector_store or not provider._embedder:
        return {
            "status": "unavailable",
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "planned": 0,
            "replayable": 0,
            "dead_letter": 0,
        }
    config = dict(provider._vector_config or {})
    page_size = _bounded_config_int(
        config,
        "startup_reconcile_page_size",
        default=_DEFAULT_STARTUP_RECONCILE_PAGE_SIZE,
        minimum=1,
        maximum=2000,
    )
    outbox_limit = _bounded_config_int(
        config,
        "startup_outbox_limit",
        default=page_size,
        minimum=1,
        maximum=2000,
    )
    interval_seconds = _bounded_config_int(
        config,
        "startup_reconcile_interval_seconds",
        default=_DEFAULT_STARTUP_RECONCILE_INTERVAL_SECONDS,
        minimum=60,
        maximum=31_536_000,
    )
    retention_days = _bounded_config_int(
        config,
        "outbox_completed_retention_days",
        default=30,
        minimum=0,
        maximum=3650,
    )
    keep_per_generation = _bounded_config_int(
        config,
        "outbox_completed_keep_per_generation",
        default=5000,
        minimum=0,
        maximum=1_000_000,
    )
    first = replay_vector_outbox(
        provider,
        limit=outbox_limit,
        refresh_audit_after=False,
    )
    if int(first.get("completed") or 0) > 0:
        _refresh_vector_row_count_only(provider)
    conn = provider._require_conn()
    with provider._lock:
        backlog = vector_outbox_backlog_status(
            conn,
            generation_id=generation_id,
        )
    result: dict[str, Any] = {
        "status": "outbox_pending" if backlog["replayable"] else "ready",
        "claimed": int(first["claimed"]),
        "completed": int(first["completed"]),
        "failed": int(first["failed"]),
        "planned": 0,
        **backlog,
    }
    if result["failed"] or backlog["dead_letter"] or backlog["replayable"]:
        result["watermark"] = vector_reconciliation_state(
            conn,
            generation_id=generation_id,
        )
        result["outbox_retention"] = {
            "status": "deferred",
            "reason": "nonterminal_backlog",
            "deleted": 0,
        }
        return result

    with provider._lock:
        page = prepare_vector_reconciliation_page(
            conn,
            generation_id=generation_id,
            should_index_row=lambda target, metadata: _should_index_row(
                provider, target, metadata
            ),
            page_size=page_size,
            interval_seconds=interval_seconds,
        )
    result["planned"] = int(page.get("planned") or 0)
    result["page_status"] = str(page.get("status") or "")
    if result["planned"]:
        second = replay_vector_outbox(
            provider,
            limit=min(outbox_limit, result["planned"]),
            refresh_audit_after=False,
        )
        result["claimed"] += int(second["claimed"])
        result["completed"] += int(second["completed"])
        result["failed"] += int(second["failed"])
        if int(second.get("completed") or 0) > 0:
            _refresh_vector_row_count_only(provider)
    with provider._lock:
        remaining = vector_outbox_backlog_status(
            conn,
            generation_id=generation_id,
        )
        result.update(remaining)
        result["watermark"] = vector_reconciliation_state(
            conn,
            generation_id=generation_id,
        )
    result["status"] = (
        "failed"
        if result["failed"] or result["dead_letter"]
        else ("outbox_pending" if result["replayable"] else str(page.get("status") or "ready"))
    )
    if result["failed"] or result["dead_letter"] or result["replayable"]:
        result["outbox_retention"] = {
            "status": "deferred",
            "reason": "nonterminal_backlog",
            "deleted": 0,
        }
    else:
        result["outbox_retention"] = _prune_completed_outbox(
            provider,
            conn,
            retention_days=retention_days,
            keep_per_generation=keep_per_generation,
        )
    return result


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



def enqueue_vector_repair_event(
    provider: Any,
    *,
    memory_id: str,
    operation: str,
    updated_at: str,
    reason: str,
    commit: bool = True,
    raise_on_error: bool = False,
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
            if commit:
                conn.commit()
        return True
    except Exception as exc:
        if raise_on_error:
            raise
        logger.warning("Scope Recall could not persist vector replay event for %s: %s", memory_id, exc)
        return False


def replay_vector_outbox(
    provider: Any,
    *,
    limit: int = 200,
    refresh_audit_after: bool = True,
) -> dict[str, int]:
    """Serialize truth outbox claims, schema bookkeeping, and physical replay."""

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id or not provider._vector_store or not provider._embedder:
        return {"claimed": 0, "completed": 0, "failed": 0}
    with _vector_mutation_lock(provider):
        return _replay_vector_outbox_guarded(
            provider,
            limit=limit,
            refresh_audit_after=refresh_audit_after,
        )


def _replay_vector_outbox_guarded(
    provider: Any,
    *,
    limit: int = 200,
    refresh_audit_after: bool = True,
) -> dict[str, int]:
    """Replay committed intent through the shared single-writer executor."""

    generation_id = str(getattr(provider, "_vector_generation_id", "") or "")
    if not generation_id or not provider._vector_store or not provider._embedder:
        return {"claimed": 0, "completed": 0, "failed": 0}

    def on_failure(error: str) -> None:
        mark_vector_needs_repair(provider, error)
        logger.warning("Scope Recall vector replay failed: %s", error)

    return replay_committed_vector_events(
        provider._require_conn(),
        generation_id=generation_id,
        vector_store=provider._vector_store,
        embedder=provider._embedder,
        vector_text=getattr(
            provider,
            "_vector_text",
            lambda summary, content: f"{summary}\n{content}".strip(),
        ),
        should_index_row=lambda target, metadata: _should_index_row(
            provider, target, metadata
        ),
        default_scope_id=str(getattr(provider, "_scope_id", "") or ""),
        db_lock=getattr(provider, "_lock", None),
        mutation_context=lambda: _vector_mutation_lock(provider),
        limit=limit,
        on_failure=on_failure,
        after_replay=(
            (lambda: refresh_vector_audit(provider))
            if refresh_audit_after
            else None
        ),
    )


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
    """Compatibility trigger that enqueues and replays committed truth intent.

    The payload arguments are retained for callers compiled against older
    versions, but physical companion content is always re-read from SQLite by
    the shared outbox executor.  This function must never write the backend
    directly.
    """

    del source, content, summary, scope_id
    resolved_metadata = metadata
    if resolved_metadata is None:
        try:
            row = provider._require_conn().execute(
                "SELECT metadata FROM memories WHERE id = ?", (id,)
            ).fetchone()
            if row is not None:
                resolved_metadata = row["metadata"]
        except Exception:
            resolved_metadata = None
    operation = (
        "upsert"
        if _should_index_row(provider, target, resolved_metadata)
        else "delete"
    )
    queued = enqueue_vector_repair_event(
        provider,
        memory_id=id,
        operation=operation,
        updated_at=updated_at,
        reason="compatibility trigger for committed SQLite truth",
        commit=True,
    )
    if not queued:
        mark_vector_needs_repair(
            provider,
            "vector intent could not be persisted to the current generation outbox",
        )
        return
    if provider._vector_ready and provider._vector_store and provider._embedder:
        replay_vector_outbox(provider, limit=vector_write_replay_limit(provider))
