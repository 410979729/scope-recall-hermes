"""Runtime setup and mutation helpers for vector companions.

Vector failures should mark repair-needed state and never silently delete or rewrite SQLite truth."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping, Sequence, cast

from ._internal.runtime.vector_runtime_state import bind_provider_vector_runtime
from .capture_filters import sanitize_report_text
from .embedders import build_embedder, close_embedder
from .embedding_validation import validate_embedding_batch, zip_embedding_rows
from .gating import config_bool
from .graph import load_metadata
from .lifecycle_policy import (
    ordinary_recall_lifecycle_visible,
    ordinary_recall_lifecycle_visible_sql,
)
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
from .vector_membership import (
    apply_membership_mutation,
    membership_is_ready,
    replace_generation_membership,
)
from .vector_outbox_replay import replay_committed_vector_events
from .vector_reconciliation import (
    prepare_vector_reconciliation_page,
    vector_outbox_backlog_status,
    vector_reconciliation_state,
)
from .vector_mutation_guard import vector_mutation_guard
from .truth_connection import probe_truth_database_connection
from .sqlite_recovery import is_sqlite_lock_contention, rollback_if_active
from .sqlite_params import chunked_sql_parameters
from .vector_store import (
    LanceVectorStore,
    VectorStoreCompatibilityError,
    build_vector_store,
    native_vector_dependency_status,
    normalize_vector_backend,
)
from .vector_status import normalize_vector_debt_counts, vector_status_contract

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

    storage_dir = bind_provider_vector_runtime(provider).storage_dir
    if storage_dir is None:
        db_path = bind_provider_vector_runtime(provider).db_path
        storage_dir = Path(db_path).parent if db_path else None
    # A path guard already serializes both threads and processes.  Retain the
    # legacy provider lock only for ad-hoc runtimes that do not expose storage.
    thread_lock = (
        None
        if storage_dir is not None
        else bind_provider_vector_runtime(provider).vector_lock
        or bind_provider_vector_runtime(provider).lock
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


def _startup_reconcile_enabled(provider: Any) -> bool:
    """Return whether bounded startup/background reconciliation may run."""

    config = getattr(bind_provider_vector_runtime(provider), "vector_config", None)
    if not isinstance(config, dict):
        config = {}
    return config_bool(config, "startup_reconcile_enabled", True)


def _empty_reconciliation_result(status: str, **extra: Any) -> dict[str, Any]:
    """Build one stable bounded-reconciliation receipt.

    A failed status always carries a positive failure count so count-based
    consumers cannot accidentally treat a fail-closed result as successful.
    """

    result: dict[str, Any] = {
        "status": status,
        "claimed": 0,
        "completed": 0,
        "failed": 1 if status == "failed" else 0,
        "planned": 0,
        "replayable": 0,
        "dead_letter": 0,
    }
    result.update(extra)
    return result


def _truth_header_preflight(provider: Any) -> dict[str, Any] | None:
    """Return a failed receipt when the live SQLite pager is unreadable."""

    require_conn = getattr(bind_provider_vector_runtime(provider), "require_conn", None)
    lock = getattr(bind_provider_vector_runtime(provider), "lock", None)
    if not callable(require_conn) or lock is None:
        # Unit doubles without a provider-owned pager are outside this runtime
        # preflight. Production providers always expose both boundaries.
        return None
    with lock:
        conn = cast(sqlite3.Connection, require_conn())
        probe = probe_truth_database_connection(conn)
    if probe.get("ok"):
        return None
    return _empty_reconciliation_result(
        "failed",
        error=str(probe.get("error") or "SQLite truth database is unreadable"),
        header_status=str(probe.get("status") or "corrupt_or_unreadable"),
        probe_method="sqlite_connection",
    )


def vector_write_replay_limit(provider: Any) -> int:
    """Return the bounded batch drained after each committed vector intent.

    Replaying one event per write can preserve an old backlog forever because
    each write also enqueues one event.  A small default batch lets ordinary
    traffic converge while retaining a strict upper bound.
    """

    config = getattr(bind_provider_vector_runtime(provider), "vector_config", {})
    if not isinstance(config, dict):
        config = {}
    return _bounded_config_int(
        config,
        "write_outbox_replay_limit",
        default=20,
        minimum=1,
        maximum=2000,
    )


_OUTBOX_RETENTION_CONTENTION_ESCALATION = 8


def _outbox_retention_due(provider: Any, *, interval_seconds: int) -> bool:
    """Minute-scale retention cadence gate.

    Retention is idempotent housekeeping: running it once per interval keeps
    ``vector_outbox`` bounded, while the previous run-on-every-idle-tick
    cadence collided with long journal digest transactions roughly once per
    conversation turn (issue #47).
    """

    now = time.monotonic()
    next_at = float(
        getattr(bind_provider_vector_runtime(provider), "next_outbox_retention_at", 0.0)
        or 0.0
    )
    if now < next_at:
        return False
    bind_provider_vector_runtime(provider).next_outbox_retention_at = now + max(
        60, int(interval_seconds)
    )
    return True


def _prune_completed_outbox(
    provider: Any,
    conn: Any,
    *,
    retention_days: int,
    keep_per_generation: int,
) -> dict[str, Any]:
    """Run one low-priority retention pass; coalesce instead of fighting locks.

    Lock contention is expected while another writer (typically a journal
    digest apply) is active. The pass skips quietly, pushes the next attempt a
    full interval away, and only escalates to WARNING after
    ``_OUTBOX_RETENTION_CONTENTION_ESCALATION`` consecutive skips, so real
    starvation stays visible without one warning per conversation turn.
    """

    max_attempts = 1
    for attempt in range(1, max_attempts + 1):
        failure: BaseException | None = None
        with bind_provider_vector_runtime(provider).lock:
            savepoint_started = False
            try:
                conn.execute("SAVEPOINT scope_recall_vector_outbox_retention")
                savepoint_started = True
                receipt = prune_completed_vector_outbox(
                    conn,
                    retention_days=retention_days,
                    keep_per_generation=keep_per_generation,
                )
                conn.execute("RELEASE SAVEPOINT scope_recall_vector_outbox_retention")
                bind_provider_vector_runtime(
                    provider
                ).outbox_retention_contention_skips = 0
                return {
                    "status": "pruned"
                    if int(receipt.get("deleted") or 0)
                    else "unchanged",
                    "attempts": attempt,
                    **receipt,
                }
            except Exception as exc:
                failure = exc
                cleanup_failed = False
                if savepoint_started:
                    try:
                        conn.execute(
                            "ROLLBACK TO SAVEPOINT scope_recall_vector_outbox_retention"
                        )
                    except Exception:
                        cleanup_failed = True
                    try:
                        conn.execute(
                            "RELEASE SAVEPOINT scope_recall_vector_outbox_retention"
                        )
                    except Exception:
                        cleanup_failed = True
                if cleanup_failed:
                    try:
                        rollback_if_active(conn)
                    except Exception:
                        pass
        assert failure is not None
        safe_error = _sanitize_vector_message(failure)
        if is_sqlite_lock_contention(failure):
            skips = (
                int(
                    getattr(
                        bind_provider_vector_runtime(provider),
                        "outbox_retention_contention_skips",
                        0,
                    )
                    or 0
                )
                + 1
            )
            bind_provider_vector_runtime(
                provider
            ).outbox_retention_contention_skips = skips
            if skips >= _OUTBOX_RETENTION_CONTENTION_ESCALATION:
                logger.warning(
                    "Scope Recall vector outbox retention has been skipped %d "
                    "consecutive times due to SQLite contention; a long-lived "
                    "writer may be starving housekeeping",
                    skips,
                )
            else:
                logger.debug(
                    "Scope Recall vector outbox retention skipped under "
                    "SQLite contention (%d consecutive)",
                    skips,
                )
            return {
                "status": "skipped_contention",
                "enabled": retention_days > 0,
                "deleted": 0,
                "attempts": attempt,
                "consecutive_skips": skips,
            }
        logger.warning("Scope Recall vector outbox retention failed: %s", safe_error)
        return {
            "status": "failed",
            "enabled": retention_days > 0,
            "deleted": 0,
            "attempts": attempt,
            "error": safe_error,
        }
    raise AssertionError("unreachable vector retention retry state")


def _sanitize_vector_message(
    value: Exception | str, *, limit: int = _VECTOR_STATUS_MESSAGE_LIMIT
) -> str:
    return sanitize_report_text(str(value))[:limit]


def _set_vector_status(
    provider: Any,
    *,
    state: str,
    reason_code: str,
    message: Exception | str = "",
    ready: bool | None = None,
    usable_for_query: bool | None = None,
    debt_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the single canonical vector status contract to a runtime."""

    contract = vector_status_contract(
        state=state,
        reason_code=reason_code,
        message=_sanitize_vector_message(message) if message else "",
        debt_counts=(
            debt_counts
            if debt_counts is not None
            else getattr(
                bind_provider_vector_runtime(provider), "vector_debt_counts", None
            )
        ),
        usable_for_query=usable_for_query,
    )
    if ready is not None:
        bind_provider_vector_runtime(provider).vector_ready = bool(ready)
    bind_provider_vector_runtime(provider).vector_status = contract["state"]
    bind_provider_vector_runtime(provider).vector_reason_code = contract["reason_code"]
    bind_provider_vector_runtime(provider).vector_auto_recoverable = contract[
        "auto_recoverable"
    ]
    bind_provider_vector_runtime(provider).vector_repair_required = contract[
        "repair_required"
    ]
    bind_provider_vector_runtime(provider).vector_usable_for_query = contract[
        "usable_for_query"
    ]
    bind_provider_vector_runtime(provider).vector_message = contract["message"]
    bind_provider_vector_runtime(provider).vector_debt_counts = contract["debt_counts"]
    return contract


def _refresh_vector_debt_counts(provider: Any) -> dict[str, int]:
    """Refresh cached aggregate outbox debt after a mutation/replay boundary."""

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if not generation_id:
        debt = normalize_vector_debt_counts(None)
        bind_provider_vector_runtime(provider).vector_debt_counts = debt
        return debt
    try:
        conn = bind_provider_vector_runtime(provider).require_conn()
        lock = getattr(bind_provider_vector_runtime(provider), "lock", None)
        if lock is None:
            debt = vector_outbox_backlog_status(conn, generation_id=generation_id)
        else:
            with lock:
                debt = vector_outbox_backlog_status(conn, generation_id=generation_id)
    except Exception:
        return normalize_vector_debt_counts(
            getattr(bind_provider_vector_runtime(provider), "vector_debt_counts", None)
        )
    normalized = normalize_vector_debt_counts(debt)
    bind_provider_vector_runtime(provider).vector_debt_counts = normalized
    return normalized


def mark_vector_needs_repair(
    provider: Any,
    exc: Exception | str,
    *,
    reason_code: str = "repair_required",
    usable_for_query: bool = False,
) -> None:
    _set_vector_status(
        provider,
        state="needs_repair",
        reason_code=reason_code,
        message=exc,
        ready=False,
        usable_for_query=usable_for_query,
    )


def _mark_vector_replay_degraded(provider: Any, exc: Exception | str) -> None:
    """Keep a live companion retryable when one durable outbox event fails."""

    bind_provider_vector_runtime(provider).vector_replay_degraded = True
    _set_vector_status(
        provider,
        state="degraded",
        reason_code="outbox_retryable",
        message=exc,
        usable_for_query=bool(
            getattr(bind_provider_vector_runtime(provider), "vector_ready", False)
        ),
    )


def mark_vector_replay_degraded(provider: Any, exc: Exception | str) -> None:
    """Public mutation-boundary helper for durable, retryable replay debt."""

    _mark_vector_replay_degraded(provider, exc)


def _recover_vector_replay_state(provider: Any, result: dict[str, int]) -> None:
    """Clear replay degradation only after the generation has no live outbox debt."""

    if not bool(
        getattr(bind_provider_vector_runtime(provider), "vector_replay_degraded", False)
    ):
        return
    if int(result.get("failed") or 0) or int(result.get("completed") or 0) < 1:
        return
    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if not generation_id:
        return
    debt = _refresh_vector_debt_counts(provider)
    if debt["dead_letter"]:
        mark_vector_needs_repair(
            provider,
            "vector outbox contains dead-letter debt",
            reason_code="outbox_dead_letter",
        )
        return
    if debt["replayable"]:
        return
    bind_provider_vector_runtime(provider).vector_replay_degraded = False
    if bool(getattr(bind_provider_vector_runtime(provider), "vector_ready", False)):
        _set_vector_status(
            provider,
            state="ready",
            reason_code="healthy",
            ready=True,
            usable_for_query=True,
            debt_counts=debt,
        )


def _mark_vector_startup_degraded(provider: Any, exc: Exception | str) -> None:
    """Preserve startup's public degraded status while bounding safe detail."""

    _set_vector_status(
        provider,
        state="degraded",
        reason_code="startup_unavailable",
        message=exc,
        ready=False,
        usable_for_query=False,
    )


def _mark_vector_startup_failure(provider: Any, exc: Exception) -> None:
    """Separate permanent generation incompatibility from transient startup loss."""

    if isinstance(exc, GenerationCompatibilityError):
        mark_vector_needs_repair(
            provider,
            exc,
            reason_code="identity_mismatch",
        )
    else:
        _mark_vector_startup_degraded(provider, exc)


def _normalize_vector_backend(value: Any) -> str:
    return normalize_vector_backend(value)


def _append_vector_message(provider: Any, message: str) -> str:
    """Append one bounded diagnostic and return the same safe text for logging."""

    safe_message = _sanitize_vector_message(message)
    current = _sanitize_vector_message(
        str(getattr(bind_provider_vector_runtime(provider), "vector_message", "") or "")
    )
    combined = f"{current}; {safe_message}" if current else safe_message
    bind_provider_vector_runtime(provider).vector_message = _sanitize_vector_message(
        combined
    )
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

    vector_config = dict(bind_provider_vector_runtime(provider).vector_config or {})
    resolved_embedder_config = dict(
        embedder_config or vector_config.get("embedder") or {}
    )
    return GenerationIdentity(
        backend=_normalize_vector_backend(
            vector_config.get("backend")
            or getattr(
                bind_provider_vector_runtime(provider), "vector_backend", "lancedb"
            )
        ),
        provider=str(
            embedder.provider or resolved_embedder_config.get("provider") or "unknown"
        ),
        model=str(embedder.model or resolved_embedder_config.get("model") or "unknown"),
        dimensions=int(embedder.dimensions),
        metric=str(
            (bind_provider_vector_runtime(provider).retrieval_config or {}).get(
                "metric"
            )
            or "cosine"
        ),
        prompt_profile=str(
            resolved_embedder_config.get("prompt_profile") or "default-v1"
        ),
        document_prefix=str(resolved_embedder_config.get("document_prefix") or ""),
        query_prefix=str(resolved_embedder_config.get("query_prefix") or ""),
        request_dimensions=bool(
            resolved_embedder_config.get("request_dimensions", False)
        ),
        table_name=str(vector_config.get("table_name") or "memories"),
    )


def _select_generation_storage(
    provider: Any, identity: GenerationIdentity
) -> dict[str, Any] | None:
    """Resolve the active generation without changing its immutable backend.

    A first startup may register an explicitly configured fallback backend. On
    later startups that manifest is authoritative: reopening it is safe, while
    silently switching back to the configured primary backend would access a
    different physical generation without migration/activation.
    """

    conn = bind_provider_vector_runtime(provider).require_conn()
    ensure_vector_generation_schema(conn)
    manifest = current_generation(conn)
    bind_provider_vector_runtime(provider).vector_generation_id = str(
        (manifest or {}).get("generation_id") or ""
    )
    if manifest is None:
        bind_provider_vector_runtime(
            provider
        ).vector_storage_dir = bind_provider_vector_runtime(provider).storage_dir
        return None
    configured_backend = _normalize_vector_backend(identity.backend)
    manifest_backend = _normalize_vector_backend(manifest.get("backend") or "")
    raw_fallback_backend = str(
        (bind_provider_vector_runtime(provider).vector_config or {}).get(
            "fallback_backend"
        )
        or ""
    ).strip()
    fallback_backend = (
        _normalize_vector_backend(raw_fallback_backend) if raw_fallback_backend else ""
    )
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
        Path(bind_provider_vector_runtime(provider).storage_dir),
        manifest.get("storage_path"),
    )
    bind_provider_vector_runtime(provider).vector_backend = manifest_backend
    bind_provider_vector_runtime(provider).vector_storage_dir = generation_root
    if manifest_backend != configured_backend:
        _append_vector_message(
            provider,
            f"using active {manifest_backend} fallback generation; switching to {configured_backend} requires explicit activation",
        )
    return manifest


