"""Process-wide, fail-closed idle writer handoff.

The OS writer lease remains the authority boundary.  This module only
coordinates a voluntary release after every same-database Provider in this
process is idle, fenced, drained, and represented by the exact named-holder
and connection-pin counts.  It never transfers authority directly: a reader
must still win the ordinary non-blocking OS lease acquisition later.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from ...embedders import close_embedder
from ...writer_lease import (
    WRITER_HANDOFF_TELEMETRY_FRESH_SECONDS,
    process_writer_handoff_state,
    publish_writer_handoff_telemetry,
    truth_writer_process_snapshot,
)
from .peer_recovery import live_providers_for_database

logger = logging.getLogger(__name__)

DEFAULT_IDLE_RELEASE_SECONDS = 1800.0
IDLE_PROBE_INTERVAL_SECONDS = 5.0
HANDOFF_LOCK_TIMEOUT_SECONDS = 3.0
HANDOFF_COOLDOWN_SECONDS = 30.0

_CONTENT_FREE_FAILURE_CODES = frozenset(
    {
        "activity_generation_changed",
        "background_lock_busy",
        "background_lock_missing",
        "busy_capture_submission_lock",
        "busy_writer_handoff_activity_lock",
        "busy_writer_lifecycle_lock",
        "capture_processing",
        "capture_queue_pending",
        "connection_close_failed",
        "connection_missing",
        "connection_missing_during_handoff",
        "connection_pin_count_mismatch",
        "connection_pins_remain_after_close",
        "connection_remained_published",
        "digest_active",
        "disabled",
        "foreground_busy",
        "holder_count_mismatch",
        "missing_capture_submission_lock",
        "missing_writer_handoff_activity_lock",
        "missing_writer_lifecycle_lock",
        "no_owner_participants",
        "not_owner",
        "provider_lease_mismatch",
        "provider_lease_missing_during_handoff",
        "reader_query_only_disabled",
        "reader_query_only_unknown",
        "reader_reopen_failed",
        "reader_runtime_status_mismatch",
        "recent_truth_activity",
        "recent_user_activity",
        "shutdown",
        "storage_identity_missing",
        "storage_identity_missing_during_handoff",
        "storage_identity_missing_during_release",
        "transaction_open",
        "transaction_open_during_handoff",
        "transaction_unknown",
        "trigger_not_owner",
        "truth_work_active",
        "writer_authority_remained_after_handoff",
        "writer_unavailable",
    }
)
_PHASE_FAILURE_CODES = {
    "preflight": "handoff_preflight_failed",
    "quiesce": "quiesce_failed",
    "final_recheck": "handoff_final_recheck_failed",
    "writer_resource_close": "writer_resource_close_failed",
    "lease_release": "os_lease_release_failed",
    "reader_initialization": "reader_initialization_failed",
}


def _content_free_failure_code(exc: BaseException, *, phase: str) -> str:
    """Map arbitrary exceptions to bounded codes that cannot disclose data."""

    candidate = str(exc or "").strip()
    if candidate in _CONTENT_FREE_FAILURE_CODES:
        return candidate
    return _PHASE_FAILURE_CODES.get(phase, "idle_handoff_failed")


def _activity_lock(provider: Any) -> threading.RLock:
    lock = getattr(provider, "_writer_handoff_activity_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(provider, "_writer_handoff_activity_lock", lock)
    return lock


def initialize_writer_handoff_activity(provider: Any, *, reset: bool = False) -> None:
    """Initialize content-free activity clocks for one Provider."""

    now = time.monotonic()
    with _activity_lock(provider):
        if reset or not hasattr(provider, "_writer_handoff_last_user_activity"):
            setattr(provider, "_writer_handoff_last_user_activity", now)
        if reset or not hasattr(provider, "_writer_handoff_last_truth_activity"):
            setattr(provider, "_writer_handoff_last_truth_activity", now)
        if reset or not hasattr(provider, "_writer_handoff_activity_generation"):
            setattr(provider, "_writer_handoff_activity_generation", 0)
        if reset or not hasattr(provider, "_writer_handoff_active_truth_work"):
            setattr(provider, "_writer_handoff_active_truth_work", 0)
        if reset or not hasattr(provider, "_writer_handoff_last_probe"):
            setattr(provider, "_writer_handoff_last_probe", 0.0)
        if reset or not hasattr(provider, "_writer_handoff_fenced"):
            setattr(provider, "_writer_handoff_fenced", False)
        if not hasattr(provider, "_writer_handoff_thread_work"):
            setattr(provider, "_writer_handoff_thread_work", threading.local())


def note_user_activity(provider: Any) -> None:
    """Record a user-turn boundary without retaining message content."""

    initialize_writer_handoff_activity(provider)
    with _activity_lock(provider):
        setattr(provider, "_writer_handoff_last_user_activity", time.monotonic())
        setattr(
            provider,
            "_writer_handoff_activity_generation",
            int(getattr(provider, "_writer_handoff_activity_generation", 0) or 0) + 1,
        )
    _publish_runtime_telemetry(provider, event_kind="user_activity")


def note_truth_activity(provider: Any) -> None:
    """Record an accepted or completed durable-truth mutation."""

    initialize_writer_handoff_activity(provider)
    with _activity_lock(provider):
        setattr(provider, "_writer_handoff_last_truth_activity", time.monotonic())
        setattr(
            provider,
            "_writer_handoff_activity_generation",
            int(getattr(provider, "_writer_handoff_activity_generation", 0) or 0) + 1,
        )
    _publish_runtime_telemetry(provider, event_kind="truth_activity")


def _connection_total_changes(provider: Any) -> int | None:
    conn = getattr(provider, "_conn", None)
    if conn is None:
        return None
    try:
        return int(conn.total_changes)
    except Exception:
        return None


def _thread_work_state(provider: Any) -> threading.local:
    with _activity_lock(provider):
        local = getattr(provider, "_writer_handoff_thread_work", None)
        if local is None:
            local = threading.local()
            setattr(provider, "_writer_handoff_thread_work", local)
        return local


def _may_start_pre_fence_truth_work(provider: Any) -> bool:
    if (
        getattr(provider, "_truth_writer_role", None) != "owner"
        or bool(getattr(provider, "_writer_handoff_fenced", False))
    ):
        return False
    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        return True
    state = process_writer_handoff_state(storage_dir)
    with state.lock:
        return not bool(
            getattr(state, "handoff_fenced", False)
            or getattr(state, "release_uncertain", False)
        )


def current_truth_work_started_before_fence(provider: Any) -> bool:
    """Return whether this thread owns an already-authorized pre-fence unit."""

    local = getattr(provider, "_writer_handoff_thread_work", None)
    return bool(
        local is not None
        and int(getattr(local, "depth", 0) or 0) > 0
        and bool(getattr(local, "pre_fence_authorized", False))
    )


@contextmanager
def active_truth_work(provider: Any, *, user_initiated: bool = False) -> Iterator[None]:
    """Fence one potential truth unit and record activity only when appropriate.

    The active counter is the handoff veto.  Background no-op probes do not
    perpetually renew the idle clock; an actual SQLite change does.  Explicit
    user work renews the user clock even when the operation is a safe no-op.
    """

    initialize_writer_handoff_activity(provider)
    if user_initiated:
        note_user_activity(provider)
    before = _connection_total_changes(provider)
    thread_work = _thread_work_state(provider)
    thread_depth = int(getattr(thread_work, "depth", 0) or 0)
    if thread_depth == 0:
        thread_work.pre_fence_authorized = _may_start_pre_fence_truth_work(provider)
    thread_work.depth = thread_depth + 1
    with _activity_lock(provider):
        setattr(
            provider,
            "_writer_handoff_active_truth_work",
            int(getattr(provider, "_writer_handoff_active_truth_work", 0) or 0) + 1,
        )
    try:
        yield
    finally:
        after = _connection_total_changes(provider)
        remaining_depth = max(0, int(getattr(thread_work, "depth", 1) or 1) - 1)
        thread_work.depth = remaining_depth
        if remaining_depth == 0:
            thread_work.pre_fence_authorized = False
        with _activity_lock(provider):
            setattr(
                provider,
                "_writer_handoff_active_truth_work",
                max(
                    0,
                    int(getattr(provider, "_writer_handoff_active_truth_work", 0) or 0)
                    - 1,
                ),
            )
        if before is not None and after is not None and after != before:
            note_truth_activity(provider)


def idle_release_seconds(provider: Any) -> float:
    raw = getattr(provider, "_config", {}).get("writer_lease")
    config = raw if isinstance(raw, dict) else {}
    try:
        value = float(config.get("idle_release_seconds", DEFAULT_IDLE_RELEASE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_RELEASE_SECONDS
    if value == 0:
        return 0.0
    if 30.0 <= value <= 86_400.0:
        return value
    # Runtime config validation should already reject this.  Fail closed to
    # the documented default if an in-memory test double bypasses the loader.
    return DEFAULT_IDLE_RELEASE_SECONDS


def _activity_snapshot(provider: Any, *, now: float | None = None) -> dict[str, Any]:
    initialize_writer_handoff_activity(provider)
    observed = time.monotonic() if now is None else float(now)
    with _activity_lock(provider):
        user_at = float(getattr(provider, "_writer_handoff_last_user_activity", observed))
        truth_at = float(getattr(provider, "_writer_handoff_last_truth_activity", observed))
        return {
            "generation": int(
                getattr(provider, "_writer_handoff_activity_generation", 0) or 0
            ),
            "active_truth_work": int(
                getattr(provider, "_writer_handoff_active_truth_work", 0) or 0
            ),
            "user_idle_seconds": max(0.0, observed - user_at),
            "truth_idle_seconds": max(0.0, observed - truth_at),
        }


def _thread_alive(value: Any) -> bool:
    return bool(value is not None and getattr(value, "is_alive", lambda: False)())


def _idle_veto(provider: Any, *, now: float, writer_may_be_stopped: bool) -> str:
    threshold = idle_release_seconds(provider)
    if threshold <= 0:
        return "disabled"
    if getattr(provider, "_truth_writer_role", None) != "owner":
        return "not_owner"
    shutdown = getattr(provider, "_shutdown_requested", None)
    if shutdown is not None and shutdown.is_set():
        return "shutdown"
    activity = _activity_snapshot(provider, now=now)
    if activity["user_idle_seconds"] < threshold:
        return "recent_user_activity"
    if activity["truth_idle_seconds"] < threshold:
        return "recent_truth_activity"
    if activity["active_truth_work"]:
        return "truth_work_active"
    if int(getattr(provider, "_foreground_busy_count", 0) or 0):
        return "foreground_busy"
    work_queue = getattr(provider, "_write_queue", None)
    if work_queue is not None and not work_queue.empty():
        return "capture_queue_pending"
    if int(getattr(provider, "_capture_queue_processing", 0) or 0):
        return "capture_processing"
    if _thread_alive(getattr(provider, "_journal_digest_thread", None)):
        return "digest_active"
    writer_alive = _thread_alive(getattr(provider, "_writer_thread", None))
    if not writer_may_be_stopped and not writer_alive:
        return "writer_unavailable"
    conn = getattr(provider, "_conn", None)
    if conn is None:
        return "connection_missing"
    try:
        if bool(conn.in_transaction):
            return "transaction_open"
    except Exception:
        return "transaction_unknown"
    return ""


def writer_handoff_status(provider: Any) -> dict[str, Any]:
    """Return zero-write, content-free lifecycle telemetry."""

    storage_dir = getattr(provider, "_storage_dir", None)
    threshold = idle_release_seconds(provider)
    activity = _activity_snapshot(provider)
    payload: dict[str, Any] = {
        "enabled": threshold > 0,
        "idle_release_enabled": threshold > 0,
        "idle_release_seconds": threshold,
        "user_idle_seconds": round(float(activity["user_idle_seconds"]), 3),
        "truth_idle_seconds": round(float(activity["truth_idle_seconds"]), 3),
        "last_user_activity_age_seconds": round(
            float(activity["user_idle_seconds"]), 3
        ),
        "last_truth_activity_age_seconds": round(
            float(activity["truth_idle_seconds"]), 3
        ),
        "active_truth_work": int(activity["active_truth_work"]),
        "provider_fenced": bool(getattr(provider, "_writer_handoff_fenced", False)),
        "writer_role": str(
            getattr(provider, "_truth_writer_role", "unknown") or "unknown"
        ),
        "writer_lease_scope": "process-wide-os-lock",
    }
    if storage_dir is None:
        return payload
    state = process_writer_handoff_state(storage_dir)
    counts = truth_writer_process_snapshot(storage_dir)
    with state.lock:
        payload.update(
            {
                "process_state": str(getattr(state, "state", "UNKNOWN") or "UNKNOWN"),
                "handoff_generation": int(getattr(state, "handoff_generation", 0) or 0),
                "demotion_in_progress": bool(
                    getattr(state, "handoff_in_progress", False)
                ),
                "successful_handoff_count": int(
                    getattr(state, "successful_handoff_count", 0) or 0
                ),
                "last_handoff_at": str(getattr(state, "last_handoff_at", "") or ""),
                "last_reason_code": str(
                    getattr(state, "last_handoff_reason_code", "") or ""
                ),
                "last_handoff_reason_code": str(
                    getattr(state, "last_handoff_reason_code", "") or ""
                ),
                "last_failure_code": str(
                    getattr(state, "last_handoff_failure_code", "") or ""
                ),
                "last_handoff_failure_code": str(
                    getattr(state, "last_handoff_failure_code", "") or ""
                ),
                "release_uncertain": bool(getattr(state, "release_uncertain", False)),
                "operator_action_required": bool(
                    getattr(state, "operator_action_required", False)
                ),
            }
        )
    payload.update(counts)
    return payload


def _telemetry_fresh_for_seconds(provider: Any) -> float:
    """Use a short fixed freshness window without introducing heartbeat writes."""

    del provider
    return WRITER_HANDOFF_TELEMETRY_FRESH_SECONDS


def _publish_runtime_telemetry(
    provider: Any,
    *,
    event_kind: str,
    activate_owner: bool = False,
    deactivate_owner: bool = False,
    writer_role_override: str | None = None,
) -> bool:
    """Best-effort persisted observability for a real activity/state event.

    The real OS writer lease remains the sole authority. A random, content-free
    epoch is created only after this process owns that lease. All later reader
    updates use ordinary epoch/sequence CAS, so an old owner cannot overwrite a
    newer process after authority changes hands. Any telemetry failure is
    swallowed and cannot affect writer safety or the caller's operation.
    """

    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        return False
    try:
        state = process_writer_handoff_state(storage_dir)
        # Snapshot advisory counters before the process coordinator lock.  Idle
        # handoff takes activity/provider locks before ``state.lock``; taking
        # any of those locks from inside the claim critical section would
        # create an ABBA deadlock.  Only lease ownership, epoch, sequence and
        # the telemetry-file CAS need to be linearized with final release.
        claim_status = writer_handoff_status(provider)

        def publish_snapshot(
            *,
            status: Mapping[str, Any],
            epoch: str,
            sequence: int,
            claim_authority_epoch: bool,
        ) -> bool:
            writer_role = str(
                writer_role_override
                if writer_role_override is not None
                else status.get("writer_role", "unknown")
            )
            payload = {
                "schema_version": "scope-recall.writer-handoff-telemetry.v1",
                "authority_epoch": epoch,
                "event_sequence": sequence,
                "event_kind": str(event_kind or "state_changed")[:64],
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "fresh_for_seconds": _telemetry_fresh_for_seconds(provider),
                "writer_role": writer_role,
                "writer_lease_scope": "process-wide-os-lock",
                "idle_release_enabled": bool(
                    status.get("idle_release_enabled", False)
                ),
                "idle_release_seconds": float(
                    status.get("idle_release_seconds", 0.0)
                ),
                "last_user_activity_age_seconds": float(
                    status.get("last_user_activity_age_seconds", 0.0)
                ),
                "last_truth_activity_age_seconds": float(
                    status.get("last_truth_activity_age_seconds", 0.0)
                ),
                "same_process_holder_count": int(
                    status.get("same_process_holder_count", 0) or 0
                ),
                "connection_pin_count": int(
                    status.get("connection_pin_count", 0) or 0
                ),
                "demotion_in_progress": bool(
                    status.get("demotion_in_progress", False)
                ),
                "successful_handoff_count": int(
                    status.get("successful_handoff_count", 0) or 0
                ),
                "last_handoff_at": str(status.get("last_handoff_at", "") or ""),
                "last_handoff_reason_code": str(
                    status.get("last_handoff_reason_code", "") or ""
                )[:64],
                "last_handoff_failure_code": str(
                    status.get("last_handoff_failure_code", "") or ""
                )[:64],
                "release_uncertain": bool(status.get("release_uncertain", False)),
                "operator_action_required": bool(
                    status.get("operator_action_required", False)
                ),
            }
            return bool(
                publish_writer_handoff_telemetry(
                    storage_dir,
                    payload,
                    claim_authority_epoch=claim_authority_epoch,
                )
            )

        with state.lock:
            lease = getattr(provider, "_truth_writer_lease", None)
            owns_lease = bool(
                getattr(provider, "_truth_writer_role", None) == "owner"
                and lease is not None
                and bool(getattr(lease, "acquired", False))
            )
            if activate_owner:
                if bool(getattr(state, "release_uncertain", False)) or not owns_lease:
                    return False
                if not bool(getattr(state, "telemetry_owner_active", False)):
                    state.telemetry_authority_epoch = secrets.token_hex(16)
                    state.telemetry_event_sequence = 0
                    state.telemetry_owner_active = True
                    state.telemetry_epoch_published = False
            epoch = str(getattr(state, "telemetry_authority_epoch", "") or "")
            if not epoch:
                return False
            claim_epoch = bool(
                getattr(state, "telemetry_owner_active", False)
                and not bool(getattr(state, "telemetry_epoch_published", False))
                and owns_lease
            )
            if deactivate_owner:
                # Compute claim eligibility first, but a post-release caller is
                # never eligible because ``owns_lease`` is false. Deactivate
                # before I/O so concurrent old-reader events cannot claim.
                state.telemetry_owner_active = False
            state.telemetry_event_sequence = int(
                getattr(state, "telemetry_event_sequence", 0) or 0
            ) + 1
            sequence = int(state.telemetry_event_sequence)
            if claim_epoch:
                # Linearize the first epoch claim with every same-process lease
                # release.  A different process cannot become the OS owner
                # while this RLock keeps the old process from releasing its
                # final lease, so a delayed old claim cannot overwrite a newer
                # owner's epoch.
                published = publish_snapshot(
                    status=claim_status,
                    epoch=epoch,
                    sequence=sequence,
                    claim_authority_epoch=True,
                )
                if published:
                    state.telemetry_epoch_published = True
                return published

        # Ordinary events take their observable snapshot only after sequence
        # assignment.  If a shutdown/handoff transition linearizes first, this
        # event sees the new role/counts; if it linearizes later, its higher
        # sequence ultimately overwrites this event.  An older owner snapshot
        # can therefore never publish with a post-shutdown sequence.
        current_status = writer_handoff_status(provider)
        published = publish_snapshot(
            status=current_status,
            epoch=epoch,
            sequence=sequence,
            claim_authority_epoch=False,
        )
        if published:
            with state.lock:
                if str(getattr(state, "telemetry_authority_epoch", "") or "") == epoch:
                    state.telemetry_epoch_published = True
        return bool(published)
    except Exception:
        logger.warning("Scope Recall writer handoff telemetry event was unavailable")
        return False


def _set_process_failure(state: Any, code: str, *, uncertain: bool) -> None:
    with state.lock:
        state.last_handoff_failure_code = str(code or "handoff_failed")[:64]
        state.last_handoff_reason_code = ""
        state.release_uncertain = bool(uncertain)
        state.operator_action_required = bool(uncertain)
        state.state = "RELEASE_UNCERTAIN" if uncertain else "OWNER"
        if not uncertain:
            state.handoff_fenced = False


def _set_process_recovery_required(state: Any, code: str) -> None:
    """Keep authority fenced when writer resources cannot be restored safely."""

    with state.lock:
        state.last_handoff_failure_code = str(code or "writer_restore_failed")[:64]
        state.last_handoff_reason_code = ""
        state.release_uncertain = False
        state.operator_action_required = True
        state.state = "OWNER_DEGRADED"
        state.handoff_fenced = True


def note_writer_promotion_succeeded(provider: Any) -> None:
    """Clear recoverable reader degradation only after writer init succeeds."""

    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        return
    state = process_writer_handoff_state(storage_dir)
    with state.lock:
        if bool(getattr(state, "release_uncertain", False)):
            return
        state.state = "OWNER"
        state.handoff_fenced = False
        state.operator_action_required = False
        state.last_handoff_failure_code = ""
    _publish_runtime_telemetry(
        provider,
        event_kind="owner_activated",
        activate_owner=True,
    )


def note_writer_shutdown_succeeded(provider: Any) -> None:
    """Persist changed local counts, deactivating only after final release."""

    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        return
    counts = truth_writer_process_snapshot(storage_dir)
    holders = int(counts.get("same_process_holder_count", 0) or 0)
    pins = int(counts.get("connection_pin_count", 0) or 0)
    if holders != 0 or pins != 0:
        _publish_runtime_telemetry(
            provider,
            event_kind="provider_shutdown",
            writer_role_override="owner" if holders > 0 else "unknown",
        )
        return
    _publish_runtime_telemetry(
        provider,
        event_kind="writer_shutdown",
        deactivate_owner=True,
        writer_role_override="unknown",
    )


def _acquire_provider_locks(
    providers: tuple[Any, ...], attribute: str
) -> list[Any]:
    acquired: list[Any] = []
    deadline = time.monotonic() + HANDOFF_LOCK_TIMEOUT_SECONDS
    try:
        for peer in providers:
            lock = getattr(peer, attribute, None)
            if lock is None:
                raise RuntimeError(f"missing_{attribute.strip('_')}")
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0 or not lock.acquire(timeout=remaining):
                raise RuntimeError(f"busy_{attribute.strip('_')}")
            acquired.append(lock)
    except BaseException:
        for lock in reversed(acquired):
            lock.release()
        raise
    return acquired


def _release_provider_locks(acquired: list[Any]) -> None:
    while acquired:
        acquired.pop().release()


def _acquire_background_locks(providers: tuple[Any, ...]) -> list[Any]:
    """Fence digest starts before taking lifecycle locks.

    Synchronous digest mode holds this lock while it may enter a lifecycle
    write unit, so acquiring background first avoids lifecycle->background
    inversion while still closing the final start race.
    """

    acquired: list[Any] = []
    deadline = time.monotonic() + HANDOFF_LOCK_TIMEOUT_SECONDS
    try:
        for peer in providers:
            background = getattr(peer, "_background", None)
            lock = getattr(background, "lock", None)
            if lock is None:
                raise RuntimeError("background_lock_missing")
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0 or not lock.acquire(timeout=remaining):
                raise RuntimeError("background_lock_busy")
            acquired.append(lock)
    except BaseException:
        _release_provider_locks(acquired)
        raise
    return acquired


def _exact_authority_veto(providers: tuple[Any, ...]) -> str:
    if not providers:
        return "no_owner_participants"
    storage_dir = getattr(providers[0], "_storage_dir", None)
    if storage_dir is None:
        return "storage_identity_missing"
    counts = truth_writer_process_snapshot(storage_dir)
    connection_count = sum(
        1 for peer in providers if getattr(peer, "_conn", None) is not None
    )
    if counts["same_process_holder_count"] != len(providers):
        return "holder_count_mismatch"
    if counts["connection_pin_count"] != connection_count:
        return "connection_pin_count_mismatch"
    for peer in providers:
        lease = getattr(peer, "_truth_writer_lease", None)
        if lease is None or not bool(getattr(lease, "acquired", False)):
            return "provider_lease_mismatch"
    return ""


def _abort_quiesced_handoff(providers: tuple[Any, ...]) -> None:
    from ...capture import resume_writer_after_handoff

    for peer in providers:
        peer._writer_handoff_fenced = False
    for peer in providers:
        if getattr(peer, "_truth_writer_role", None) == "owner":
            resume_writer_after_handoff(peer)


def _close_writer_resources(providers: tuple[Any, ...]) -> None:
    """Close every writer-only resource before dropping named authority."""

    # Close truth pagers first.  If one close fails, vector/embedder resources
    # are still untouched and any peers whose pagers already closed can be
    # rebuilt while this process continues to hold the exact OS lease.
    for peer in providers:
        lock = getattr(peer, "_lock", None)
        context = lock if lock is not None else threading.RLock()
        with context:
            conn = getattr(peer, "_conn", None)
            if conn is None:
                raise RuntimeError("connection_missing_during_handoff")
            if bool(getattr(conn, "in_transaction", False)):
                raise RuntimeError("transaction_open_during_handoff")
            closer = getattr(peer, "_close_published_connection", None)
            if not callable(closer) or not closer(
                conn, context="idle writer handoff", reraise=True
            ):
                raise RuntimeError("connection_close_failed")
            if getattr(peer, "_conn", None) is not None:
                raise RuntimeError("connection_remained_published")
    for peer in providers:
        vector_lock = getattr(peer, "_vector_lock", None)
        lock = vector_lock if vector_lock is not None else threading.RLock()
        with lock:
            vector = getattr(peer, "_vector_store", None)
            if vector is not None:
                vector.close()
            peer._vector_store = None
            close_embedder(getattr(peer, "_embedder", None))
            peer._embedder = None


def _restore_writer_resources(providers: tuple[Any, ...]) -> bool:
    """Best-effort reversible recovery while the exact OS lease is retained."""

    from ...capture import resume_writer_after_handoff
    from .process_lifecycle import initialize_writer_runtime

    try:
        for peer in providers:
            lease = getattr(peer, "_truth_writer_lease", None)
            if lease is None or not bool(getattr(lease, "acquired", False)):
                raise RuntimeError("writer_lease_missing_during_restore")
            peer._truth_writer_role = "owner"
            conn = getattr(peer, "_conn", None)
            connection_healthy = False
            if conn is not None:
                try:
                    conn.execute("SELECT 1").fetchone()
                    query_only = conn.execute("PRAGMA query_only").fetchone()
                    connection_healthy = bool(
                        query_only is not None and int(query_only[0] or 0) == 0
                    )
                except Exception:
                    connection_healthy = False
            if connection_healthy:
                resume_writer_after_handoff(peer)
            else:
                if conn is not None:
                    closer = getattr(peer, "_close_published_connection", None)
                    if not callable(closer) or not closer(
                        conn, context="idle handoff restore", reraise=False
                    ):
                        raise RuntimeError("writer_connection_restore_blocked")
                vector = getattr(peer, "_vector_store", None)
                if vector is not None:
                    try:
                        vector.close()
                    except Exception:
                        logger.warning(
                            "Scope Recall discarded an unhealthy vector handle "
                            "during writer handoff restore"
                        )
                    peer._vector_store = None
                try:
                    close_embedder(getattr(peer, "_embedder", None))
                except Exception:
                    logger.warning(
                        "Scope Recall discarded an unhealthy embedder during "
                        "writer handoff restore"
                    )
                peer._embedder = None
                initializer = getattr(peer, "_initialize_writer_runtime", None)
                if callable(initializer):
                    initializer()
                else:
                    initialize_writer_runtime(peer)
            writer = getattr(peer, "_writer_thread", None)
            if (
                getattr(peer, "_conn", None) is None
                or writer is None
                or not writer.is_alive()
            ):
                raise RuntimeError("writer_runtime_restore_incomplete")
        storage_dir = getattr(providers[0], "_storage_dir", None)
        if storage_dir is None:
            raise RuntimeError("storage_identity_missing_during_restore")
        counts = truth_writer_process_snapshot(storage_dir)
        if counts != {
            "same_process_holder_count": len(providers),
            "connection_pin_count": len(providers),
        }:
            raise RuntimeError("writer_authority_restore_count_mismatch")
        for peer in providers:
            peer._writer_handoff_fenced = False
        return True
    except Exception:
        logger.warning("Scope Recall fenced writer runtime restore failed")
        for peer in providers:
            peer._truth_writer_role = "unknown"
            peer._writer_handoff_fenced = True
        return False


def _release_named_authority(providers: tuple[Any, ...]) -> bool:
    first_error: BaseException | None = None
    for peer in providers:
        lease = getattr(peer, "_truth_writer_lease", None)
        try:
            if lease is None:
                raise RuntimeError("provider_lease_missing_during_handoff")
            lease.release()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    storage_dir = getattr(providers[0], "_storage_dir", None)
    if storage_dir is None:
        raise RuntimeError("storage_identity_missing_during_release")
    counts = truth_writer_process_snapshot(storage_dir)
    released = (
        counts["same_process_holder_count"] == 0
        and counts["connection_pin_count"] == 0
        and all(
            not bool(getattr(getattr(peer, "_truth_writer_lease", None), "acquired", False))
            for peer in providers
        )
    )
    if not released:
        if first_error is not None:
            raise first_error
        raise RuntimeError("writer_authority_remained_after_handoff")
    # A close-then-raise is observable but authority is definitely gone.  The
    # caller may safely continue to read-only mode instead of resurrecting a
    # writer from an already-released lease.
    return first_error is not None


def _validate_read_only_runtime(provider: Any) -> None:
    """Refuse to report a healthy reader when its durable pager did not reopen."""

    db_path = getattr(provider, "_db_path", None)
    database_exists = db_path is not None and Path(db_path).is_file()
    conn = getattr(provider, "_conn", None)
    if database_exists and conn is None:
        raise RuntimeError("reader_reopen_failed")
    if conn is not None:
        try:
            query_only = conn.execute("PRAGMA query_only").fetchone()
        except Exception as exc:
            raise RuntimeError("reader_query_only_unknown") from exc
        if query_only is None or int(query_only[0] or 0) != 1:
            raise RuntimeError("reader_query_only_disabled")
    if str(getattr(provider, "_runtime_status", "") or "") != (
        "active_read_only"
    ):
        raise RuntimeError("reader_runtime_status_mismatch")


def _perform_idle_handoff(provider: Any, state: Any) -> None:
    from ...capture import quiesce_writer_for_handoff
    from .process_lifecycle import initialize_read_only_runtime

    submissions: list[Any] = []
    backgrounds: list[Any] = []
    lifecycles: list[Any] = []
    activities: list[Any] = []
    providers: tuple[Any, ...] = ()
    generations: dict[int, int] = {}
    teardown_started = False
    release_started = False
    authority_released = False
    phase = "preflight"
    telemetry_event = ""
    telemetry_deactivate_owner = False
    try:
        with state.lock:
            if bool(getattr(state, "release_uncertain", False)):
                return
            state.handoff_thread_id = threading.get_ident()
            state.handoff_fenced = True
            state.handoff_generation = int(getattr(state, "handoff_generation", 0) or 0) + 1
            state.state = "HANDOFF_PENDING"
            state.last_handoff_failure_code = ""

        _publish_runtime_telemetry(provider, event_kind="handoff_pending")

        providers = tuple(
            peer
            for peer in live_providers_for_database(provider)
            if getattr(peer, "_truth_writer_role", None) == "owner"
        )
        if provider not in providers:
            raise RuntimeError("trigger_not_owner")
        submissions = _acquire_provider_locks(providers, "_capture_submission_lock")
        backgrounds = _acquire_background_locks(providers)
        lifecycles = _acquire_provider_locks(providers, "_writer_lifecycle_lock")
        now = time.monotonic()
        for peer in providers:
            veto = _idle_veto(peer, now=now, writer_may_be_stopped=False)
            if veto:
                raise RuntimeError(veto)
            snapshot = _activity_snapshot(peer, now=now)
            generations[id(peer)] = int(snapshot["generation"])
            peer._writer_handoff_fenced = True
        authority_veto = _exact_authority_veto(providers)
        if authority_veto:
            raise RuntimeError(authority_veto)

        # Accepted captures must finish under their existing lifecycle unit.
        # Keep every submission lock and every fence while lifecycle locks are
        # released for the writer consumers to drain.
        _release_provider_locks(lifecycles)
        phase = "quiesce"
        for peer in providers:
            quiesce_writer_for_handoff(peer, timeout=HANDOFF_LOCK_TIMEOUT_SECONDS)
        phase = "final_recheck"
        lifecycles = _acquire_provider_locks(providers, "_writer_lifecycle_lock")
        # This is the handoff linearization point. Activity that acquired its
        # lock first changes generation and vetoes below. Activity arriving
        # later waits until reader initialization, then follows the ordinary
        # reader-promotion path; it cannot interleave between final check and
        # OS lease release.
        activities = _acquire_provider_locks(
            providers, "_writer_handoff_activity_lock"
        )
        final_now = time.monotonic()
        for peer in providers:
            veto = _idle_veto(peer, now=final_now, writer_may_be_stopped=True)
            if veto:
                raise RuntimeError(veto)
            if _activity_snapshot(peer, now=final_now)["generation"] != generations[id(peer)]:
                raise RuntimeError("activity_generation_changed")
        authority_veto = _exact_authority_veto(providers)
        if authority_veto:
            raise RuntimeError(authority_veto)

        teardown_started = True
        phase = "writer_resource_close"
        _close_writer_resources(providers)
        storage_dir = getattr(provider, "_storage_dir", None)
        if storage_dir is None:
            raise RuntimeError("storage_identity_missing_during_handoff")
        close_counts = truth_writer_process_snapshot(storage_dir)
        if close_counts["connection_pin_count"] != 0:
            raise RuntimeError("connection_pins_remain_after_close")
        release_started = True
        phase = "lease_release"
        release_had_exception = _release_named_authority(providers)
        authority_released = True

        phase = "reader_initialization"
        for peer in providers:
            peer._truth_writer_lease = None
            peer._truth_writer_role = "reader"
            peer._truth_writer_owner = {}
            initialize_read_only_runtime(peer)
            _validate_read_only_runtime(peer)
            peer._writer_handoff_fenced = False
        with state.lock:
            state.handoff_fenced = False
            state.state = "READER"
            state.successful_handoff_count = int(
                getattr(state, "successful_handoff_count", 0) or 0
            ) + 1
            state.last_handoff_at = datetime.now(timezone.utc).isoformat()
            state.last_handoff_monotonic = time.monotonic()
            state.last_handoff_reason_code = "idle_process_handoff"
            state.last_handoff_failure_code = (
                "lease_close_then_raise" if release_had_exception else ""
            )
            state.release_uncertain = False
            state.operator_action_required = False
        telemetry_event = "handoff_succeeded"
        telemetry_deactivate_owner = True
    except BaseException as exc:
        failure_code = _content_free_failure_code(exc, phase=phase)
        if not teardown_started:
            resume_failed = False
            if not lifecycles and providers:
                try:
                    lifecycles = _acquire_provider_locks(
                        providers, "_writer_lifecycle_lock"
                    )
                except Exception:
                    lifecycles = []
                    resume_failed = True
            if providers and lifecycles:
                try:
                    _abort_quiesced_handoff(providers)
                except Exception:
                    logger.warning(
                        "Scope Recall fenced writer handoff resume failed"
                    )
                    resume_failed = True
            if resume_failed:
                failure_code = "writer_resume_failed"
                for peer in providers:
                    peer._truth_writer_role = "unknown"
                    peer._writer_handoff_fenced = True
                _set_process_recovery_required(state, failure_code)
            else:
                _set_process_failure(state, failure_code, uncertain=False)
        elif authority_released:
            for peer in providers:
                peer._truth_writer_role = "reader"
                peer._writer_handoff_fenced = False
            with state.lock:
                state.handoff_fenced = False
                state.state = "READER_DEGRADED"
                state.last_handoff_failure_code = failure_code
                state.release_uncertain = False
                state.operator_action_required = True
        elif not release_started:
            restored = _restore_writer_resources(providers)
            if restored:
                _set_process_failure(state, failure_code, uncertain=False)
            else:
                _set_process_recovery_required(state, "writer_restore_failed")
        else:
            for peer in providers:
                peer._truth_writer_role = "unknown"
                peer._writer_handoff_fenced = True
            _set_process_failure(state, failure_code, uncertain=True)
        telemetry_event = "handoff_failed"
        telemetry_deactivate_owner = authority_released
        logger.warning(
            "Scope Recall idle writer handoff did not complete (reason=%s)",
            failure_code,
        )
    finally:
        _release_provider_locks(activities)
        _release_provider_locks(lifecycles)
        _release_provider_locks(backgrounds)
        _release_provider_locks(submissions)
        with state.lock:
            state.handoff_thread_id = 0
    if telemetry_event:
        _publish_runtime_telemetry(
            provider,
            event_kind=telemetry_event,
            deactivate_owner=telemetry_deactivate_owner,
        )


def _handoff_thread_main(provider: Any, state: Any) -> None:
    orchestration = state.orchestration_lock
    acquired = orchestration.acquire(blocking=False)
    try:
        if acquired:
            _perform_idle_handoff(provider, state)
    finally:
        if acquired:
            orchestration.release()
        with state.lock:
            state.handoff_in_progress = False


def maybe_schedule_idle_writer_handoff(provider: Any) -> bool:
    """Start at most one process-wide handoff worker after a cheap idle probe."""

    if getattr(provider, "_truth_writer_role", None) != "owner":
        return False
    threshold = idle_release_seconds(provider)
    if threshold <= 0:
        return False
    now = time.monotonic()
    initialize_writer_handoff_activity(provider)
    with _activity_lock(provider):
        last_probe = float(getattr(provider, "_writer_handoff_last_probe", 0.0) or 0.0)
        if now - last_probe < IDLE_PROBE_INTERVAL_SECONDS:
            return False
        setattr(provider, "_writer_handoff_last_probe", now)
    if _idle_veto(provider, now=now, writer_may_be_stopped=False):
        return False
    storage_dir = getattr(provider, "_storage_dir", None)
    if storage_dir is None:
        return False
    state = process_writer_handoff_state(storage_dir)
    with state.lock:
        if (
            bool(getattr(state, "handoff_in_progress", False))
            or bool(getattr(state, "handoff_fenced", False))
            or bool(getattr(state, "release_uncertain", False))
        ):
            return False
        last_monotonic = float(
            getattr(state, "last_handoff_monotonic", 0.0) or 0.0
        )
        if last_monotonic > 0 and now - last_monotonic < HANDOFF_COOLDOWN_SECONDS:
            return False
        state.handoff_in_progress = True
    thread = threading.Thread(
        target=_handoff_thread_main,
        args=(provider, state),
        name="scope-recall-writer-handoff",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        with state.lock:
            state.handoff_in_progress = False
        raise
    return True


__all__ = [
    "DEFAULT_IDLE_RELEASE_SECONDS",
    "active_truth_work",
    "current_truth_work_started_before_fence",
    "idle_release_seconds",
    "initialize_writer_handoff_activity",
    "maybe_schedule_idle_writer_handoff",
    "note_writer_promotion_succeeded",
    "note_writer_shutdown_succeeded",
    "note_truth_activity",
    "note_user_activity",
    "writer_handoff_status",
]
