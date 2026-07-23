"""Asynchronous capture writer for current-turn memory rows.

The provider queues capture work here so tool latency stays low, while the synchronous helpers remain available for tests and explicit writes."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Any, Callable

from .capture_filters import should_capture_text
from .freshness import upsert_memory_freshness
from .models import recall_scope_mode
from .relation_frequency_maintenance import (
    drain_relation_frequency_work,
    relation_frequency_debt_exists,
)
from .relation_rebuild_queue import (
    drain_relation_rebuild_queue,
    relation_rebuild_debt_exists,
)
from .scope import canonical_user_id
from .sql_store import store_row
from .vector_runtime import replay_vector_outbox

logger = logging.getLogger(__name__)


def _writer_lifecycle_lock(provider: Any):
    """Return the provider's enqueue/shutdown gate, with legacy-test fallback."""

    return getattr(provider, "_writer_lifecycle_lock", None) or nullcontext()


def _drain_relation_rebuild_debt(provider: Any) -> None:
    """Use the existing writer thread to process one bounded relation chunk."""

    maintenance_stop = getattr(provider, "_maintenance_stop", None)
    if maintenance_stop is not None and maintenance_stop.is_set():
        return
    if not bool(provider._config.get("relation_extraction_enabled", True)):
        return
    configured_pairs = int(
        provider._config.get("relation_rebuild_chunk_pairs", 250) or 250
    )
    pair_limit = max(1, min(configured_pairs, 1000))
    if (
        getattr(provider, "_vector_ready", False)
        and getattr(provider, "_vector_store", None) is not None
        and getattr(provider, "_embedder", None) is not None
    ):
        try:
            from .vector_runtime import run_bounded_vector_reconciliation

            vector_result = run_bounded_vector_reconciliation(provider)
            if int(vector_result.get("failed") or 0):
                logger.warning(
                    "Scope Recall bounded vector maintenance failed: %s",
                    vector_result,
                )
        except Exception:
            logger.warning(
                "Scope Recall bounded vector maintenance tick failed",
                exc_info=True,
            )
    if maintenance_stop is not None and maintenance_stop.is_set():
        return
    with provider._lock:
        conn = provider._require_conn()
        if relation_frequency_debt_exists(conn):
            drain_relation_frequency_work(
                conn,
                change_limit=pair_limit,
                backfill_limit=pair_limit,
                reclassification_limit=pair_limit,
                commit=True,
            )
        if not relation_rebuild_debt_exists(conn):
            return
        result = drain_relation_rebuild_queue(
            conn,
            max_events=1,
            pair_limit=pair_limit,
            lease_seconds=120,
        )
    if int(result.get("failed", 0) or 0):
        logger.warning("Scope Recall relation rebuild chunk failed: %s", result)


def start_writer(provider: Any) -> None:
    with _writer_lifecycle_lock(provider):
        if provider._writer_thread and provider._writer_thread.is_alive():
            return
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if shutdown_requested is not None:
            shutdown_requested.clear()
        provider._stop.clear()
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.clear()
        provider._writer_thread = threading.Thread(
            target=writer_loop,
            args=(provider,),
            daemon=True,
            name="scope-recall-writer",
        )
        provider._writer_thread.start()


def writer_loop(provider: Any) -> None:
    while not provider._stop.is_set():
        try:
            job = provider._write_queue.get(timeout=0.2)
        except queue.Empty:
            maintenance_stop = getattr(provider, "_maintenance_stop", None)
            if provider._stop.is_set() or (
                maintenance_stop is not None and maintenance_stop.is_set()
            ):
                continue
            now = time.monotonic()
            last_drain = float(
                getattr(provider, "_last_relation_rebuild_drain", 0.0) or 0.0
            )
            if now - last_drain >= 1.0:
                provider._last_relation_rebuild_drain = now
                try:
                    _drain_relation_rebuild_debt(provider)
                except Exception:
                    rollback = getattr(provider, "_rollback_conn_after_error", None)
                    if callable(rollback):
                        rollback("relation rebuild background drain")
                    logger.exception("Scope Recall relation rebuild background drain failed")
            continue
        try:
            if job is None:
                return
            if job.get("kind") == "flush":
                event = job.get("event")
                result = job.get("result")
                with provider._lock:
                    failed_writes = int(
                        getattr(provider, "_writer_failed_writes", 0) or 0
                    )
                    reported_failures = int(
                        getattr(provider, "_writer_reported_failures", 0) or 0
                    )
                    success = failed_writes == reported_failures
                    provider._writer_reported_failures = failed_writes
                if isinstance(result, dict):
                    result["success"] = success
                if isinstance(event, threading.Event):
                    event.set()
                continue
            if job.get("kind") == "store":
                store_now(
                    provider,
                    content=job["content"],
                    source=job["source"],
                    target=job["target"],
                    session_id=job.get("session_id") or provider._session_id,
                    metadata=job.get("metadata") or {},
                )
        except Exception as exc:
            rollback = getattr(provider, "_rollback_conn_after_error", None)
            if callable(rollback):
                rollback("background writer")
            with provider._lock:
                provider._writer_failed_writes = (
                    int(getattr(provider, "_writer_failed_writes", 0) or 0) + 1
                )
                provider._writer_last_error_type = type(exc).__name__
            logger.exception("Scope Recall background write failed")
        finally:
            provider._write_queue.task_done()