def _open_sqlite_vector_store(
    provider: Any, *, table_name: str, dimensions: int, metric: str
) -> None:
    storage_root = Path(
        getattr(bind_provider_vector_runtime(provider), "vector_storage_dir", None)
        or bind_provider_vector_runtime(provider).storage_dir
    )
    temp_store = build_vector_store(
        "sqlite-bruteforce",
        storage_dir=storage_root,
        table_name=table_name,
        dimensions=dimensions,
        metric=metric,
    )
    temp_store.open_existing_for_update()
    bind_provider_vector_runtime(provider).vector_store = temp_store
    bind_provider_vector_runtime(provider).vector_backend = "sqlite-bruteforce"


def _open_vector_store(provider: Any, *, dimensions: int) -> None:
    """Open only the physical store named by the active generation.

    Backend fallback is a fresh-bootstrap decision. Once a manifest is active,
    switching backend here would cross an embedding-space boundary and is
    therefore forbidden.
    """
    if bind_provider_vector_runtime(provider).storage_dir is None:
        raise RuntimeError("storage not initialized")
    storage_root = Path(
        getattr(bind_provider_vector_runtime(provider), "vector_storage_dir", None)
        or bind_provider_vector_runtime(provider).storage_dir
    )
    table_name = str(
        (bind_provider_vector_runtime(provider).vector_config or {}).get("table_name")
        or "memories"
    )
    metric = str(
        (bind_provider_vector_runtime(provider).retrieval_config or {}).get("metric")
        or "cosine"
    )
    backend = _normalize_vector_backend(
        getattr(bind_provider_vector_runtime(provider), "vector_backend", "")
        or "lancedb"
    )
    try:
        old_store = bind_provider_vector_runtime(provider).vector_store
        if old_store is not None:
            old_store.close()
    except Exception:
        pass

    if backend == "sqlite-bruteforce":
        _open_sqlite_vector_store(
            provider, table_name=table_name, dimensions=dimensions, metric=metric
        )
        return

    if backend == "pgvector":
        temp_store = build_vector_store(
            backend,
            storage_dir=storage_root,
            table_name=table_name,
            dimensions=dimensions,
            metric=metric,
            config=bind_provider_vector_runtime(provider).vector_config or {},
        )
        if not temp_store.is_available():
            raise RuntimeError("pgvector dependencies or DSN are not configured")
        temp_store.open_existing_for_update()
        bind_provider_vector_runtime(provider).vector_backend = "pgvector"
        bind_provider_vector_runtime(provider).vector_store = temp_store
        return

    if backend != "lancedb":
        raise RuntimeError(f"unsupported backend {backend}")

    vector_dir = storage_root / "lancedb"
    temp_store = LanceVectorStore(
        vector_dir, table_name=table_name, dimensions=dimensions, metric=metric
    )
    if not temp_store.is_available():
        status = native_vector_dependency_status()
        detail = _native_dependency_detail(status)
        raise RuntimeError(f"lancedb/pyarrow is not installed or unsafe ({detail})")
    try:
        temp_store.open_existing_for_update()
    except VectorStoreCompatibilityError:
        temp_store.close()
        raise
    bind_provider_vector_runtime(provider).vector_backend = "lancedb"
    bind_provider_vector_runtime(provider).vector_store = temp_store


