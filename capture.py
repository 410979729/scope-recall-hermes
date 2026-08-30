"""Bounded process-local asynchronous capture writer.

Capture payloads are sanitized before enqueue and live only until the writer
consumes them.  Synchronous helpers remain available for explicit writes and
tests.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .capture_control import (
    CONTROL_PUT_TIMEOUT_SECONDS,
    bind_write_queue,
    capture_queue_report as _process_capture_queue_report,
    put_work,
    queue_capacity,
    queue_maxsize,
)
from .capture_filters import (
    sanitize_capture_text,
    sanitize_structured_value,
    should_capture_text,
)
from .capture_outcomes import ensure_outcome_accounted, handle_capture_enqueue
from .gating import config_bool
from .memory_mutation import MemoryMutationService
from .models import recall_scope_mode
from .relation_frequency_maintenance import drain_relation_frequency_work
from .relation_containment import record_relation_lock_contention
from .scope import canonical_user_id
from .sql_store import store_row
from .sqlite_recovery import is_sqlite_lock_contention
from .vector_runtime import replay_vector_outbox
from . import write_kernel as write_kernel_mod
from .write_kernel import (
    WRITE_AUTHORITY_BUSY,
    _admitted_truth_mutation,
    _writer_lifecycle_lock,
    has_positive_write_authority,
    hold_positive_write_authority,
    require_positive_write_authority,
)

_WRITE_KERNEL_REEXPORT_COMPAT = (
    WRITE_AUTHORITY_BUSY,
    hold_positive_write_authority,
    require_positive_write_authority,
)
logger = logging.getLogger(__name__)


def capture_queue_report(provider: Any) -> dict[str, Any]:
    """Expose bounded process-local queue status to runtime diagnostics."""

    return _process_capture_queue_report(provider)


@dataclass(frozen=True)
class CaptureAuthorizationEnvelope:
    """Immutable enqueue-time routing and identity authority for one capture."""

    scope_mode: str
    row_scope_id: str
    runtime_scope_id: str
    shared_scope_id: str
    shared_pool_scope_id: str
    platform: str
    user_id: str
    chat_id: str
    thread_id: str
    gateway_session_key: str
    agent_identity: str
    agent_workspace: str
    canonical_user: str


@contextmanager
def _capture_submission_lock(provider: Any) -> Iterator[None]:
    """Serialize capture publication with explicit forget/delete mutations."""

    lock = getattr(provider, "_capture_submission_lock", None)
    if lock is None:
        yield
        return
    with lock:
        yield


def _resolve_capture_authorization(
    provider: Any,
    *,
    target: str,
    source: str,
    scope_mode: str | None = None,
) -> CaptureAuthorizationEnvelope:
    requested_scope_mode = str(scope_mode or "").strip().lower()
    if requested_scope_mode not in {"shared", "local", "shared_pool"}:
        requested_scope_mode = recall_scope_mode(target, source)
    runtime_scope_id = str(getattr(provider, "_scope_id", "") or "")
    shared_scope_id = str(getattr(provider, "_shared_scope_id", "") or "")
    shared_pool_scope_id = str(
        getattr(provider, "_shared_pool_scope_id", "") or ""
    )
    row_scope_id = runtime_scope_id
    if requested_scope_mode == "shared":
        row_scope_id = shared_scope_id
    elif requested_scope_mode == "shared_pool":
        row_scope_id = shared_pool_scope_id
    scope = provider._scope
    return CaptureAuthorizationEnvelope(
        scope_mode=requested_scope_mode,
        row_scope_id=row_scope_id,
        runtime_scope_id=runtime_scope_id,
        shared_scope_id=shared_scope_id,
        shared_pool_scope_id=shared_pool_scope_id,
        platform=str(getattr(scope, "platform", "") or ""),
        user_id=str(getattr(scope, "user_id", "") or ""),
        chat_id=str(getattr(scope, "chat_id", "") or ""),
        thread_id=str(getattr(scope, "thread_id", "") or ""),
        gateway_session_key=str(
            getattr(scope, "gateway_session_key", "") or ""
        ),
        agent_identity=str(getattr(scope, "agent_identity", "") or ""),
        agent_workspace=str(getattr(scope, "agent_workspace", "") or ""),
        canonical_user=str(canonical_user_id(scope, provider._config) or ""),
    )


_ACCEPTED_CAPTURE_DRAIN_TOKEN = object()


@contextmanager
def _capture_store_authority(
    provider: Any, *, accepted_capture_token: object | None = None
) -> Iterator[None]:
    """Hold lifecycle authority for one truth-store unit.

    An accepted queue item may finish during shutdown flush, but it must still
    belong to the current owner process.  New direct writes fail closed after
    shutdown is published.
    """

    if accepted_capture_token is None:
        with write_kernel_mod.command_write_access(provider):
            yield
        return

    if accepted_capture_token is not _ACCEPTED_CAPTURE_DRAIN_TOKEN:
        raise RuntimeError(WRITE_AUTHORITY_BUSY)

    from ._internal.runtime.writer_handoff import active_truth_work

    with _writer_lifecycle_lock(provider):
        if getattr(provider, "_truth_writer_role", None) != "owner":
            raise RuntimeError(WRITE_AUTHORITY_BUSY)
        with _admitted_truth_mutation(provider):
            with active_truth_work(provider):
                yield


def _drain_relation_rebuild_debt(provider: Any) -> None:
    """Run one bounded idle tick without waiting behind foreground authority."""

    from ._internal.runtime.writer_handoff import active_truth_work

    lifecycle_lock = _writer_lifecycle_lock(provider)
    acquire = getattr(lifecycle_lock, "acquire", None)
    release = getattr(lifecycle_lock, "release", None)
    if callable(acquire) and callable(release):
        if not bool(acquire(blocking=False)):
            provider._relation_maintenance_lock_contention_skips = int(
                getattr(provider, "_relation_maintenance_lock_contention_skips", 0)
                or 0
            ) + 1
            provider._relation_maintenance_consecutive_failures = int(
                getattr(provider, "_relation_maintenance_consecutive_failures", 0)
                or 0
            ) + 1
            return
        try:
            with active_truth_work(provider):
                _drain_relation_rebuild_debt_locked(provider)
        finally:
            release()
        return
    with lifecycle_lock:
        with active_truth_work(provider):
            _drain_relation_rebuild_debt_locked(provider)


def _drain_relation_rebuild_debt_locked(provider: Any) -> None:
    """Run vector maintenance independently from optional relation extraction."""

    if not has_positive_write_authority(provider):
        return
    maintenance_stop = getattr(provider, "_maintenance_stop", None)
    if maintenance_stop is not None and maintenance_stop.is_set():
        return

    # Vector reconciliation, like relation maintenance below, must never adopt
    # a transaction opened by a foreground caller.  Check the published
    # connection before either lane while the lifecycle gate is held.  The
    # provider lock remains nonblocking so idle maintenance cannot queue behind
    # foreground work.
    require_conn = getattr(provider, "_require_conn", None)
    if callable(require_conn):
        provider_lock = getattr(provider, "_lock", None)
        acquired = True
        if provider_lock is not None:
            acquired = bool(provider_lock.acquire(blocking=False))
        if not acquired:
            setattr(
                provider,
                "_relation_maintenance_lock_contention_skips",
                int(
                    getattr(provider, "_relation_maintenance_lock_contention_skips", 0)
                    or 0
                )
                + 1,
            )
            setattr(
                provider,
                "_relation_maintenance_consecutive_failures",
                int(
                    getattr(provider, "_relation_maintenance_consecutive_failures", 0)
                    or 0
                )
                + 1,
            )
            return
        try:
            conn = require_conn()
            if bool(getattr(conn, "in_transaction", False)):
                setattr(
                    provider,
                    "_relation_maintenance_busy_skips",
                    int(getattr(provider, "_relation_maintenance_busy_skips", 0) or 0)
                    + 1,
                )
                setattr(
                    provider,
                    "_relation_maintenance_consecutive_failures",
                    int(
                        getattr(
                            provider, "_relation_maintenance_consecutive_failures", 0
                        )
                        or 0
                    )
                    + 1,
                )
                return
        finally:
            if provider_lock is not None:
                provider_lock.release()

    if (
        getattr(provider, "_vector_store", None) is not None
        and getattr(provider, "_embedder", None) is not None
        and bool(getattr(provider, "_vector_generation_id", ""))
    ):
        try:
            from .vector_runtime import run_bounded_vector_reconciliation

            vector_result = run_bounded_vector_reconciliation(provider)
            if (
                str(vector_result.get("status") or "").strip().lower() == "failed"
                or int(vector_result.get("failed") or 0)
            ):
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
    if not has_positive_write_authority(provider):
        return
    if not bool(provider._config.get("relation_extraction_enabled", True)):
        return
    write_queue = getattr(provider, "_write_queue", None)
    if write_queue is not None and callable(getattr(write_queue, "qsize", None)):
        if int(write_queue.qsize() or 0) > 0:
            provider._relation_maintenance_busy_skips = int(
                getattr(provider, "_relation_maintenance_busy_skips", 0) or 0
            ) + 1
            provider._relation_maintenance_consecutive_failures = int(
                getattr(provider, "_relation_maintenance_consecutive_failures", 0)
                or 0
            ) + 1
            return
    if int(getattr(provider, "_capture_queue_processing", 0) or 0) > 0:
        provider._relation_maintenance_busy_skips = int(
            getattr(provider, "_relation_maintenance_busy_skips", 0) or 0
        ) + 1
        provider._relation_maintenance_consecutive_failures = int(
            getattr(provider, "_relation_maintenance_consecutive_failures", 0)
            or 0
        ) + 1
        return

    config = provider._config
    configured_pairs = int(config.get("relation_rebuild_chunk_pairs", 250) or 250)
    pair_limit = max(1, min(configured_pairs, 1000))
    wall_clock_seconds = max(
        0.05,
        min(float(config.get("relation_maintenance_wall_clock_seconds", 0.5) or 0.5), 10.0),
    )
    deadline = time.monotonic() + wall_clock_seconds
    provider_lock = getattr(provider, "_lock", None)
    acquired = True
    if provider_lock is not None:
        acquired = bool(provider_lock.acquire(blocking=False))
    if not acquired:
        provider._relation_maintenance_lock_contention_skips = int(
            getattr(provider, "_relation_maintenance_lock_contention_skips", 0) or 0
        ) + 1
        provider._relation_maintenance_consecutive_failures = int(
            getattr(provider, "_relation_maintenance_consecutive_failures", 0) or 0
        ) + 1
        return
    try:
        conn = provider._require_conn()
        # The shared provider connection can carry a transaction opened by a
        # foreground caller after it releases the provider lock.  Idle
        # maintenance must not adopt that transaction: the bounded drain uses
        # ``commit=True`` and would otherwise commit work it does not own.
        if bool(getattr(conn, "in_transaction", False)):
            setattr(
                provider,
                "_relation_maintenance_busy_skips",
                int(getattr(provider, "_relation_maintenance_busy_skips", 0) or 0) + 1,
            )
            setattr(
                provider,
                "_relation_maintenance_consecutive_failures",
                int(
                    getattr(provider, "_relation_maintenance_consecutive_failures", 0)
                    or 0
                )
                + 1,
            )
            return
        deferred_contention = int(
            getattr(provider, "_relation_maintenance_lock_contention_skips", 0) or 0
        )
        if deferred_contention:
            record_relation_lock_contention(
                conn,
                scope_ids=getattr(provider, "_accessible_scope_ids", None),
                count=deferred_contention,
            )
            provider._relation_maintenance_lock_contention_skips = 0
        result = drain_relation_frequency_work(
            conn,
            change_limit=pair_limit,
            focus_limit=pair_limit,
            backfill_limit=pair_limit,
            reclassification_limit=pair_limit,
            relation_candidate_cap=max(
                1,
                min(
                    int(config.get("relation_reclassification_candidate_cap", 250) or 250),
                    5000,
                ),
            ),
            relation_max_attempts=max(
                1,
                min(int(config.get("relation_maintenance_max_attempts", 5) or 5), 20),
            ),
            relation_policy_generation_enabled=config_bool(
                config, "relation_policy_generation_enabled", False
            ),
            wall_clock_seconds=wall_clock_seconds,
            backoff_base_seconds=max(
                0.1,
                float(config.get("relation_maintenance_backoff_base_seconds", 5.0) or 5.0),
            ),
            backoff_max_seconds=max(
                float(config.get("relation_maintenance_backoff_base_seconds", 5.0) or 5.0),
                float(config.get("relation_maintenance_backoff_max_seconds", 300.0) or 300.0),
            ),
            deadline_monotonic=deadline,
            commit=True,
        )
        provider._relation_maintenance_consecutive_failures = 0
    except Exception as exc:
        if is_sqlite_lock_contention(exc):
            provider._relation_maintenance_lock_contention_skips = int(
                getattr(provider, "_relation_maintenance_lock_contention_skips", 0)
                or 0
            ) + 1
        provider._relation_maintenance_consecutive_failures = int(
            getattr(provider, "_relation_maintenance_consecutive_failures", 0) or 0
        ) + 1
        raise
    finally:
        if provider_lock is not None:
            provider_lock.release()
    if int(result.get("failed", 0) or 0):
        logger.warning("Scope Recall relation containment tick failed: %s", result)


def start_writer(provider: Any) -> None:
    """Start the sole consumer after binding the configured finite queue."""

    with _capture_submission_lock(provider):
        with _writer_lifecycle_lock(provider):
            bind_write_queue(provider)
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


def _set_processing(provider: Any, delta: int) -> None:
    lock = getattr(provider, "_lock", None)
    if lock is None:
        provider._capture_queue_processing = max(
            0, int(getattr(provider, "_capture_queue_processing", 0) or 0) + delta
        )
        return
    with lock:
        provider._capture_queue_processing = max(
            0, int(getattr(provider, "_capture_queue_processing", 0) or 0) + delta
        )


def writer_loop(provider: Any) -> None:
    """Consume bounded process-local jobs; never acquire the submission lock."""

    # Shutdown is sentinel-driven.  ``_stop`` suppresses maintenance, but must
    # never let the consumer disappear before it acknowledges the queued
    # sentinel; otherwise that stale control item kills the next writer.
    while True:
        try:
            job = provider._write_queue.get(timeout=0.2)
        except queue.Empty:
            maintenance_stop = getattr(provider, "_maintenance_stop", None)
            if provider._stop.is_set() or (
                maintenance_stop is not None and maintenance_stop.is_set()
            ):
                continue
            now = time.monotonic()
            from ._internal.runtime.writer_handoff import (
                maybe_schedule_idle_writer_handoff,
            )

            if maybe_schedule_idle_writer_handoff(provider):
                continue
            last_drain = float(
                getattr(provider, "_last_relation_rebuild_drain", 0.0) or 0.0
            )
            config = getattr(provider, "_config", {}) or {}
            base_interval = max(
                1.0,
                min(
                    float(config.get("relation_maintenance_interval_seconds", 30.0) or 30.0),
                    3600.0,
                ),
            )
            failures = max(
                0,
                int(getattr(provider, "_relation_maintenance_consecutive_failures", 0) or 0),
            )
            backoff_base = max(
                0.1,
                float(config.get("relation_maintenance_backoff_base_seconds", 5.0) or 5.0),
            )
            backoff_max = max(
                backoff_base,
                float(config.get("relation_maintenance_backoff_max_seconds", 300.0) or 300.0),
            )
            retry_delay = min(backoff_max, backoff_base * (2 ** min(failures, 12)))
            maintenance_interval = max(base_interval, retry_delay if failures else 0.0)
            if now - last_drain >= maintenance_interval:
                provider._last_relation_rebuild_drain = now
                try:
                    _drain_relation_rebuild_debt(provider)
                except Exception:
                    rollback = getattr(provider, "_rollback_conn_after_error", None)
                    if callable(rollback):
                        rollback("capture writer maintenance")
                    logger.exception("Scope Recall writer maintenance failed")
            continue

        processing_store = False
        try:
            if job is None:
                return
            if not isinstance(job, dict):
                continue
            if job.get("kind") == "flush":
                event = job.get("event")
                result = job.get("result")
                try:
                    lock = getattr(provider, "_lock", None)
                    if lock is None:
                        failed_writes = int(
                            getattr(provider, "_writer_failed_writes", 0) or 0
                        )
                        reported_failures = int(
                            getattr(provider, "_writer_reported_failures", 0) or 0
                        )
                        success = failed_writes == reported_failures
                        provider._writer_reported_failures = failed_writes
                    else:
                        with lock:
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
                finally:
                    if isinstance(event, threading.Event):
                        event.set()
                continue
            if job.get("kind") == "store":
                processing_store = True
                _set_processing(provider, 1)
                store_now(
                    provider,
                    content=job["content"],
                    source=job["source"],
                    target=job["target"],
                    session_id=job.get("session_id") or "",
                    metadata=job.get("metadata") or {},
                    replay_vector=False,
                    _accepted_capture_token=_ACCEPTED_CAPTURE_DRAIN_TOKEN,
                    authorization=job.get("authorization"),
                )
        except Exception as exc:
            rollback = getattr(provider, "_rollback_conn_after_error", None)
            if callable(rollback):
                rollback("background writer")
            lock = getattr(provider, "_lock", None)
            if lock is None:
                provider._writer_failed_writes = (
                    int(getattr(provider, "_writer_failed_writes", 0) or 0) + 1
                )
                provider._writer_last_error_type = type(exc).__name__
            else:
                with lock:
                    provider._writer_failed_writes = (
                        int(getattr(provider, "_writer_failed_writes", 0) or 0) + 1
                    )
                    provider._writer_last_error_type = type(exc).__name__
            logger.exception("Scope Recall background write failed")
        finally:
            if processing_store:
                _set_processing(provider, -1)
            provider._write_queue.task_done()
            job = None


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


@contextmanager
def _provider_lock_until(
    provider: Any, attribute: str, deadline: float
) -> Iterator[None]:
    """Acquire one provider RLock without crossing the caller's deadline."""

    lock = getattr(provider, attribute, None)
    if lock is None:
        yield
        return
    remaining = _remaining(deadline)
    if remaining <= 0 or not lock.acquire(timeout=remaining):
        raise RuntimeError("Scope Recall writer did not acknowledge the shutdown flush")
    try:
        yield
    finally:
        lock.release()