def flush_writer(provider: Any, timeout: float = 2.0) -> bool:
    thread = provider._writer_thread
    if thread is None:
        return provider._write_queue.empty()
    if not thread.is_alive():
        return False
    done = threading.Event()
    result: dict[str, bool] = {}
    provider._write_queue.put({"kind": "flush", "event": done, "result": result})
    if not done.wait(timeout=timeout):
        return False
    return bool(result.get("success", False))


def shutdown_writer(provider: Any, timeout: float = 3.0) -> None:
    with _writer_lifecycle_lock(provider):
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if shutdown_requested is not None:
            shutdown_requested.set()
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.set()
    if not flush_writer(provider, timeout=timeout):
        raise RuntimeError("Scope Recall writer did not acknowledge the shutdown flush")
    provider._stop.set()
    thread = provider._writer_thread
    if thread is not None and thread.is_alive():
        provider._write_queue.put(None)
        thread.join(timeout=timeout)
    if thread is not None and thread.is_alive():
        raise RuntimeError("Scope Recall writer did not stop before resource teardown")
    provider._writer_thread = None


def enqueue_store(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not should_capture_text(content, provider._config).allowed:
        return
    with _writer_lifecycle_lock(provider):
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if (
            (shutdown_requested is not None and shutdown_requested.is_set())
            or provider._stop.is_set()
        ):
            raise RuntimeError("Scope Recall writer is shutting down")
        provider._write_queue.put(
            {
                "kind": "store",
                "content": content,
                "source": source,
                "target": target,
                "session_id": session_id,
                "metadata": metadata or {},
            }
        )


def store_now(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
    scope_mode: str | None = None,
    before_commit: Callable[[sqlite3.Connection, str], dict[str, Any] | None]
    | None = None,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Synchronously store one capture row through the provider database.

    This is the direct write path used by tests and queue workers, so it must preserve duplicate checks and metadata hygiene."""
    if not should_capture_text(content, provider._config).allowed:
        return "", False, None
    conn = provider._require_conn()
    memory_id = uuid.uuid4().hex
    requested_scope_mode = str(scope_mode or "").strip().lower()
    if requested_scope_mode not in {"shared", "local", "shared_pool"}:
        requested_scope_mode = recall_scope_mode(target, source)
    row_scope_id = provider._scope_id
    if requested_scope_mode == "shared":
        row_scope_id = provider._shared_scope_id
    elif requested_scope_mode == "shared_pool":
        row_scope_id = provider._shared_pool_scope_id
    metadata_payload = dict(metadata or {})
    metadata_payload.setdefault("scope_mode", requested_scope_mode)
    metadata_payload.setdefault("runtime_scope_id", provider._scope_id)
    metadata_payload.setdefault("shared_scope_id", provider._shared_scope_id)
    metadata_payload.setdefault("raw_platform", provider._scope.platform)
    metadata_payload.setdefault("raw_user_id", provider._scope.user_id)
    canonical = canonical_user_id(provider._scope, provider._config)
    if canonical:
        metadata_payload.setdefault("canonical_user", canonical)
        metadata_payload.setdefault("scope_identity_mode", "canonical")
    metadata_json = json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True)
    companion_result: dict[str, Any] | None = None
    with provider._lock:
        try:
            memory_id, _summary, _updated_at, inserted = store_row(
                conn,
                memory_id=memory_id,
                scope_id=row_scope_id,
                platform=provider._scope.platform,
                user_id=provider._scope.user_id,
                chat_id=provider._scope.chat_id,
                thread_id=provider._scope.thread_id,
                gateway_session_key=provider._scope.gateway_session_key,
                agent_identity=provider._scope.agent_identity,
                agent_workspace=provider._scope.agent_workspace,
                session_id=session_id,
                source=source,
                target=target,
                content=content,
                metadata=metadata_json,
                allow_duplicate=allow_duplicate
                or str(source).startswith("legacy-"),
                commit=False,
            )
            if inserted:
                conn.execute("SAVEPOINT scope_recall_capture_freshness")
                try:
                    upsert_memory_freshness(
                        conn,
                        memory_id=memory_id,
                        metadata=metadata_payload,
                        content=content,
                        commit=False,
                    )
                except Exception as exc:
                    conn.execute("ROLLBACK TO scope_recall_capture_freshness")
                    conn.execute("RELEASE scope_recall_capture_freshness")
                    provider._freshness_write_failures = (
                        int(getattr(provider, "_freshness_write_failures", 0) or 0)
                        + 1
                    )
                    provider._freshness_last_error_type = type(exc).__name__
                    logger.exception("Scope Recall freshness companion write failed")
                else:
                    conn.execute("RELEASE scope_recall_capture_freshness")
                if before_commit is not None:
                    companion_result = before_commit(conn, memory_id)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    replay_vector_outbox(provider)
    return memory_id, inserted, companion_result
