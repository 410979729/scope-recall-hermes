"""Single write-kernel port for lease, authority, and network boundaries.

Call sites must import this module instead of reaching into capture /
writer_lease / transaction_guard for new durable writes.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
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


def _writer_lifecycle_lock(provider: Any):
    return getattr(provider, "_writer_lifecycle_lock", None) or nullcontext()


def has_positive_write_authority(provider: Any) -> bool:
    """Return whether a new durable write unit may start."""

    blocked = getattr(provider, "_truth_writes_blocked", None)
    if callable(blocked):
        return not bool(blocked())
    shutdown = getattr(provider, "_shutdown_requested", None)
    if shutdown is not None and shutdown.is_set():
        return False
    return getattr(provider, "_truth_writer_role", None) == "owner"


def require_positive_write_authority(provider: Any) -> None:
    if not has_positive_write_authority(provider):
        raise RuntimeError(WRITE_AUTHORITY_BUSY)


@contextmanager
def hold_positive_write_authority(provider: Any) -> Iterator[None]:
    """Hold lifecycle from the authority check through the caller's durable unit."""

    with _writer_lifecycle_lock(provider):
        require_positive_write_authority(provider)
        yield


__all__ = [
    "OpenTransactionAtNetworkBoundaryError",
    "TruthWriterBusyError",
    "TruthWriterLease",
    "WRITE_AUTHORITY_BUSY",
    "assert_no_open_transaction",
    "has_positive_write_authority",
    "hold_positive_write_authority",
    "holding_truth_writer_lease",
    "prepare_network_boundary",
    "release_snapshot_transaction",
    "require_positive_write_authority",
    "sanitized_truth_writer_owner",
    "timed_truth_transaction",
]