def flush_writer(provider: Any, timeout: float = 2.0) -> bool:
    """Wait for all jobs published before the marker, bounded by one deadline."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    thread = getattr(provider, "_writer_thread", None)
    if thread is None:
        return bool(
            provider._write_queue.empty()
            and int(getattr(provider, "_capture_queue_processing", 0) or 0) == 0
        )
    if not thread.is_alive():
        return False
    done = threading.Event()
    result: dict[str, bool] = {}
    remaining = _remaining(deadline)
    if remaining <= 0:
        return False
    try:
        if not put_work(
            provider._write_queue,
            {"kind": "flush", "event": done, "result": result},
            timeout=min(CONTROL_PUT_TIMEOUT_SECONDS, remaining),
        ):
            return False
    except RuntimeError:
        return False
    remaining = _remaining(deadline)
    if remaining <= 0 or not done.wait(timeout=remaining):
        return False
    return bool(result.get("success", False))


def shutdown_writer(provider: Any, timeout: float = 3.0) -> None:
    """Publish shutdown atomically with enqueue and stop by a hard deadline."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    with _provider_lock_until(provider, "_capture_submission_lock", deadline):
        with _provider_lock_until(provider, "_writer_lifecycle_lock", deadline):
            shutdown_requested = getattr(provider, "_shutdown_requested", None)
            if shutdown_requested is not None:
                shutdown_requested.set()
            maintenance_stop = getattr(provider, "_maintenance_stop", None)
            if maintenance_stop is not None:
                maintenance_stop.set()
    remaining = _remaining(deadline)
    if remaining <= 0 or not flush_writer(provider, timeout=remaining):
        raise RuntimeError("Scope Recall writer did not acknowledge the shutdown flush")
    provider._stop.set()
    thread = getattr(provider, "_writer_thread", None)
    if thread is not None and thread.is_alive():
        remaining = _remaining(deadline)
        if remaining > 0:
            try:
                put_work(
                    provider._write_queue,
                    None,
                    timeout=min(CONTROL_PUT_TIMEOUT_SECONDS, remaining),
                )
            except RuntimeError:
                pass
        join = getattr(thread, "join", None)
        if callable(join):
            join(timeout=_remaining(deadline))
    if thread is not None and thread.is_alive():
        raise RuntimeError("Scope Recall writer did not stop before resource teardown")
    if not provider._write_queue.empty():
        raise RuntimeError("Scope Recall writer stopped with unconsumed queue control")
    provider._writer_thread = None


