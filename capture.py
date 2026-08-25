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
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeGuard

from .capture_control import (
    CONTROL_PUT_TIMEOUT_SECONDS,
    bind_write_control_queue,
    put_control,
    wake_writer,
)
from .capture_filters import should_capture_text
from .capture_intents import (
    capture_intent_report,
    claim_next_capture_intent,
    complete_capture_intent,
    merge_capture_queue_report,
    persist_from_provider,
    queue_capacity,
    release_stale_processing,
    requeue_capture_intent,
    unconsumed_depth,
)
from .capture_outcomes import ensure_outcome_accounted, handle_capture_enqueue
from .memory_mutation import MemoryMutationService
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
from . import write_kernel as write_kernel_mod
from .write_kernel import (
    WRITE_AUTHORITY_BUSY,
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


@contextmanager
def _capture_store_authority(
    provider: Any, *, complete_accepted_intent: bool = False
) -> Iterator[None]:
    """Hold lifecycle for one truth store.

    Completing an already-persisted intent may finish during shutdown flush.
    New synchronous stores still fail closed once shutdown is visible.
    """

    with _writer_lifecycle_lock(provider):
        if complete_accepted_intent:
            if getattr(provider, "_truth_writer_role", None) != "owner":
                raise RuntimeError(WRITE_AUTHORITY_BUSY)
        else:
            write_kernel_mod.require_positive_write_authority(provider)
        yield


def _is_sqlite_conn(conn: object) -> TypeGuard[sqlite3.Connection]:
    """Skip stub connections used by shutdown tests."""

    return hasattr(conn, "in_transaction") and hasattr(conn, "execute")


def _wake_writer(provider: Any) -> bool:
    """Hint the writer that durable intents are waiting. Never blocks."""

    return wake_writer(provider)


def _release_stale_capture_intents(provider: Any) -> None:
    """Requeue processing rows left behind by a previous writer process."""

    require = getattr(provider, "_require_conn", None)
    lock = getattr(provider, "_lock", None)
    if not callable(require) or lock is None:
        return
    with lock:
        conn = require()
        if not _is_sqlite_conn(conn) or conn.in_transaction:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            release_stale_processing(conn)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _open_intent_transaction(provider: Any) -> sqlite3.Connection | None:
    require = getattr(provider, "_require_conn", None)
    if not callable(require):
        return None
    conn = require()
    if not _is_sqlite_conn(conn) or conn.in_transaction:
        return None
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _consume_durable_intents(
    provider: Any, *, limit: int = 8, allow_after_shutdown: bool = False
) -> int:
    """Claim and store a bounded page of persisted intents without vector replay.

    Each unit commits the truth row, then marks the intent completed in a
    second short transaction. Vector replay stays on the idle/sync path so a
    blocked embedder cannot occupy the capture-intent persist lock.
    """

    require = getattr(provider, "_require_conn", None)
    lock = getattr(provider, "_lock", None)
    if not callable(require) or lock is None:
        return 0
    # A close retry can intentionally detach the published connection before
    # shutdown asks the writer to flush. Do not reopen truth after shutdown has
    # begun merely to discover that there is no remaining connection to drain.
    if allow_after_shutdown and getattr(provider, "_conn", None) is None:
        return 0
    probe = require()
    if not _is_sqlite_conn(probe):
        return 0
    consumed = 0
    for _ in range(max(1, int(limit))):
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if not allow_after_shutdown and (
            provider._stop.is_set()
            or (shutdown_requested is not None and shutdown_requested.is_set())
        ):
            break
        with lock:
            conn = _open_intent_transaction(provider)
            if conn is None:
                break
            try:
                intent = claim_next_capture_intent(conn)
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
        if intent is None:
            break
        try:
            store_now(
                provider,
                content=intent["content"],
                source=intent["source"],
                target=intent["target"],
                session_id=intent["session_id"] or provider._session_id,
                metadata=intent.get("metadata") or {},
                replay_vector=False,
                complete_accepted_intent=True,
            )
            with lock:
                conn = _open_intent_transaction(provider)
                if conn is None:
                    raw = require()
                    if _is_sqlite_conn(raw) and raw.in_transaction:
                        raw.rollback()
                    conn = _open_intent_transaction(provider)
                if conn is None:
                    continue
                try:
                    complete_capture_intent(conn, int(intent["id"]))
                    conn.commit()
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
        except Exception:
            with lock:
                try:
                    conn = require()
                    if not _is_sqlite_conn(conn):
                        raise RuntimeError("capture intent SQLite connection is unavailable")
                    if conn.in_transaction:
                        conn.rollback()
                    conn.execute("BEGIN IMMEDIATE")
                    requeue_capture_intent(conn, int(intent["id"]))
                    conn.commit()
                except Exception:
                    logger.exception("Scope Recall could not requeue a failed capture intent")
            raise
        consumed += 1
    return consumed


def capture_queue_report(provider: Any) -> dict[str, Any]:
    """Expose depth, oldest age, and reject/defer counters without path leakage."""

    config = getattr(provider, "_config", None)
    capacity = queue_capacity(config if isinstance(config, dict) else None)
    durable: dict[str, Any] = {
        "status": "unavailable",
        "capacity": capacity,
        "depth": 0,
        "pending": 0,
        "processing": 0,
        "oldest_age_seconds": 0.0,
        "rejected": 0,
        "deferred": 0,
    }
    require = getattr(provider, "_require_conn", None)
    lock = getattr(provider, "_lock", None)
    if callable(require) and lock is not None:
        acquired = False
        try:
            acquired = bool(lock.acquire(timeout=0.05))
        except TypeError:
            lock.acquire()
            acquired = True
        if acquired:
            try:
                conn = require()
                if _is_sqlite_conn(conn):
                    durable = capture_intent_report(conn, capacity=capacity)
            except Exception:
                durable = dict(durable)
            finally:
                lock.release()
    return merge_capture_queue_report(durable, provider, capacity=capacity)


def _drain_relation_rebuild_debt(provider: Any) -> None:
    """Use the existing writer thread to process one bounded relation chunk.

    Each idle unit rechecks shutdown/role under the lifecycle lock before any
    durable work. Returning here is fail-closed: a blocked writer must not
    start or continue a bounded mutation after the flag is visible.
    """

    with _writer_lifecycle_lock(provider):
        _drain_relation_rebuild_debt_locked(provider)


def _drain_relation_rebuild_debt_locked(provider: Any) -> None:
    """Run one idle maintenance tick while the caller holds lifecycle."""

    if not has_positive_write_authority(provider):
        return
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
        bind_write_control_queue(provider)
        if provider._writer_thread and provider._writer_thread.is_alive():
            return
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if shutdown_requested is not None:
            shutdown_requested.clear()
        provider._stop.clear()
        maintenance_stop = getattr(provider, "_maintenance_stop", None)
        if maintenance_stop is not None:
            maintenance_stop.clear()
        try:
            _release_stale_capture_intents(provider)
        except Exception:
            logger.exception("Scope Recall could not requeue stale capture intents")
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
            try:
                _consume_durable_intents(provider, limit=1)
            except Exception:
                rollback = getattr(provider, "_rollback_conn_after_error", None)
                if callable(rollback):
                    rollback("durable capture intent drain")
                logger.exception("Scope Recall durable capture intent drain failed")
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
            if not isinstance(job, dict):
                continue
            if job.get("kind") == "flush":
                event = job.get("event")
                result = job.get("result")
                try:
                    _consume_durable_intents(
                        provider,
                        limit=queue_capacity(getattr(provider, "_config", None)),
                        allow_after_shutdown=True,
                    )
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
                finally:
                    if isinstance(event, threading.Event):
                        event.set()
                continue
            if job.get("kind") == "drain":
                _consume_durable_intents(provider, limit=8)
                continue
            if job.get("kind") == "store":
                store_now(
                    provider,
                    content=job["content"],
                    source=job["source"],
                    target=job["target"],
                    session_id=job.get("session_id") or provider._session_id,
                    metadata=job.get("metadata") or {},
                    replay_vector=False,
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


def _durable_queue_idle(provider: Any) -> bool:
    require = getattr(provider, "_require_conn", None)
    lock = getattr(provider, "_lock", None)
    if not callable(require) or lock is None:
        return True
    with lock:
        try:
            conn = require()
            if not _is_sqlite_conn(conn):
                return True
            return unconsumed_depth(conn) == 0
        except Exception:
            return True


def flush_writer(provider: Any, timeout: float = 2.0) -> bool:
    thread = provider._writer_thread
    if thread is None:
        return provider._write_queue.empty() and _durable_queue_idle(provider)
    if not thread.is_alive():
        return False
    done = threading.Event()
    result: dict[str, bool] = {}
    put_timeout = min(CONTROL_PUT_TIMEOUT_SECONDS, max(0.01, float(timeout)))
    try:
        if not put_control(
            provider._write_queue,
            {"kind": "flush", "event": done, "result": result},
            timeout=put_timeout,
        ):
            return False
    except RuntimeError:
        return False
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
        try:
            put_control(
                provider._write_queue,
                None,
                timeout=min(CONTROL_PUT_TIMEOUT_SECONDS, max(0.01, float(timeout))),
            )
        except RuntimeError:
            pass
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
) -> dict[str, Any]:
    """Persist a capture intent, then wake the writer.

    Returns an explicit accepted/coalesced/rejected/deferred status. Filtered
    text is rejected without occupying queue capacity. Shutdown still raises so
    existing fail-closed callers keep their contract.
    """

    if not should_capture_text(content, provider._config).allowed:
        return ensure_outcome_accounted(
            provider,
            {
                "status": "rejected",
                "reason": "filtered",
                "intent_id": None,
                "depth": 0,
                "capacity": queue_capacity(getattr(provider, "_config", None)),
                "durable_accounted": False,
            },
        )
    with _writer_lifecycle_lock(provider):
        shutdown_requested = getattr(provider, "_shutdown_requested", None)
        if (
            (shutdown_requested is not None and shutdown_requested.is_set())
            or provider._stop.is_set()
        ):
            raise RuntimeError("Scope Recall writer is shutting down")
        result = persist_from_provider(
            provider,
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata or {},
        )
    if result.pop("_fallback", False):
        work_queue = bind_write_control_queue(provider)
        hinted = False
        try:
            hinted = put_control(
                work_queue,
                {
                    "kind": "store",
                    "content": content,
                    "source": source,
                    "target": target,
                    "session_id": session_id,
                    "metadata": metadata or {},
                },
            )
        except RuntimeError:
            hinted = False
        if not hinted:
            result = {
                "status": "rejected",
                "reason": "control_queue_full",
                "intent_id": None,
                "depth": int(getattr(work_queue, "qsize", lambda: 0)()),
                "capacity": queue_capacity(getattr(provider, "_config", None)),
                "durable_accounted": False,
            }
    if result.get("status") in {"accepted", "coalesced"}:
        _wake_writer(provider)
    return ensure_outcome_accounted(provider, result)


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
    complete_accepted_intent: bool = False,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Synchronously store one capture row through the provider database.

    Queue workers call this after dequeue, so the lifecycle gate is acquired
    here independently. The same RLock spans the authority check through
    commit so a concurrent shutdown setter cannot publish the flag mid-unit.
    """
    if not should_capture_text(content, provider._config).allowed:
        return "", False, None
    with _capture_store_authority(
        provider, complete_accepted_intent=complete_accepted_intent
    ):
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
        inserted = False
        from .transaction_guard import TruthTransactionTimer

        store_transaction_timer = TruthTransactionTimer(f"capture store ({source})")
        try:
            with MemoryMutationService(provider).transaction() as conn:
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
            "durable capture fallbacks disabled for this turn"
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
        if outcome.get("status") in {"accepted", "coalesced"}:
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

    from .gating import config_bool

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
            if outcome.get("status") in {"accepted", "coalesced"}:
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