def setup_vector_layer(provider: Any) -> None:
    """Initialize the configured vector companion for a provider instance.

    Setup records readiness and repair status without blocking SQLite-only operation when vector support is intentionally disabled."""
    old_store = getattr(bind_provider_vector_runtime(provider), "vector_store", None)
    if old_store is not None:
        try:
            old_store.close()
        except Exception:
            logger.debug(
                "Scope Recall vector store close during setup failed", exc_info=True
            )
    try:
        close_embedder(
            getattr(bind_provider_vector_runtime(provider), "embedder", None)
        )
    except Exception:
        logger.debug("Scope Recall embedder close during setup failed", exc_info=True)
    bind_provider_vector_runtime(provider).vector_enabled = config_bool(
        bind_provider_vector_runtime(provider).vector_config or {}, "enabled", False
    )
    bind_provider_vector_runtime(provider).vector_backend = str(
        (bind_provider_vector_runtime(provider).vector_config or {}).get("backend")
        or "lancedb"
    )
    _set_vector_status(
        provider,
        state="disabled",
        reason_code="disabled_by_config",
        ready=False,
        usable_for_query=False,
        debt_counts={},
    )
    bind_provider_vector_runtime(provider).vector_row_count = 0
    bind_provider_vector_runtime(provider).vector_unique_id_count = 0
    bind_provider_vector_runtime(provider).vector_duplicate_row_count = 0
    bind_provider_vector_runtime(provider).embedder = None
    bind_provider_vector_runtime(provider).vector_store = None
    bind_provider_vector_runtime(provider).vector_generation_id = ""
    bind_provider_vector_runtime(provider).vector_reconciliation = None
    bind_provider_vector_runtime(
        provider
    ).vector_storage_dir = bind_provider_vector_runtime(provider).storage_dir
    if not bind_provider_vector_runtime(provider).vector_enabled:
        return
    if bind_provider_vector_runtime(provider).storage_dir is None:
        mark_vector_needs_repair(
            provider,
            "storage not initialized",
            reason_code="storage_uninitialized",
        )
        return

    conn = bind_provider_vector_runtime(provider).require_conn()
    try:
        manifest_hint = current_generation(conn)
    except Exception as exc:
        _mark_vector_startup_failure(provider, exc)
        return
    if manifest_hint is None:
        runtime_config = getattr(bind_provider_vector_runtime(provider), "config", None)
        if not isinstance(runtime_config, dict):
            runtime_config = {
                "vector": dict(
                    bind_provider_vector_runtime(provider).vector_config or {}
                ),
                "retrieval": dict(
                    bind_provider_vector_runtime(provider).retrieval_config or {}
                ),
            }
        try:
            bootstrap_receipt = bootstrap_fresh_vector_companion(
                Path(bind_provider_vector_runtime(provider).storage_dir),
                runtime_config,
                truth_conn=conn,
            )
        except Exception as exc:
            _mark_vector_startup_failure(provider, exc)
            return
        bootstrap_status = str(bootstrap_receipt.get("status") or "")
        if bootstrap_status not in {"ready", "existing"}:
            reason = str(bootstrap_receipt.get("reason") or "bootstrap unavailable")
            message = f"vector generation bootstrap unavailable: {reason}"
            explicit_migration_required = (
                bool(bootstrap_receipt.get("explicit_migration_required"))
                or "explicit_migration_required" in reason
            )
            if explicit_migration_required:
                mark_vector_needs_repair(
                    provider,
                    message,
                    reason_code="generation_incomplete",
                )
            else:
                _mark_vector_startup_degraded(provider, message)
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

    embedder_cfg = dict(
        (bind_provider_vector_runtime(provider).vector_config or {}).get("embedder")
        or {}
    )
    fallback_cfg = dict(
        (bind_provider_vector_runtime(provider).vector_config or {}).get(
            "fallback_embedder"
        )
        or {}
    )
    manifest_backend = _normalize_vector_backend(manifest_hint.get("backend") or "")
    candidate_specs = [("primary", embedder_cfg)]
    if fallback_cfg:
        candidate_specs.append(("fallback", fallback_cfg))

    selected_embedder: Any | None = None
    selected_identity: GenerationIdentity | None = None
    candidate_failures: list[str] = []
    fallback_incompatible = False
    compatibility_failure_seen = False
    candidate_unavailability_seen = False
    for label, candidate_config in candidate_specs:
        try:
            candidate_embedder = build_embedder(candidate_config)
        except Exception as exc:
            candidate_unavailability_seen = True
            candidate_failures.append(
                f"{label}_embedder_build_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        try:
            available = bool(candidate_embedder.is_available())
        except Exception as exc:
            close_embedder(candidate_embedder)
            candidate_failures.append(
                f"{label}_embedder_probe_failed:{_sanitize_vector_message(exc)}"
            )
            continue
        if not available:
            candidate_unavailability_seen = True
            close_embedder(candidate_embedder)
            candidate_failures.append(f"{label}_embedder_unavailable")
            continue
        try:
            provisional_identity = _configured_generation_identity(
                provider,
                candidate_embedder,
                candidate_config,
            )
        except Exception as exc:
            candidate_unavailability_seen = True
            close_embedder(candidate_embedder)
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
            compatibility_failure_seen = True
            close_embedder(candidate_embedder)
            candidate_failures.append(
                f"{label}_embedding_space_incompatible:{_sanitize_vector_message(exc)}"
            )
            fallback_incompatible = fallback_incompatible or label == "fallback"
            continue
        try:
            candidate_embedder.probe_readiness()
        except Exception as exc:
            candidate_unavailability_seen = True
            close_embedder(candidate_embedder)
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
            candidate_unavailability_seen = True
            close_embedder(candidate_embedder)
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
            compatibility_failure_seen = True
            close_embedder(candidate_embedder)
            candidate_failures.append(
                f"{label}_embedding_space_incompatible:{_sanitize_vector_message(exc)}"
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
        if compatibility_failure_seen and not candidate_unavailability_seen:
            mark_vector_needs_repair(
                provider,
                reason,
                reason_code="identity_mismatch",
            )
        else:
            _mark_vector_startup_degraded(provider, reason)
        return

    bind_provider_vector_runtime(provider).embedder = selected_embedder
    try:
        existing_manifest = _select_generation_storage(provider, selected_identity)
    except Exception as exc:
        close_embedder(bind_provider_vector_runtime(provider).embedder)
        _mark_vector_startup_failure(provider, exc)
        return

    if existing_manifest is None:
        close_embedder(bind_provider_vector_runtime(provider).embedder)
        _mark_vector_startup_degraded(
            provider,
            "active vector generation disappeared before physical open",
        )
        return

    try:
        _open_vector_store(
            provider,
            dimensions=bind_provider_vector_runtime(provider).embedder.dimensions,
        )
        opened_identity = replace(
            selected_identity,
            backend=_normalize_vector_backend(
                bind_provider_vector_runtime(provider).vector_backend
            ),
        )
        validate_generation_compatibility(existing_manifest, opened_identity)
        bind_provider_vector_runtime(provider).vector_row_count = int(
            existing_manifest.get("row_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_unique_id_count = int(
            existing_manifest.get("unique_id_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_duplicate_row_count = max(
            0,
            bind_provider_vector_runtime(provider).vector_row_count
            - bind_provider_vector_runtime(provider).vector_unique_id_count,
        )
        # Ordinary startup is outbox-first and strictly bounded.  Full stale-row
        # sweeps remain explicit repair/doctor work; they are never hidden here.
        reconciliation = run_bounded_vector_reconciliation(provider)
        bind_provider_vector_runtime(provider).vector_reconciliation = reconciliation
        bind_provider_vector_runtime(
            provider
        ).vector_debt_counts = normalize_vector_debt_counts(reconciliation)
        if int(reconciliation.get("dead_letter") or 0) > 0:
            mark_vector_needs_repair(
                provider,
                "vector outbox contains dead-lettered startup debt; inspect with "
                "python scripts/requeue.vector_dead_letter.py --dry-run",
                reason_code="outbox_dead_letter",
            )
            close_embedder(bind_provider_vector_runtime(provider).embedder)
            if bind_provider_vector_runtime(provider).vector_store is not None:
                bind_provider_vector_runtime(provider).vector_store.close()
            bind_provider_vector_runtime(provider).vector_store = None
            return
        if (
            str(reconciliation.get("status") or "").strip().lower() == "failed"
            or int(reconciliation.get("failed") or 0) > 0
        ):
            raise RuntimeError(
                str(reconciliation.get("error") or "")
                or "bounded vector outbox replay failed during startup"
            )
        replayed = int(reconciliation.get("completed") or 0)
        planned = int(reconciliation.get("planned") or 0)
        if replayed or planned:
            _append_vector_message(
                provider,
                f"bounded vector startup planned {planned} and replayed {replayed} event(s)",
            )
    except Exception as exc:
        _mark_vector_startup_failure(provider, exc)
        close_embedder(bind_provider_vector_runtime(provider).embedder)
        if bind_provider_vector_runtime(provider).vector_store is not None:
            try:
                bind_provider_vector_runtime(provider).vector_store.close()
            except Exception:
                pass
        bind_provider_vector_runtime(provider).vector_store = None
        return

    _set_vector_status(
        provider,
        state="ready",
        reason_code="healthy",
        message=bind_provider_vector_runtime(provider).vector_message,
        ready=True,
        usable_for_query=True,
        debt_counts=bind_provider_vector_runtime(provider).vector_debt_counts,
    )


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

    if (
        getattr(bind_provider_vector_runtime(provider), "vector_store", None)
        is not None
    ):
        return True
    if str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    ).strip():
        return True

    try:
        conn = bind_provider_vector_runtime(provider).require_conn()
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

    status = (
        str(getattr(bind_provider_vector_runtime(provider), "vector_status", "") or "")
        .strip()
        .lower()
    )
    enabled = bool(
        getattr(bind_provider_vector_runtime(provider), "vector_enabled", False)
    )
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
    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if not generation_id:
        return
    conn = bind_provider_vector_runtime(provider).require_conn()
    with bind_provider_vector_runtime(provider).lock:
        owns_transaction = not bool(getattr(conn, "in_transaction", False))
        changed = update_generation_cardinality(
            conn,
            generation_id=generation_id,
            row_count=physical_rows,
            unique_id_count=unique_ids,
        )
        if changed and owns_transaction:
            conn.commit()


def _vector_audit_counts(provider: Any) -> dict[str, int]:
    if not getattr(bind_provider_vector_runtime(provider), "vector_store", None):
        return {
            "physical_rows": 0,
            "unique_ids": 0,
            "duplicate_rows": 0,
            "duplicate_ids": 0,
        }
    return bind_provider_vector_runtime(provider).vector_store.audit_counts()


def _vector_store_ids(provider: Any) -> list[str]:
    """Collect companion ids for an explicit Doctor/full audit only."""

    store = getattr(bind_provider_vector_runtime(provider), "vector_store", None)
    list_ids = getattr(store, "list_ids", None) if store is not None else None
    if not callable(list_ids):
        return []
    return [str(item) for item in cast(Iterable[Any], list_ids()) if str(item)]


def refresh_vector_audit(provider: Any, *, persist: bool = True) -> dict[str, int]:
    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if not persist:
        counts = _vector_audit_counts(provider)
        bind_provider_vector_runtime(provider).vector_row_count = int(
            counts.get("physical_rows") or counts.get("row_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_unique_id_count = int(
            counts.get("unique_ids") or counts.get("unique_id_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_duplicate_row_count = int(
            counts.get("duplicate_rows") or 0
        )
        return counts

    with _vector_mutation_lock(provider):
        counts = _vector_audit_counts(provider)
        member_ids = _vector_store_ids(provider)
        bind_provider_vector_runtime(provider).vector_row_count = int(
            counts.get("physical_rows") or counts.get("row_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_unique_id_count = int(
            counts.get("unique_ids") or counts.get("unique_id_count") or 0
        )
        bind_provider_vector_runtime(provider).vector_duplicate_row_count = int(
            counts.get("duplicate_rows") or 0
        )
        conn = bind_provider_vector_runtime(provider).require_conn()
        with bind_provider_vector_runtime(provider).lock:
            owns_transaction = not bool(getattr(conn, "in_transaction", False))
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            try:
                if generation_id:
                    replace_generation_membership(conn, generation_id, member_ids)
                _persist_vector_cardinality(
                    provider,
                    physical_rows=bind_provider_vector_runtime(
                        provider
                    ).vector_row_count,
                    unique_ids=bind_provider_vector_runtime(
                        provider
                    ).vector_unique_id_count,
                )
                if owns_transaction:
                    conn.commit()
            except Exception:
                if owns_transaction:
                    conn.rollback()
                raise
    return counts


def refresh_vector_audit_and_persist(provider: Any) -> dict[str, int]:
    """Writer-only cardinality persist. Stats/prefetch must not call this."""
    from .write_kernel import hold_positive_write_authority

    with hold_positive_write_authority(provider):
        return refresh_vector_audit(provider, persist=True)


def _should_index_target(provider: Any, target: str) -> bool:
    return str(target) != "general" or config_bool(
        bind_provider_vector_runtime(provider).vector_config or {},
        "index_general",
        False,
    )


def _should_index_row(
    provider: Any, target: str, metadata: dict[str, Any] | str | None
) -> bool:
    """Apply one lifecycle/target contract to every vector mutation path."""

    payload = load_metadata(metadata or {})
    lifecycle = str(payload.get("lifecycle") or "")
    return _should_index_target(provider, target) and ordinary_recall_lifecycle_visible(
        lifecycle=lifecycle,
        target=str(target),
    )


def _adjusted_vector_counts(
    provider: Any,
    *,
    operation: str,
    existed: bool,
) -> tuple[int, int] | None:
    """Return the next cached physical/unique pair for one proven mutation."""

    physical = max(
        0,
        int(
            getattr(bind_provider_vector_runtime(provider), "vector_row_count", 0) or 0
        ),
    )
    unique = max(
        0,
        int(
            getattr(bind_provider_vector_runtime(provider), "vector_unique_id_count", 0)
            or 0
        ),
    )
    if operation == "upsert":
        if not existed:
            physical += 1
            unique += 1
    elif operation == "delete":
        if existed:
            physical = max(0, physical - 1)
            unique = max(0, unique - 1)
    else:
        return None
    unique = min(unique, physical)
    return physical, unique


def _apply_incremental_vector_counts(
    provider: Any,
    *,
    operation: str,
    memory_id: str,
    existed: bool | None,
) -> None:
    """Adjust cached physical/unique counts after one successful mutation.

    The supported path uses the SQLite membership ledger: a primary-key
    equality lookup on ``(generation_id, memory_id)`` decides insert vs
    update vs delete, then membership and cached counts commit together.
    Unknown existence leaves the cache unchanged. Duplicate discovery stays
    on explicit Doctor audit. In-memory counters update only after that
    transaction commits so a persist error cannot drift ahead of the ledger.
    """

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    conn = bind_provider_vector_runtime(provider).require_conn()
    with bind_provider_vector_runtime(provider).lock:
        owns_transaction = not bool(getattr(conn, "in_transaction", False))
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            resolved_existed = existed
            if generation_id and membership_is_ready(conn, generation_id):
                resolved_existed = apply_membership_mutation(
                    conn,
                    generation_id=generation_id,
                    memory_id=memory_id,
                    operation=operation,
                )
            if resolved_existed is None:
                if owns_transaction:
                    conn.rollback()
                return
            adjusted = _adjusted_vector_counts(
                provider,
                operation=operation,
                existed=resolved_existed,
            )
            if adjusted is None:
                if owns_transaction:
                    conn.rollback()
                return
            physical, unique = adjusted
            if generation_id:
                update_generation_cardinality(
                    conn,
                    generation_id=generation_id,
                    row_count=physical,
                    unique_id_count=unique,
                )
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
    bind_provider_vector_runtime(provider).vector_row_count = physical
    bind_provider_vector_runtime(provider).vector_unique_id_count = unique
    bind_provider_vector_runtime(provider).vector_duplicate_row_count = max(
        0, physical - unique
    )


def run_bounded_vector_reconciliation(provider: Any) -> dict[str, Any]:
    """Serialize the complete bounded truth/outbox/physical reconciliation."""

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if (
        not generation_id
        or not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
        return _empty_reconciliation_result("unavailable")
    if not _startup_reconcile_enabled(provider):
        # Disabled ticks must not acquire the mutation lock, touch outbox rows,
        # or advance the truth reconciliation watermark.
        return _empty_reconciliation_result("disabled")
    header_failure = _truth_header_preflight(provider)
    if header_failure is not None:
        return header_failure
    with _vector_mutation_lock(provider):
        return _run_bounded_vector_reconciliation_guarded(provider)


def _run_bounded_vector_reconciliation_guarded(provider: Any) -> dict[str, Any]:
    """Replay debt first, then plan and replay at most one truth page.

    The startup/background contract is intentionally independent of total truth
    or physical-vector cardinality.  A full stale-row/duplicate sweep remains an
    explicit operator repair action via :func:`sync_vector_index` and doctor.
    """

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if (
        not generation_id
        or not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
        return {
            "status": "unavailable",
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "planned": 0,
            "replayable": 0,
            "dead_letter": 0,
        }
    config = dict(bind_provider_vector_runtime(provider).vector_config or {})
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
    retention_interval_seconds = _bounded_config_int(
        config,
        "outbox_retention_interval_seconds",
        default=900,
        minimum=60,
        maximum=86_400,
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
    conn = bind_provider_vector_runtime(provider).require_conn()
    with bind_provider_vector_runtime(provider).lock:
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

    with bind_provider_vector_runtime(provider).lock:
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
    with bind_provider_vector_runtime(provider).lock:
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
        else (
            "outbox_pending"
            if result["replayable"]
            else str(page.get("status") or "ready")
        )
    )
    if result["failed"] or result["dead_letter"] or result["replayable"]:
        result["outbox_retention"] = {
            "status": "deferred",
            "reason": "nonterminal_backlog",
            "deleted": 0,
        }
    elif not _outbox_retention_due(
        provider, interval_seconds=retention_interval_seconds
    ):
        result["outbox_retention"] = {
            "status": "rate_limited",
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
    if (
        not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
        return 0
    conn = bind_provider_vector_runtime(provider).require_conn()
    with bind_provider_vector_runtime(provider).lock:
        rows = conn.execute(
            f"SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories m WHERE {ordinary_recall_lifecycle_visible_sql('m')} ORDER BY updated_at ASC"
        ).fetchall()
    with _vector_mutation_lock(provider):
        if not rows:
            # No visible SQLite rows means any remaining vector records are
            # stale companion data under the same lifecycle filter.
            existing = bind_provider_vector_runtime(provider).vector_store.list_ids()
            if existing:
                bind_provider_vector_runtime(provider).vector_store.delete_by_ids(
                    existing
                )
            refresh_vector_audit(provider)
            return 0

        desired = {
            str(row["id"]): row
            for row in rows
            if _should_index_row(provider, str(row["target"]), row["metadata"])
        }
        existing_records = bind_provider_vector_runtime(
            provider
        ).vector_store.list_records()
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
            bind_provider_vector_runtime(provider).vector_store.delete_by_ids(stale_ids)

        changed_rows = []
        for memory_id, row in desired.items():
            current = existing_records.get(memory_id)
            if current is None:
                changed_rows.append(row)
                continue
            if str(current.get("updated_at") or "") != str(row["updated_at"] or ""):
                changed_rows.append(row)

        if changed_rows:
            texts = [
                bind_provider_vector_runtime(provider).vector_text(
                    row["summary"], row["content"]
                )
                for row in changed_rows
            ]
            embed_maintenance = getattr(
                bind_provider_vector_runtime(provider).embedder,
                "embed_maintenance",
                None,
            )
            raw_vectors = (
                embed_maintenance(texts)
                if callable(embed_maintenance)
                else bind_provider_vector_runtime(provider).embedder.embed_texts(texts)
            )
            vectors = validate_embedding_batch(
                raw_vectors,
                expected_count=len(changed_rows),
                expected_dimensions=int(
                    bind_provider_vector_runtime(provider).embedder.dimensions
                ),
                provider=str(
                    getattr(
                        bind_provider_vector_runtime(provider).embedder,
                        "provider",
                        "embedder",
                    )
                ),
            )
            payload = []
            for row, vector in zip_embedding_rows(
                changed_rows,
                vectors,
                provider=str(
                    getattr(
                        bind_provider_vector_runtime(provider).embedder,
                        "provider",
                        "embedder",
                    )
                ),
            ):
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
            bind_provider_vector_runtime(provider).vector_store.upsert_records(payload)
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

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if not generation_id:
        return False
    material = "\x1f".join((generation_id, memory_id, operation, str(updated_at or "")))
    event_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    try:
        conn = bind_provider_vector_runtime(provider).require_conn()
        with bind_provider_vector_runtime(provider).lock:
            enqueue_vector_event(
                conn,
                event_key=event_key,
                generation_id=generation_id,
                memory_id=memory_id,
                operation=operation,
                payload={
                    "updated_at": str(updated_at or ""),
                    "reason": sanitize_report_text(reason)[:500],
                },
            )
            if commit:
                conn.commit()
        return True
    except Exception as exc:
        if raise_on_error:
            raise
        logger.warning(
            "Scope Recall could not persist vector replay event for %s: %s",
            memory_id,
            exc,
        )
        return False


def replay_vector_outbox_events(
    provider: Any,
    *,
    event_ids: Sequence[int],
    refresh_audit_after: bool = False,
) -> dict[str, int]:
    """Replay exact committed outbox IDs without draining unrelated backlog first."""

    resolved_ids = tuple(
        dict.fromkeys(int(event_id) for event_id in event_ids if int(event_id) > 0)
    )
    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if (
        not resolved_ids
        or not generation_id
        or not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
        return {"claimed": 0, "completed": 0, "failed": 0}
    with _vector_mutation_lock(provider):
        return _replay_vector_outbox_guarded(
            provider,
            limit=len(resolved_ids),
            event_ids=resolved_ids,
            refresh_audit_after=refresh_audit_after,
        )


def resolve_vector_outbox_rows_by_keys(
    provider: Any,
    event_keys: Sequence[str],
) -> list[dict[str, Any]]:
    """Read exact durable vector intents in stable caller key order.

    Missing rows are represented explicitly so retention-facing callers fail
    closed instead of mistaking an aggregate replay count for causal success.
    """

    resolved_keys = tuple(
        dict.fromkeys(
            str(event_key).strip()
            for event_key in event_keys
            if str(event_key).strip()
        )
    )
    if not resolved_keys:
        return []
    runtime = bind_provider_vector_runtime(provider)
    conn = runtime.require_conn()
    rows: list[Any] = []

    def read_rows() -> None:
        for key_chunk in chunked_sql_parameters(conn, resolved_keys):
            placeholders = ",".join("?" for _ in key_chunk)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id, event_key, generation_id, memory_id, operation,
                           status, attempts, available_at
                    FROM vector_outbox
                    WHERE event_key IN ({placeholders})
                    """,
                    key_chunk,
                ).fetchall()
            )

    lock = getattr(runtime, "lock", None)
    if lock is None:
        read_rows()
    else:
        with lock:
            read_rows()
    by_key = {
        str(row["event_key"]): {
            "id": int(row["id"]),
            "event_key": str(row["event_key"]),
            "generation_id": str(row["generation_id"]),
            "memory_id": str(row["memory_id"]),
            "operation": str(row["operation"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"] or 0),
            "available_at": str(row["available_at"] or ""),
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
                "attempts": 0,
                "available_at": "",
            },
        )
        for event_key in resolved_keys
    ]


def replay_and_classify_exact_vector_intents(
    provider: Any,
    event_keys: Sequence[str],
) -> dict[str, Any]:
    """Replay and classify only the causal intents named by ``event_keys``."""

    resolved_keys = list(
        dict.fromkeys(
            str(event_key).strip()
            for event_key in event_keys
            if str(event_key).strip()
        )
    )
    if not resolved_keys:
        return {
            "event_keys": [],
            "event_ids": [],
            "status_counts": {},
            "replay": {"claimed": 0, "completed": 0, "failed": 0},
            "all_completed": True,
            "retryable_pending": 0,
            "dead_letter": 0,
            "missing": 0,
            "other_pending": 0,
        }
    before_rows = resolve_vector_outbox_rows_by_keys(provider, resolved_keys)
    replay_ids = [int(row["id"]) for row in before_rows if int(row["id"]) > 0]
    replay = replay_vector_outbox_events(provider, event_ids=replay_ids)
    final_rows = resolve_vector_outbox_rows_by_keys(provider, resolved_keys)
    status_counts: dict[str, int] = {}
    for row in final_rows:
        status = str(row.get("status") or "missing")
        status_counts[status] = status_counts.get(status, 0) + 1
    retryable_statuses = {"pending", "processing", "retry"}
    known_statuses = {*retryable_statuses, "completed", "dead_letter", "missing"}
    dead_letter = int(status_counts.get("dead_letter", 0))
    missing = int(status_counts.get("missing", 0))
    retryable_pending = sum(
        int(status_counts.get(status, 0)) for status in retryable_statuses
    )
    other_pending = sum(
        count for status, count in status_counts.items() if status not in known_statuses
    )
    all_completed = bool(final_rows) and all(
        str(row.get("status") or "missing") == "completed" for row in final_rows
    )
    if dead_letter:
        mark_vector_needs_repair(
            provider,
            "exact vector intent reached dead-letter status",
            reason_code="exact_outbox_dead_letter",
        )
    return {
        "event_keys": resolved_keys,
        "event_ids": [int(row["id"]) for row in final_rows if int(row["id"]) > 0],
        "status_counts": status_counts,
        "replay": replay,
        "all_completed": all_completed,
        "retryable_pending": retryable_pending,
        "dead_letter": dead_letter,
        "missing": missing,
        "other_pending": other_pending,
    }


def replay_vector_outbox(
    provider: Any,
    *,
    limit: int = 200,
    refresh_audit_after: bool = False,
) -> dict[str, int]:
    """Serialize truth outbox claims, schema bookkeeping, and physical replay.

    Ordinary writes keep cardinality incrementally after successful per-id
    mutations. They never COUNT or list the companion. A full ID audit runs
    only when the caller explicitly asks for one, and empty ``claimed=0``
    replays skip that hook entirely.
    """

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if (
        not generation_id
        or not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
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
    event_ids: Sequence[int] | None = None,
    refresh_audit_after: bool = False,
) -> dict[str, int]:
    """Replay committed intent through the shared single-writer executor."""

    generation_id = str(
        getattr(bind_provider_vector_runtime(provider), "vector_generation_id", "")
        or ""
    )
    if (
        not generation_id
        or not bind_provider_vector_runtime(provider).vector_store
        or not bind_provider_vector_runtime(provider).embedder
    ):
        return {"claimed": 0, "completed": 0, "failed": 0}

    def on_failure(error: str) -> None:
        _mark_vector_replay_degraded(provider, error)
        logger.warning("Scope Recall vector replay failed: %s", error)

    result = replay_committed_vector_events(
        bind_provider_vector_runtime(provider).require_conn(),
        generation_id=generation_id,
        vector_store=bind_provider_vector_runtime(provider).vector_store,
        embedder=bind_provider_vector_runtime(provider).embedder,
        vector_text=getattr(
            bind_provider_vector_runtime(provider),
            "vector_text",
            lambda summary, content: f"{summary}\n{content}".strip(),
        ),
        should_index_row=lambda target, metadata: _should_index_row(
            provider, target, metadata
        ),
        default_scope_id=str(
            getattr(bind_provider_vector_runtime(provider), "scope_id", "") or ""
        ),
        db_lock=getattr(bind_provider_vector_runtime(provider), "lock", None),
        mutation_context=lambda: _vector_mutation_lock(provider),
        limit=limit,
        event_ids=event_ids,
        on_failure=on_failure,
        after_replay=(
            (lambda: refresh_vector_audit(provider)) if refresh_audit_after else None
        ),
        on_physical_mutation=(
            lambda operation, memory_id, existed: _apply_incremental_vector_counts(
                provider,
                operation=operation,
                memory_id=memory_id,
                existed=existed,
            )
        ),
    )
    debt = _refresh_vector_debt_counts(provider)
    if debt["dead_letter"]:
        mark_vector_needs_repair(
            provider,
            "vector outbox contains dead-letter debt",
            reason_code="outbox_dead_letter",
        )
    else:
        _recover_vector_replay_state(provider, result)
    return result


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
            row = (
                bind_provider_vector_runtime(provider)
                .require_conn()
                .execute("SELECT metadata FROM memories WHERE id = ?", (id,))
                .fetchone()
            )
            if row is not None:
                resolved_metadata = row["metadata"]
        except Exception:
            resolved_metadata = None
    operation = (
        "upsert" if _should_index_row(provider, target, resolved_metadata) else "delete"
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
    if (
        bind_provider_vector_runtime(provider).vector_ready
        and bind_provider_vector_runtime(provider).vector_store
        and bind_provider_vector_runtime(provider).embedder
    ):
        replay_vector_outbox(provider, limit=vector_write_replay_limit(provider))