def quiesce_writer_for_handoff(provider: Any, timeout: float = 3.0) -> None:
    """Drain and stop a writer while the caller holds its submission fence.

    Unlike process shutdown this does not publish ``shutdown_requested`` and
    therefore remains reversible until resource teardown begins.  The caller
    must keep the capture-submission lock and writer-handoff fence for the
    entire operation.
    """

    deadline = time.monotonic() + max(0.0, float(timeout))
    if not bool(getattr(provider, "_writer_handoff_fenced", False)):
        raise RuntimeError("writer_handoff_fence_missing")
    if not flush_writer(provider, timeout=_remaining(deadline)):
        raise RuntimeError("writer_handoff_flush_failed")
    maintenance_stop = getattr(provider, "_maintenance_stop", None)
    if maintenance_stop is not None:
        maintenance_stop.set()
    stop = getattr(provider, "_stop", None)
    if stop is None:
        raise RuntimeError("writer_handoff_stop_event_missing")
    stop.set()
    thread = getattr(provider, "_writer_thread", None)
    if thread is not None and thread is threading.current_thread():
        raise RuntimeError("writer_handoff_cannot_join_current_thread")
    if thread is not None and thread.is_alive():
        remaining = _remaining(deadline)
        if remaining <= 0 or not put_work(
            getattr(provider, "_write_queue", None),
            None,
            timeout=min(CONTROL_PUT_TIMEOUT_SECONDS, remaining),
        ):
            raise RuntimeError("writer_handoff_stop_publish_failed")
        thread.join(timeout=_remaining(deadline))
    if thread is not None and thread.is_alive():
        raise RuntimeError("writer_handoff_stop_timeout")
    work_queue = getattr(provider, "_write_queue", None)
    if work_queue is None or not work_queue.empty():
        raise RuntimeError("writer_handoff_queue_not_empty")
    setattr(provider, "_writer_thread", None)


