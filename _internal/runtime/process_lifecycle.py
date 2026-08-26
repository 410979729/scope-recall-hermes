"""Process lifecycle: initialize, promote, quiesce, then close shared resources.

ProcessLifecycle owns startup (scope/principal, lease, reader/writer roles,
schema/backfill, registration, background digest start, failed-writer cleanup,
and reader-to-writer promotion) and shutdown. Provider keeps one-line
delegates. Connect, lease, schema, backfill, and writer hooks are read from
``type(adapter).__module__`` at call time so provider-module patches still
resolve.

Writer or digest join failure is a quiescence-barrier failure. The caller
must keep SQLite, vector, the provider registry, and the writer lease in
place, with ``finalized=false``, so a later shutdown can finish after the
worker acknowledges stop. After workers stop, SQLite close failure keeps
the published connection and lease and raises incomplete teardown. Vector
close failure is a companion error: the store is detached and the lease
may still release once truth teardown completed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ...capture import shutdown_writer
from ...config import load_runtime_config
from ...desktop_principal import (
    desktop_principal_from_config,
    is_desktop_platform,
    resolve_desktop_principal,
)
from ...experience_store import backfill_skill_anchors
from ...freshness import backfill_untracked_memory_freshness
from ...gating import config_bool
from ...journal import ensure_journal_schema
from ...maintenance_lease import ensure_activation_guard_triggers
from ...migration import migrate_legacy_scope_recall_storage
from ...models import RuntimeScope
from ...scope import (
    RUNTIME_STATUS_ACTIVE,
    RUNTIME_STATUS_ACTIVE_READ_ONLY,
    RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL,
    accessible_scope_ids,
    build_scope_id,
    build_shared_pool_scope_id,
    build_shared_scope_id,
    normalize_scope_identity,
    runtime_principal_status,
    writable_scope_ids,
)
from ...sql_store import ensure_schema
from ...sqlite_recovery import is_sqlite_lock_contention
from ...truth_connection import connect_truth_database
from ...embedders import close_embedder
from ...write_kernel import TruthWriterLease, sanitized_truth_writer_owner
from .storage import finish_writer_schema_setup, open_readonly_truth_connection

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_SECONDS = 10.0
DEFAULT_FRESHNESS_BACKFILL_LIMIT = 500
_MISSING = object()


def _adapter_modules(adapter: Any) -> list[Any]:
    names: list[str] = []
    module_name = getattr(type(adapter), "__module__", "") or ""
    if module_name:
        names.append(module_name)
    if module_name != "scope_recall.provider":
        names.append("scope_recall.provider")
    modules: list[Any] = []
    seen: set[int] = set()
    for name in names:
        module = sys.modules.get(name)
        if module is None or id(module) in seen:
            continue
        seen.add(id(module))
        modules.append(module)
    return modules


def _module_attr(adapter: Any, name: str, default: Any) -> Any:
    """Prefer the adapter module hook, then the provider module, then default."""

    for module in _adapter_modules(adapter):
        value = getattr(module, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _provider_module() -> Any:
    return sys.modules.get("scope_recall.provider")


def _call_named_provider_hook(name: str, fallback: Callable[..., Any], provider: Any, timeout: float) -> None:
    module = _provider_module()
    fn = getattr(module, name, None) if module is not None else None
    if not callable(fn):
        fn = fallback
    fn(provider, timeout=timeout)


def _shutdown_writer(provider: Any, timeout: float) -> None:
    from ...capture import shutdown_writer as default_shutdown_writer

    _call_named_provider_hook("shutdown_writer", default_shutdown_writer, provider, timeout)


def _background_work(provider: Any) -> Any:
    work = getattr(provider, "_background_work", None)
    if callable(work):
        return work()
    background = getattr(provider, "_background", None)
    if background is None:
        raise RuntimeError("Scope Recall process lifecycle has no background work owner")
    return background


def _lock_contention(provider: Any, exc: BaseException) -> bool:
    fn = _module_attr(provider, "_is_sqlite_lock_contention", None)
    if not callable(fn):
        fn = _module_attr(provider, "is_sqlite_lock_contention", is_sqlite_lock_contention)
    return bool(fn(exc))


def _call_provider(provider: Any, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    fn = getattr(provider, name, None)
    if callable(fn):
        return fn(*args, **kwargs)
    if callable(default):
        return default(*args, **kwargs)
    raise RuntimeError(f"Scope Recall process lifecycle missing {name}")


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _acquire_shutdown_lock(lock: Any, *, deadline: float, name: str) -> bool:
    """Acquire one optional process-shutdown lock without crossing deadline."""

    if lock is None:
        return False
    remaining = _remaining(deadline)
    if remaining <= 0 or not lock.acquire(timeout=remaining):
        raise RuntimeError(
            f"Scope Recall shutdown deadline expired while acquiring {name} lock"
        )
    return True


class _ShutdownCleanupAttempt:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None


def _run_shutdown_cleanup(
    provider: Any, attempt: _ShutdownCleanupAttempt
) -> None:
    """Run one process-shutdown cleanup attempt and retain its outcome."""

    try:
        cleanup = getattr(provider, "_cleanup_failed_writer_initialization", None)
        if callable(cleanup):
            cleaned = cleanup(reraise_companion_errors=True)
        else:
            cleaned = cleanup_failed_writer_initialization(
                provider, reraise_companion_errors=True
            )
        if not cleaned:
            raise RuntimeError("Scope Recall truth teardown incomplete")
    except BaseException as exc:
        attempt.error = exc


def _join_shutdown_cleanup(provider: Any, *, deadline: float) -> None:
    """Start cleanup once, then wait only within this shutdown deadline."""

    lifecycle_lock = getattr(provider, "_writer_lifecycle_lock", None)
    lifecycle_acquired = False
    attempt: _ShutdownCleanupAttempt | None = None
    cleanup_thread: threading.Thread | None = None
    try:
        lifecycle_acquired = _acquire_shutdown_lock(
            lifecycle_lock, deadline=deadline, name="writer lifecycle"
        )
        candidate = getattr(provider, "_shutdown_cleanup_attempt", None)
        if isinstance(candidate, _ShutdownCleanupAttempt):
            attempt = candidate
            cleanup_thread = attempt.thread
        if (
            cleanup_thread is not None
            and not cleanup_thread.is_alive()
            and attempt is not None
            and attempt.error is not None
        ):
            attempt = None
            cleanup_thread = None
        if cleanup_thread is None:
            if _remaining(deadline) <= 0:
                raise RuntimeError(
                    "Scope Recall shutdown deadline expired before resource cleanup"
                )
            attempt = _ShutdownCleanupAttempt()
            cleanup_thread = threading.Thread(
                target=_run_shutdown_cleanup,
                args=(provider, attempt),
                name="scope-recall-shutdown-cleanup",
                daemon=True,
            )
            attempt.thread = cleanup_thread
            provider._shutdown_cleanup_attempt = attempt
            provider._shutdown_cleanup_thread = cleanup_thread
            try:
                cleanup_thread.start()
            except BaseException:
                provider._shutdown_cleanup_attempt = None
                provider._shutdown_cleanup_thread = None
                raise
    finally:
        if lifecycle_acquired:
            assert lifecycle_lock is not None
            lifecycle_lock.release()

    assert attempt is not None
    assert cleanup_thread is not None
    cleanup_thread.join(timeout=_remaining(deadline))
    if cleanup_thread.is_alive():
        raise RuntimeError(
            "Scope Recall shutdown deadline expired during resource cleanup"
        )
    if attempt.error is not None:
        raise attempt.error


def _attach_writer_runtime(provider: Any) -> None:
    bind = getattr(provider, "_bind_composition", None)
    composition = bind() if callable(bind) else getattr(provider, "_composition", None)
    attach = getattr(composition, "attach_writer_runtime", None)
    if not callable(attach):
        raise RuntimeError("Scope Recall process lifecycle has no writer-runtime composition")
    attach()


def has_live_initialize_runtime(provider: Any) -> bool:
    """Return whether this adapter already holds a live initialize runtime."""

    if getattr(provider, "_truth_writer_role", None) in {"owner", "reader"}:
        return True
    if getattr(provider, "_conn", None) is not None:
        return True
    lease = getattr(provider, "_truth_writer_lease", None)
    if lease is not None and bool(getattr(lease, "acquired", False)):
        return True
    thread = getattr(provider, "_writer_thread", None)
    return bool(thread is not None and thread.is_alive())


def initialize_provider_process(provider: Any, session_id: str, **kwargs: Any) -> None:
    """Take the writer-lifecycle lock, then run under-lock initialize."""

    lifecycle_lock = getattr(provider, "_writer_lifecycle_lock", None)
    runner = getattr(provider, "_initialize_under_lifecycle_lock", None)

    def _run() -> None:
        if callable(runner):
            runner(session_id, **kwargs)
            return
        initialize_under_lifecycle_lock(provider, session_id, **kwargs)

    if lifecycle_lock is not None:
        with lifecycle_lock:
            _run()
        return
    _run()


def initialize_under_lifecycle_lock(provider: Any, session_id: str, **kwargs: Any) -> None:
    """Initialize scope, lease, and reader/writer runtime while lifecycle is held."""

    live = getattr(provider, "_has_live_initialize_runtime", None)
    if callable(live):
        if live():
            raise RuntimeError(
                "Scope Recall provider must complete shutdown before initialize"
            )
    elif has_live_initialize_runtime(provider):
        raise RuntimeError(
            "Scope Recall provider must complete shutdown before initialize"
        )
    provider._shutdown_requested.clear()
    provider._shutdown_finalized = False
    provider._shutdown_cleanup_attempt = None
    provider._shutdown_cleanup_thread = None
    provider._session_id = session_id
    provider._current_turn = 0
    provider._last_recall_turns = {}
    provider._config = {}
    provider._retrieval_config = {}
    provider._vector_config = {}
    provider._scope_id = ""
    provider._shared_scope_id = ""
    provider._shared_pool_enabled = False
    provider._shared_pool_write_enabled = False
    provider._shared_pool_id = ""
    provider._shared_pool_scope_id = ""
    provider._accessible_scope_ids = []
    provider._writable_scope_ids = []

    scope_cls = _module_attr(provider, "RuntimeScope", RuntimeScope)
    raw_scope = scope_cls(
        platform=str(kwargs.get("platform") or "cli").strip().lower() or "cli",
        user_id=str(kwargs.get("user_id") or "").strip(),
        chat_id=str(kwargs.get("chat_id") or "").strip(),
        thread_id=str(kwargs.get("thread_id") or "").strip(),
        gateway_session_key=str(kwargs.get("gateway_session_key") or "").strip(),
        agent_identity=str(kwargs.get("agent_identity") or "").strip(),
        agent_workspace=str(kwargs.get("agent_workspace") or "").strip(),
        agent_context=str(kwargs.get("agent_context") or "primary").strip() or "primary",
    )
    provider._hermes_home = Path(
        kwargs.get("hermes_home") or "~/.hermes"
    ).expanduser()
    provider._storage_dir = None
    provider._db_path = None
    disabled = _module_attr(
        provider,
        "RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL",
        RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL,
    )
    # Desktop is a single-operator surface and often omits user_id. Resolve a
    # profile-local opaque principal before the fail-closed principal gate so
    # non-Desktop platforms still refuse activation without storage side effects.
    desktop_fn = _module_attr(provider, "is_desktop_platform", is_desktop_platform)
    if not raw_scope.user_id and desktop_fn(raw_scope.platform):
        storage_dir = provider._hermes_home / "scope-recall"
        load_config = _module_attr(provider, "load_runtime_config", load_runtime_config)
        early_config = load_config(provider._plugin_dir, storage_dir)
        try:
            explicit = _module_attr(
                provider, "desktop_principal_from_config", desktop_principal_from_config
            )(early_config)
        except ValueError:
            logger.error(
                "Scope Recall disabled: identity.desktop_principal is invalid"
            )
            provider._scope = raw_scope
            provider._runtime_status = disabled
            return
        principal = _module_attr(
            provider, "resolve_desktop_principal", resolve_desktop_principal
        )(
            hermes_home=provider._hermes_home,
            explicit=explicit,
        )
        raw_scope = scope_cls(
            platform=raw_scope.platform,
            user_id=principal,
            chat_id=raw_scope.chat_id,
            thread_id=raw_scope.thread_id,
            gateway_session_key=raw_scope.gateway_session_key,
            agent_identity=raw_scope.agent_identity,
            agent_workspace=raw_scope.agent_workspace,
            agent_context=raw_scope.agent_context,
        )
    provider._scope = raw_scope
    provider._runtime_status = _module_attr(
        provider, "runtime_principal_status", runtime_principal_status
    )(raw_scope)
    if provider._runtime_status == disabled:
        logger.error(
            "Scope Recall disabled: trusted non-CLI user principal is missing"
        )
        return

    provider._runtime_status = "initializing"
    provider._storage_dir = provider._hermes_home / "scope-recall"
    provider._storage_dir.mkdir(parents=True, exist_ok=True)
    provider._migration_info = _module_attr(
        provider, "migrate_legacy_scope_recall_storage", migrate_legacy_scope_recall_storage
    )(provider._hermes_home, provider._storage_dir)
    provider._db_path = provider._storage_dir / "memory.sqlite3"
    load_config = _module_attr(provider, "load_runtime_config", load_runtime_config)
    provider._config = load_config(provider._plugin_dir, provider._storage_dir)
    provider._retrieval_config = dict(provider._config.get("retrieval") or {})
    provider._vector_config = dict(provider._config.get("vector") or {})
    provider._scope = _module_attr(provider, "normalize_scope_identity", normalize_scope_identity)(
        raw_scope, provider._config
    )
    provider._scope_id = _module_attr(provider, "build_scope_id", build_scope_id)(
        provider._scope, provider._config
    )
    provider._shared_scope_id = _module_attr(
        provider, "build_shared_scope_id", build_shared_scope_id
    )(provider._scope, provider._config)
    provider._accessible_scope_ids = _module_attr(
        provider, "accessible_scope_ids", accessible_scope_ids
    )(provider._scope, provider._config)
    provider._writable_scope_ids = _module_attr(
        provider, "writable_scope_ids", writable_scope_ids
    )(provider._scope, provider._config)
    bool_fn = _module_attr(provider, "config_bool", config_bool)
    raw_shared_pool_config = provider._config.get("shared_pool")
    shared_pool_config = raw_shared_pool_config if isinstance(raw_shared_pool_config, dict) else {}
    provider._shared_pool_enabled = bool_fn(shared_pool_config, "enabled", False)
    provider._shared_pool_write_enabled = provider._shared_pool_enabled and bool_fn(
        shared_pool_config, "write_enabled", False
    )
    provider._shared_pool_id = (
        str(shared_pool_config.get("pool_id") or "default") if provider._shared_pool_enabled else ""
    )
    provider._shared_pool_scope_id = (
        _module_attr(provider, "build_shared_pool_scope_id", build_shared_pool_scope_id)(
            provider._scope, provider._shared_pool_id
        )
        if provider._shared_pool_enabled
        else ""
    )
    if provider._shared_pool_scope_id and provider._shared_pool_scope_id not in provider._accessible_scope_ids:
        provider._accessible_scope_ids.append(provider._shared_pool_scope_id)
    if (
        provider._shared_pool_write_enabled
        and provider._shared_pool_scope_id
        and provider._shared_pool_scope_id not in provider._writable_scope_ids
    ):
        provider._writable_scope_ids.append(provider._shared_pool_scope_id)
    provider._current_turn = 0
    provider._last_recall_turns = {}

    lease_cls = _module_attr(provider, "TruthWriterLease", TruthWriterLease)
    lease = lease_cls(
        provider._storage_dir,
        role="provider",
    )
    lease_result = lease.acquire()
    if lease_result.get("status") != "acquired":
        provider._truth_writer_role = "reader"
        owner = lease_result.get("owner")
        provider._truth_writer_owner = _module_attr(
            provider, "sanitized_truth_writer_owner", sanitized_truth_writer_owner
        )(owner)
        logger.warning(
            "Scope Recall truth writer lease is held by another process; "
            "continuing in read-only recall mode (owner=%s)",
            json.dumps(provider._truth_writer_owner, ensure_ascii=False, sort_keys=True),
        )
        _call_provider(provider, "_initialize_read_only_runtime", default=lambda: initialize_read_only_runtime(provider))
        return
    provider._truth_writer_lease = lease
    provider._truth_writer_role = "owner"
    provider._truth_writer_owner = {}

    try:
        _call_provider(provider, "_initialize_writer_runtime", default=lambda: initialize_writer_runtime(provider))
    except BaseException:
        _call_provider(
            provider,
            "_cleanup_failed_writer_initialization",
            default=lambda: cleanup_failed_writer_initialization(provider),
        )
        raise


def initialize_writer_runtime(provider: Any) -> None:
    """Writer-role initialization: schema, backfills, vector, writer."""

    try:
        conn = _call_provider(provider, "_open_runtime_connection")
    except sqlite3.OperationalError as exc:
        if not _lock_contention(provider, exc):
            raise
        # Issue #43: a same-process peer provider holding a dirty write
        # transaction can block startup schema work. Roll back idle dirty
        # peers (nonblocking; busy peers are skipped) and retry once.
        recovery = _call_provider(provider, "_rollback_peer_provider_transactions", "initialize")
        rollbacks = int(recovery.get("peer_rollbacks", 0) or 0)
        busy = int(recovery.get("peer_busy_skipped", 0) or 0)
        errors = int(recovery.get("peer_rollback_errors", 0) or 0)
        if rollbacks <= 0 or busy > 0 or errors > 0:
            raise
        logger.warning(
            "Scope Recall initialize recovered from same-process peer "
            "SQLite lock contention; retrying startup once (%s)",
            json.dumps(recovery, sort_keys=True),
        )
        conn = _call_provider(provider, "_open_runtime_connection")
    provider._conn = conn
    freshness_limit = int(
        _module_attr(provider, "STARTUP_FRESHNESS_BACKFILL_LIMIT", DEFAULT_FRESHNESS_BACKFILL_LIMIT)
    )
    freshness_fn = _module_attr(
        provider, "backfill_untracked_memory_freshness", backfill_untracked_memory_freshness
    )
    try:
        provider._freshness_backfill = freshness_fn(
            conn,
            apply=True,
            limit=freshness_limit,
        )
    except sqlite3.OperationalError as exc:
        if not _lock_contention(provider, exc):
            _call_provider(
                provider,
                "_close_published_connection",
                conn,
                context="startup freshness backfill",
            )
            raise
        _call_provider(provider, "_rollback_conn_after_error", "startup freshness backfill contention")
        provider._freshness_backfill = {
            "apply": True,
            "status": "deferred_error",
            "error_type": type(exc).__name__,
        }
        logger.warning(
            "Scope Recall startup freshness backfill deferred after SQLite lock contention"
        )
    except BaseException:
        _call_provider(
            provider,
            "_close_published_connection",
            conn,
            context="startup writer initialization",
        )
        raise
    try:
        _module_attr(provider, "backfill_skill_anchors", backfill_skill_anchors)(conn)
    except Exception:
        _call_provider(provider, "_rollback_conn_after_error", "skill-anchor backfill")
        logger.exception("Scope Recall skill-anchor backfill failed")
    _module_attr(provider, "finish_writer_schema_setup", finish_writer_schema_setup)(
        provider,
        conn,
        schema_fn=_module_attr(provider, "ensure_schema", ensure_schema),
        journal_fn=_module_attr(provider, "ensure_journal_schema", ensure_journal_schema),
        ensure_triggers_fn=_module_attr(
            provider, "ensure_activation_guard_triggers", ensure_activation_guard_triggers
        ),
    )
    _attach_writer_runtime(provider)
    _call_provider(provider, "_register_provider_instance")
    provider._runtime_status = _module_attr(provider, "RUNTIME_STATUS_ACTIVE", RUNTIME_STATUS_ACTIVE)
    has_journal = getattr(provider, "_has_unprocessed_journal", None)
    if callable(has_journal) and has_journal():
        _call_provider(provider, "_maybe_start_background_journal_digest")


def cleanup_failed_writer_initialization(
    provider: Any, *, reraise_companion_errors: bool = False
) -> bool:
    """Abandon a partial writer without hiding the original error."""

    writer_stopped = True
    try:
        _module_attr(provider, "shutdown_writer", shutdown_writer)(provider, timeout=3.0)
    except Exception:
        logger.exception(
            "Scope Recall failed-initialization writer quiesce failed"
        )
        thread = getattr(provider, "_writer_thread", None)
        writer_stopped = thread is None or not thread.is_alive()

    try:
        digest = getattr(provider, "_journal_digest_thread", None)
        if (
            digest is not None
            and digest.is_alive()
            and digest is not threading.current_thread()
        ):
            digest.join(timeout=3.0)
    except Exception:
        logger.exception(
            "Scope Recall failed-initialization digest join failed"
        )
    digest = getattr(provider, "_journal_digest_thread", None)
    digest_stopped = digest is None or not digest.is_alive()
    if not writer_stopped or not digest_stopped:
        provider._truth_writer_role = "unknown"
        return False

    vector_error: Exception | None = None
    vector = getattr(provider, "_vector_store", None)
    if vector is not None:
        try:
            vector.close()
        except Exception as exc:
            logger.exception(
                "Scope Recall failed-initialization vector close failed"
            )
            vector_error = exc
        provider._vector_store = None

    try:
        close_embedder(getattr(provider, "_embedder", None))
    except Exception:
        logger.exception(
            "Scope Recall failed-initialization embedder close failed"
        )
    provider._embedder = None

    try:
        with provider._lock:
            conn = provider._conn
            if conn is not None:
                try:
                    if bool(getattr(conn, "in_transaction", False)):
                        conn.rollback()
                except Exception:
                    logger.exception(
                        "Scope Recall failed-initialization SQLite rollback failed"
                    )
                _call_provider(
                    provider,
                    "_close_published_connection",
                    conn,
                    context="failed-initialization",
                    reraise=False,
                )
    except Exception:
        logger.exception(
            "Scope Recall failed-initialization SQLite close failed"
        )

    try:
        _call_provider(provider, "_unregister_provider_instance")
    except Exception:
        logger.exception(
            "Scope Recall failed-initialization registry clear failed"
        )

    completed = False
    if provider._conn is not None:
        provider._truth_writer_role = "unknown"
    else:
        lease = provider._truth_writer_lease
        if lease is None:
            provider._truth_writer_role = "unknown"
            completed = True
        else:
            try:
                lease.release()
            except Exception:
                logger.exception(
                    "Scope Recall failed-initialization writer-lease release failed"
                )
                provider._truth_writer_role = "unknown"
            else:
                provider._truth_writer_lease = None
                provider._truth_writer_role = "unknown"
                completed = True
    if reraise_companion_errors and completed and vector_error is not None:
        raise vector_error
    return completed


def initialize_read_only_runtime(provider: Any) -> None:
    """Finish initialization as a read-only recall peer.

    Another process holds the truth writer lease. Opening a second write
    pager here is the exact multi-process overlap that produced issue
    #39's corruptions, so this runtime keeps recall available on a
    ``mode='ro'`` + ``PRAGMA query_only`` connection and disables every
    write surface: capture, journal, digests, vector mutation, and write
    tools. The SQLite read-only connection is the hard
    backstop for any write path this gate misses.
    """

    provider._truth_writer_lease = None
    try:
        db_path = getattr(provider, "_db_path", None)
        if db_path is not None and Path(db_path).is_file():
            opener = _module_attr(
                provider, "open_readonly_truth_connection", open_readonly_truth_connection
            )
            provider._conn = opener(
                db_path,
                timeout=float(
                    _module_attr(
                        provider, "SQLITE_BUSY_TIMEOUT_SECONDS", DEFAULT_BUSY_TIMEOUT_SECONDS
                    )
                ),
                connect_fn=_module_attr(provider, "connect_truth_database", connect_truth_database),
            )
        else:
            provider._conn = None
    except Exception:
        logger.exception(
            "Scope Recall read-only runtime could not open the truth database"
        )
        provider._conn = None
    provider._vector_enabled = False
    provider._vector_ready = False
    provider._vector_status = "disabled"
    provider._vector_reason_code = "reader_mode"
    provider._vector_auto_recoverable = False
    provider._vector_repair_required = False
    provider._vector_usable_for_query = False
    provider._vector_debt_counts = {
        "pending": 0,
        "processing": 0,
        "retry": 0,
        "dead_letter": 0,
        "replayable": 0,
    }
    provider._vector_message = (
        "vector companion is owned by the writer-lease process"
    )
    _call_provider(provider, "_register_provider_instance")
    provider._runtime_status = _module_attr(
        provider, "RUNTIME_STATUS_ACTIVE_READ_ONLY", RUNTIME_STATUS_ACTIVE_READ_ONLY
    )


def promote_reader_to_writer(provider: Any) -> None:
    """Rebind a read-only runtime as the writer once the lease frees."""

    lifecycle_lock = getattr(provider, "_writer_lifecycle_lock", None)
    if lifecycle_lock is not None:
        with lifecycle_lock:
            _promote_under_lifecycle_lock(provider)
        return
    _promote_under_lifecycle_lock(provider)


def _promote_under_lifecycle_lock(provider: Any) -> None:
    shutdown = getattr(provider, "_shutdown_requested", None)
    is_set = getattr(shutdown, "is_set", None)
    if callable(is_set) and is_set():
        return
    if getattr(provider, "_truth_writer_role", None) != "reader" or getattr(provider, "_storage_dir", None) is None:
        return
    lease_cls = _module_attr(provider, "TruthWriterLease", TruthWriterLease)
    lease = lease_cls(provider._storage_dir, role="provider")
    try:
        result = lease.acquire()
    except Exception:
        logger.exception("Scope Recall writer-lease promotion probe failed")
        return
    if result.get("status") != "acquired":
        return
    logger.info(
        "Scope Recall truth writer lease became available; promoting this "
        "read-only runtime to the writer role"
    )
    try:
        provider._truth_writer_lease = lease
        provider._truth_writer_role = "owner"
        provider._truth_writer_owner = {}
        with provider._lock:
            if provider._conn is not None:
                _call_provider(
                    provider,
                    "_close_published_connection",
                    provider._conn,
                    context="reader promotion close",
                )
        _call_provider(provider, "_initialize_writer_runtime", default=lambda: initialize_writer_runtime(provider))
    except BaseException:
        cleaned = _call_provider(
            provider,
            "_cleanup_failed_writer_initialization",
            default=lambda: cleanup_failed_writer_initialization(provider),
        )
        logger.exception(
            "Scope Recall writer promotion failed; staying in read-only mode"
        )
        if not cleaned:
            return
        provider._truth_writer_role = "reader"
        _call_provider(provider, "_initialize_read_only_runtime", default=lambda: initialize_read_only_runtime(provider))


def shutdown_provider_process(provider: Any, *, timeout: float = 3.0) -> None:
    """Quiesce writer and digest work, then close shared resources.

    Writer and digest shutdown is a fail-closed barrier: if a worker does
    not acknowledge stop, SQLite, vector, the peer registry, the writer
    lease, and ``finalized`` stay in place so a later retry can finish.
    After workers stop, incomplete truth teardown keeps the published
    connection and lease and raises so callers cannot treat a fail-closed
    retain as a finished shutdown.
    """

    disabled = getattr(provider, "_runtime_memory_disabled", None)
    if callable(disabled) and disabled():
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if shutdown_requested is not None:
            shutdown_requested.set()
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.set()
        _call_provider(provider, "_unregister_provider_instance")
        provider._shutdown_finalized = True
        return

    deadline = time.monotonic() + max(0.0, float(timeout))
    submission_lock = getattr(provider, "_capture_submission_lock", None)
    lifecycle_lock = getattr(provider, "_writer_lifecycle_lock", None)
    submission_acquired = False
    lifecycle_acquired = False
    try:
        submission_acquired = _acquire_shutdown_lock(
            submission_lock, deadline=deadline, name="capture submission"
        )
        lifecycle_acquired = _acquire_shutdown_lock(
            lifecycle_lock, deadline=deadline, name="writer lifecycle"
        )
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if shutdown_requested is not None:
            shutdown_requested.set()
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.set()
    finally:
        if lifecycle_acquired:
            assert lifecycle_lock is not None
            lifecycle_lock.release()
        if submission_acquired:
            assert submission_lock is not None
            submission_lock.release()

    if bool(getattr(provider, "_shutdown_finalized", False)):
        return

    writer_error: Exception | None = None
    try:
        _shutdown_writer(provider, _remaining(deadline))
    except Exception as exc:
        writer_error = exc

    digest_error: Exception | None = None
    try:
        _background_work(provider).join_digest(_remaining(deadline))
    except Exception as exc:
        digest_error = exc
    if writer_error is not None:
        raise writer_error
    if digest_error is not None:
        raise digest_error

    _join_shutdown_cleanup(provider, deadline=deadline)
    provider._shutdown_finalized = True


class ProcessLifecycle:
    """Held process-lifecycle entry. Initialize, promote, and shutdown use this owner."""

    def initialize(self, provider: Any, session_id: str, **kwargs: Any) -> None:
        initialize_provider_process(provider, session_id, **kwargs)

    def has_live_initialize_runtime(self, provider: Any) -> bool:
        return has_live_initialize_runtime(provider)

    def initialize_under_lifecycle_lock(self, provider: Any, session_id: str, **kwargs: Any) -> None:
        initialize_under_lifecycle_lock(provider, session_id, **kwargs)

    def initialize_writer_runtime(self, provider: Any) -> None:
        initialize_writer_runtime(provider)

    def initialize_read_only_runtime(self, provider: Any) -> None:
        initialize_read_only_runtime(provider)

    def cleanup_failed_writer_initialization(
        self, provider: Any, *, reraise_companion_errors: bool = False
    ) -> bool:
        return cleanup_failed_writer_initialization(
            provider, reraise_companion_errors=reraise_companion_errors
        )

    def promote_to_writer(self, provider: Any) -> None:
        promote_reader_to_writer(provider)

    def shutdown(self, provider: Any, *, timeout: float = 3.0) -> None:
        shutdown_provider_process(provider, timeout=timeout)
