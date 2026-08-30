"""Single write-kernel port for lease, authority, and network boundaries.

Call sites must import this module instead of reaching into capture /
writer_lease / transaction_guard for new durable writes.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import threading
from typing import Any, Iterator

from .transaction_guard import (
    OpenTransactionAtNetworkBoundaryError,
    assert_no_open_transaction,
    prepare_network_boundary,
    release_snapshot_transaction,
    timed_truth_transaction,
)
from .writer_lease import (
    TruthWriterBusyError,
    TruthWriterLease,
    holding_truth_writer_lease,
    sanitized_truth_writer_owner,
)

WRITE_AUTHORITY_BUSY = "truth_writer_busy"
COMMAND_CAPTURE_BARRIER_MISSING = "command_capture_barrier_missing"

_COMMAND_WRITE_STATE = threading.local()
_TRUTH_MUTATION_ADMISSION_STATE = threading.local()


def _writer_lifecycle_lock(provider: Any):
    return getattr(provider, "_writer_lifecycle_lock", None) or nullcontext()


def has_positive_write_authority(provider: Any) -> bool:
    """Return whether a new durable write unit may start."""

    if getattr(provider, "_truth_writer_role", None) != "owner":
        return False
    blocked = getattr(provider, "_truth_writes_blocked", None)
    if callable(blocked):
        return not bool(blocked())
    shutdown = getattr(provider, "_shutdown_requested", None)
    if shutdown is not None and shutdown.is_set():
        return False
    return True


def require_positive_write_authority(provider: Any) -> None:
    if not has_positive_write_authority(provider):
        raise RuntimeError(WRITE_AUTHORITY_BUSY)


def _command_write_states() -> dict[int, dict[str, Any]]:
    states = getattr(_COMMAND_WRITE_STATE, "states", None)
    if states is None:
        states = {}
        _COMMAND_WRITE_STATE.states = states
    return states


def _truth_mutation_admissions() -> dict[int, int]:
    admissions = getattr(_TRUTH_MUTATION_ADMISSION_STATE, "admissions", None)
    if admissions is None:
        admissions = {}
        _TRUTH_MUTATION_ADMISSION_STATE.admissions = admissions
    return admissions


def _truth_mutation_is_admitted(provider: Any) -> bool:
    """Return whether this thread owns an explicit mutation admission token."""

    return _truth_mutation_admissions().get(id(provider), 0) > 0


@contextmanager
def _admitted_truth_mutation(provider: Any) -> Iterator[None]:
    """Mark one already-authorized mutation, including accepted capture drain."""

    admissions = _truth_mutation_admissions()
    key = id(provider)
    admissions[key] = admissions.get(key, 0) + 1
    try:
        yield
    finally:
        remaining = admissions.get(key, 1) - 1
        if remaining > 0:
            admissions[key] = remaining
        else:
            admissions.pop(key, None)


@contextmanager
def command_write_access(
    provider: Any,
    *,
    capture_barrier: bool = False,
    user_initiated: bool = False,
) -> Iterator[None]:
    """Admit one command through the capture, lifecycle, and handoff gates.

    Tool dispatch and the provider-compatible command adapter deliberately use
    this same boundary.  The adapter therefore re-enters when invoked from a
    tool call instead of double-counting active work.  A nested caller may not
    add the capture barrier after lifecycle admission because that would invert
    the global submission-before-lifecycle lock order; such classifier drift is
    rejected rather than silently weakening capture/delete ordering.
    """

    states = _command_write_states()
    key = id(provider)
    state = states.get(key)
    if state is not None:
        if capture_barrier and not bool(state.get("capture_barrier")):
            raise RuntimeError(COMMAND_CAPTURE_BARRIER_MISSING)
        state["depth"] = int(state.get("depth", 0)) + 1
        try:
            yield
        finally:
            state["depth"] = max(1, int(state.get("depth", 1))) - 1
        return

    from ._internal.runtime.writer_handoff import active_truth_work

    if capture_barrier:
        from .capture import capture_mutation_barrier

        barrier = capture_mutation_barrier(provider)
    else:
        barrier = nullcontext()

    with barrier:
        with _writer_lifecycle_lock(provider):
            require_positive_write_authority(provider)
            with _admitted_truth_mutation(provider):
                with active_truth_work(provider, user_initiated=user_initiated):
                    states[key] = {
                        "capture_barrier": bool(capture_barrier),
                        "depth": 1,
                        "provider": provider,
                    }
                    try:
                        yield
                    finally:
                        states.pop(key, None)


@contextmanager
def hold_positive_write_authority(provider: Any) -> Iterator[None]:
    """Hold lifecycle from the authority check through the caller's durable unit."""

    with command_write_access(provider):
        yield


__all__ = [
    "OpenTransactionAtNetworkBoundaryError",
    "TruthWriterBusyError",
    "TruthWriterLease",
    "COMMAND_CAPTURE_BARRIER_MISSING",
    "WRITE_AUTHORITY_BUSY",
    "assert_no_open_transaction",
    "command_write_access",
    "has_positive_write_authority",
    "hold_positive_write_authority",
    "holding_truth_writer_lease",
    "prepare_network_boundary",
    "release_snapshot_transaction",
    "require_positive_write_authority",
    "sanitized_truth_writer_owner",
    "timed_truth_transaction",
]