def resume_writer_after_handoff(provider: Any) -> None:
    """Resume a quiesced writer after a pre-teardown handoff veto."""

    shutdown = getattr(provider, "_shutdown_requested", None)
    if shutdown is not None and shutdown.is_set():
        raise RuntimeError("writer_handoff_resume_during_shutdown")
    stop = getattr(provider, "_stop", None)
    if stop is None:
        raise RuntimeError("writer_handoff_stop_event_missing")
    prior = getattr(provider, "_writer_thread", None)
    if prior is threading.current_thread():
        raise RuntimeError("writer_handoff_resume_from_writer_thread")
    if prior is not None and prior.is_alive() and not stop.is_set():
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.clear()
        return
    if prior is not None and prior.is_alive():
        # A quiesce timeout can occur after the stop sentinel was published.
        # Do not clear ``_stop`` or claim OWNER while that consumer is still
        # alive: it may consume the sentinel just after start_writer decides
        # that an existing thread is healthy.  Boundedly finish the old
        # consumer first; failure remains fenced/uncertain at the coordinator.
        prior.join(timeout=3.0)
    if prior is not None and prior.is_alive():
        raise RuntimeError("writer_handoff_resume_wait_timeout")
    work_queue = getattr(provider, "_write_queue", None)
    if work_queue is None or not work_queue.empty():
        raise RuntimeError("writer_handoff_resume_queue_not_empty")
    # ``provider`` is intentionally structural here (tests and alternate hosts
    # supply compatible writer runtimes), so keep the reversible handoff state
    # update dynamic in the same way as the reads above.
    setattr(provider, "_writer_thread", None)
    stop.clear()
    maintenance_stop = getattr(provider, "_maintenance_stop", None)
    if maintenance_stop is not None:
        maintenance_stop.clear()
    start_writer(provider)
    resumed = getattr(provider, "_writer_thread", None)
    if resumed is None or not resumed.is_alive():
        raise RuntimeError("writer_handoff_resume_start_failed")


