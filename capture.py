"""Asynchronous capture writer for current-turn memory rows.

The provider queues capture work here so tool latency stays low, while the synchronous helpers remain available for tests and explicit writes."""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from typing import Any

from .capture_filters import should_capture_text
from .freshness import upsert_memory_freshness
from .models import recall_scope_mode
from .scope import canonical_user_id
from .sql_store import store_row
from .vector_runtime import upsert_vector_record

logger = logging.getLogger(__name__)


def start_writer(provider: Any) -> None:
    if provider._writer_thread and provider._writer_thread.is_alive():
        return
    provider._stop.clear()
    provider._writer_thread = threading.Thread(target=writer_loop, args=(provider,), daemon=True, name="scope-recall-writer")
    provider._writer_thread.start()


def writer_loop(provider: Any) -> None:
    while not provider._stop.is_set():
        try:
            job = provider._write_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            if job is None:
                return
            if job.get("kind") == "flush":
                event = job.get("event")
                result = job.get("result")
                with provider._lock:
                    failed_writes = int(getattr(provider, "_writer_failed_writes", 0) or 0)
                    reported_failures = int(getattr(provider, "_writer_reported_failures", 0) or 0)
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
                provider._writer_failed_writes = int(getattr(provider, "_writer_failed_writes", 0) or 0) + 1
                provider._writer_last_error_type = type(exc).__name__
            logger.exception("Scope Recall background write failed")
        finally:
            provider._write_queue.task_done()


def flush_writer(provider: Any, timeout: float = 2.0) -> bool:
    if not provider._writer_thread:
        return True
    done = threading.Event()
    result: dict[str, bool] = {}
    provider._write_queue.put({"kind": "flush", "event": done, "result": result})
    if not done.wait(timeout=timeout):
        return False
    return bool(result.get("success", False))


def shutdown_writer(provider: Any, timeout: float = 3.0) -> None:
    flush_writer(provider, timeout=timeout)
    provider._stop.set()
    if provider._writer_thread and provider._writer_thread.is_alive():
        provider._write_queue.put(None)
        provider._writer_thread.join(timeout=timeout)
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
) -> tuple[str, bool]:
    """Synchronously store one capture row through the provider database.

    This is the direct write path used by tests and queue workers, so it must preserve duplicate checks and metadata hygiene."""
    if not should_capture_text(content, provider._config).allowed:
        return "", False
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
    with provider._lock:
        memory_id, summary, updated_at, inserted = store_row(
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
            allow_duplicate=allow_duplicate or str(source).startswith("legacy-"),
        )
    if inserted:
        try:
            with provider._lock:
                upsert_memory_freshness(conn, memory_id=memory_id, metadata=metadata_payload)
        except Exception as exc:
            with provider._lock:
                conn.rollback()
                provider._freshness_write_failures = int(getattr(provider, "_freshness_write_failures", 0) or 0) + 1
                provider._freshness_last_error_type = type(exc).__name__
            logger.exception("Scope Recall freshness companion write failed")
        upsert_vector_record(
            provider,
            id=memory_id,
            source=source,
            target=target,
            content=content,
            summary=summary,
            updated_at=updated_at,
            scope_id=row_scope_id,
        )
    return memory_id, inserted