@contextmanager
def capture_mutation_barrier(provider: Any, timeout: float = 2.0) -> Iterator[None]:
    """Drain accepted captures, then exclude new enqueue through a mutation.

    Lock order is submission then lifecycle (inside queued store units).  The
    writer never acquires submission, so processing jobs can finish while a
    forget/delete caller waits on the flush marker.
    """

    if not hasattr(provider, "_capture_submission_lock"):
        yield
        return
    with _capture_submission_lock(provider):
        work_queue = getattr(provider, "_write_queue", None)
        thread = getattr(provider, "_writer_thread", None)
        has_pending = work_queue is not None and not work_queue.empty()
        processing = int(getattr(provider, "_capture_queue_processing", 0) or 0)
        if thread is not None and thread.is_alive():
            if not flush_writer(provider, timeout=timeout):
                raise RuntimeError(
                    "Scope Recall capture queue did not flush before memory mutation"
                )
        elif has_pending or processing:
            raise RuntimeError(
                "Scope Recall capture queue is unavailable before memory mutation"
            )
        yield


def _enqueue_result(
    provider: Any,
    *,
    status: str,
    reason: str,
    depth: int,
    capacity: int,
) -> dict[str, Any]:
    return ensure_outcome_accounted(
        provider,
        {
            "status": status,
            "reason": reason,
            "intent_id": None,
            "depth": max(0, int(depth)),
            "capacity": max(0, int(capacity)),
        },
    )


def enqueue_store(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sanitize and publish one capture to the finite process-local queue."""

    # Enter the process-wide handoff activity gate before waiting for the
    # submission lock.  A capture that begins while the idle fence is being
    # assembled must veto that handoff instead of waking after demotion and
    # being rejected by an authority state that changed underneath it.
    from ._internal.runtime.writer_handoff import active_truth_work

    safe_content = sanitize_capture_text(content)
    safe_metadata_raw, _ = sanitize_structured_value(metadata or {})
    safe_metadata = safe_metadata_raw if isinstance(safe_metadata_raw, dict) else {}
    work_queue = getattr(provider, "_write_queue", None)
    capacity = queue_maxsize(work_queue) or queue_capacity(
        getattr(provider, "_config", None)
    )
    if not should_capture_text(safe_content, provider._config).allowed:
        return _enqueue_result(
            provider,
            status="rejected",
            reason="filtered",
            depth=int(getattr(work_queue, "qsize", lambda: 0)()),
            capacity=capacity,
        )

    with active_truth_work(provider):
        with _capture_submission_lock(provider):
            with _writer_lifecycle_lock(provider):
                shutdown_requested = getattr(provider, "_shutdown_requested", None)
                if (
                    (shutdown_requested is not None and shutdown_requested.is_set())
                    or provider._stop.is_set()
                ):
                    raise RuntimeError("Scope Recall writer is shutting down")
                if not has_positive_write_authority(provider):
                    return _enqueue_result(
                        provider,
                        status="rejected",
                        reason="write_authority",
                        depth=int(getattr(work_queue, "qsize", lambda: 0)()),
                        capacity=capacity,
                    )
                if work_queue is None:
                    return _enqueue_result(
                        provider,
                        status="deferred",
                        reason="writer_unavailable",
                        depth=0,
                        capacity=capacity,
                    )
                thread = getattr(provider, "_writer_thread", None)
                if thread is None or not thread.is_alive():
                    return _enqueue_result(
                        provider,
                        status="deferred",
                        reason="writer_unavailable",
                        depth=int(getattr(work_queue, "qsize", lambda: 0)()),
                        capacity=capacity,
                    )
                authorization = _resolve_capture_authorization(
                    provider,
                    target=target,
                    source=source,
                )
                job = {
                    "kind": "store",
                    "content": safe_content,
                    "source": str(source),
                    "target": str(target),
                    "session_id": str(session_id),
                    "metadata": safe_metadata,
                    "authorization": authorization,
                }
                try:
                    work_queue.put_nowait(job)
                except queue.Full:
                    return _enqueue_result(
                        provider,
                        status="rejected",
                        reason="queue_full",
                        depth=int(work_queue.qsize()),
                        capacity=capacity,
                    )
            from ._internal.runtime.writer_handoff import note_truth_activity

            note_truth_activity(provider)
            return {
                "status": "accepted",
                "reason": "queued",
                "intent_id": None,
                "depth": int(work_queue.qsize()),
                "capacity": capacity,
            }


def enqueue_and_observe(
    provider: Any,
    *,
    caller: str,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enqueue one capture and contractually handle the returned status."""

    result = enqueue_store(
        provider,
        content=content,
        source=source,
        target=target,
        session_id=session_id,
        metadata=metadata,
    )
    return handle_capture_enqueue(provider, result, caller=caller)


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
    replay_vector: bool = True,
    _accepted_capture_token: object | None = None,
    authorization: CaptureAuthorizationEnvelope | None = None,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Synchronously commit one sanitized capture row through SQLite truth."""

    safe_content = sanitize_capture_text(content)
    safe_metadata_raw, _ = sanitize_structured_value(metadata or {})
    safe_metadata = safe_metadata_raw if isinstance(safe_metadata_raw, dict) else {}
    if not should_capture_text(safe_content, provider._config).allowed:
        return "", False, None
    with _capture_store_authority(
        provider, accepted_capture_token=_accepted_capture_token
    ):
        resolved_authorization = authorization or _resolve_capture_authorization(
            provider,
            target=target,
            source=source,
            scope_mode=scope_mode,
        )
        memory_id = uuid.uuid4().hex
        metadata_payload = dict(safe_metadata)
        metadata_payload.setdefault("scope_mode", resolved_authorization.scope_mode)
        metadata_payload.setdefault(
            "runtime_scope_id", resolved_authorization.runtime_scope_id
        )
        metadata_payload.setdefault(
            "shared_scope_id", resolved_authorization.shared_scope_id
        )
        metadata_payload.setdefault("raw_platform", resolved_authorization.platform)
        metadata_payload.setdefault("raw_user_id", resolved_authorization.user_id)
        if resolved_authorization.canonical_user:
            metadata_payload.setdefault(
                "canonical_user", resolved_authorization.canonical_user
            )
            metadata_payload.setdefault("scope_identity_mode", "canonical")
        metadata_json = json.dumps(
            metadata_payload, ensure_ascii=False, sort_keys=True
        )
        companion_result: dict[str, Any] | None = None
        inserted = False
        from .transaction_guard import TruthTransactionTimer

        store_transaction_timer = TruthTransactionTimer(f"capture store ({source})")
        try:
            with MemoryMutationService(provider).transaction() as conn:
                memory_id, _summary, _updated_at, inserted = store_row(
                    conn,
                    memory_id=memory_id,
                    scope_id=resolved_authorization.row_scope_id,
                    platform=resolved_authorization.platform,
                    user_id=resolved_authorization.user_id,
                    chat_id=resolved_authorization.chat_id,
                    thread_id=resolved_authorization.thread_id,
                    gateway_session_key=resolved_authorization.gateway_session_key,
                    agent_identity=resolved_authorization.agent_identity,
                    agent_workspace=resolved_authorization.agent_workspace,
                    session_id=session_id,
                    source=source,
                    target=target,
                    content=safe_content,
                    metadata=metadata_json,
                    allow_duplicate=allow_duplicate
                    or str(source).startswith("legacy-"),
                    commit=False,
                )
                if inserted and before_commit is not None:
                    companion_result = before_commit(conn, memory_id)
        finally:
            store_transaction_timer.stop()
    if replay_vector:
        replay_vector_outbox(provider)
    return memory_id, inserted, companion_result


def capture_turn_llm_candidates(
    provider: Any,
    *,
    clean_user: str,
    clean_assistant: str,
    user_allowed: bool,
    assistant_allowed: bool,
    extract_fn: Callable[..., Any],
) -> tuple[bool, bool]:
    """Run optional turn LLM capture. Returns (extracted, policy_blocked)."""

    from .http_utils import UnsafeEndpointError
    from .write_kernel import prepare_network_boundary, release_snapshot_transaction

    capture_llm_config = provider._config.get("capture_llm")
    if not isinstance(capture_llm_config, dict) or (
        capture_llm_config.get("enabled") not in (True, "true", "1", "yes", "on")
    ):
        return False, False
    min_user = int(capture_llm_config.get("min_user_chars", 20))
    min_asst = int(capture_llm_config.get("min_assistant_chars", 30))
    if not (
        user_allowed
        and len(clean_user) >= min_user
        and assistant_allowed
        and len(clean_assistant) >= min_asst
    ):
        return False, False
    try:
        with provider._lock:
            release_snapshot_transaction(provider._conn)
            prepare_network_boundary(provider._conn, "provider.sync_turn.capture_llm")
        candidates = extract_fn(clean_user, clean_assistant, provider._config)
    except UnsafeEndpointError:
        logger.warning(
            "Scope Recall capture blocked by endpoint policy; "
            "capture fallbacks disabled for this turn"
        )
        return False, True
    except Exception:
        logger.exception("Scope Recall capture LLM extraction failed")
        return False, False
    extracted = False
    for candidate in candidates:
        if len(candidate.content) < 12:
            continue
        outcome = enqueue_and_observe(
            provider,
            caller="capture_turn_llm_candidates",
            content=candidate.content,
            source="turn-llm-extracted",
            target=candidate.target,
            session_id=provider._session_id,
            metadata={
                "category": candidate.memory_type,
                "confidence": candidate.confidence,
                "entities": candidate.entities,
                "tags": candidate.tags,
            },
        )
        if outcome.get("status") == "accepted":
            extracted = True
    return extracted, False


def capture_turn_fallbacks(
    provider: Any,
    *,
    clean_user: str,
    clean_assistant: str,
    user_allowed: bool,
    assistant_allowed: bool,
    llm_extracted: bool,
    capture_policy_blocked: bool,
    min_capture: int,
    extract_candidates_fn: Callable[..., Any],
) -> None:
    """Legacy regex/raw capture fallbacks after LLM capture."""

    extracted = False
    per_turn_cfg = provider._config.get("per_turn_extraction") if isinstance(provider._config.get("per_turn_extraction"), dict) else {}
    per_turn_regex_enabled = config_bool(per_turn_cfg, "enabled", False) if isinstance(per_turn_cfg, dict) else False
    if (
        not llm_extracted
        and not capture_policy_blocked
        and per_turn_regex_enabled
        and user_allowed
    ):
        for candidate in extract_candidates_fn(clean_user):
            candidate_min_capture = min(min_capture, 24) if candidate.target in {"user", "ops", "project"} else min_capture
            if len(candidate.content) < candidate_min_capture:
                continue
            outcome = enqueue_and_observe(
                provider,
                caller="capture_turn_fallbacks.regex",
                content=candidate.content,
                source="turn-extracted",
                target=candidate.target,
                session_id=provider._session_id,
                metadata={"category": candidate.category, "confidence": candidate.confidence},
            )
            if outcome.get("status") == "accepted":
                extracted = True
    if (
        not llm_extracted
        and not capture_policy_blocked
        and config_bool(provider._config, "capture_raw_user", False)
        and user_allowed
        and len(clean_user) >= min_capture
        and not extracted
    ):
        enqueue_and_observe(
            provider,
            caller="capture_turn_fallbacks.raw_user",
            content=clean_user,
            source="turn-user",
            target="general",
            session_id=provider._session_id,
        )
    if (
        not llm_extracted
        and not capture_policy_blocked
        and config_bool(provider._config, "capture_assistant", False)
        and assistant_allowed
        and len(clean_assistant) >= min_capture
    ):
        enqueue_and_observe(
            provider,
            caller="capture_turn_fallbacks.assistant",
            content=clean_assistant,
            source="turn-assistant",
            target="general",
            session_id=provider._session_id,
        )
