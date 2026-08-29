"""Same-process provider registry and peer SQLite transaction recovery.

Peer recovery rolls back dirty writer transactions on other live providers
that share this process and the same truth database. Lock order is the
peer's writer-lifecycle RLock, then the peer's ``_lock``. Busy peers are
skipped without blocking. Registry membership is process-local and weak.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_SHARED_PROVIDER_STATE_NAME = "_scope_recall_process_provider_registry_v1"


def _shared_provider_registry() -> tuple[Any, weakref.WeakSet[Any]]:
    candidate = types.ModuleType(_SHARED_PROVIDER_STATE_NAME)
    setattr(candidate, "lock", threading.RLock())
    setattr(candidate, "registry", weakref.WeakSet())
    setattr(candidate, "pid", os.getpid())
    holder = sys.modules.setdefault(_SHARED_PROVIDER_STATE_NAME, candidate)
    lock = getattr(holder, "lock", None)
    registry = getattr(holder, "registry", None)
    if not hasattr(lock, "acquire") or not hasattr(lock, "release"):
        lock = threading.RLock()
        setattr(holder, "lock", lock)
    if not isinstance(registry, weakref.WeakSet):
        registry = weakref.WeakSet()
        setattr(holder, "registry", registry)
    if int(getattr(holder, "pid", os.getpid()) or 0) != os.getpid():
        registry = weakref.WeakSet()
        setattr(holder, "registry", registry)
        setattr(holder, "pid", os.getpid())
    return lock, registry


PROVIDER_REGISTRY_LOCK, PROVIDER_REGISTRY = _shared_provider_registry()


def _current_provider_registry() -> tuple[Any, weakref.WeakSet[Any]]:
    """Refresh forked state and keep compatibility globals current."""

    global PROVIDER_REGISTRY_LOCK, PROVIDER_REGISTRY
    PROVIDER_REGISTRY_LOCK, PROVIDER_REGISTRY = _shared_provider_registry()
    return PROVIDER_REGISTRY_LOCK, PROVIDER_REGISTRY


def same_truth_database_path(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    left_text = os.fspath(left)
    right_text = os.fspath(right)
    try:
        return os.path.samefile(left_text, right_text)
    except OSError:
        left_key = os.path.normcase(os.path.realpath(left_text))
        right_key = os.path.normcase(os.path.realpath(right_text))
        return left_key == right_key


def provider_shutdown_requested(peer: Any) -> bool:
    shutdown = getattr(peer, "_shutdown_requested", None)
    is_set = getattr(shutdown, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def try_acquire_nonblocking(lock: Any) -> bool:
    acquire = getattr(lock, "acquire", None)
    if not callable(acquire):
        return False
    try:
        return bool(acquire(blocking=False))
    except TypeError:
        return bool(acquire(False))


def peer_writer_lifecycle_lock(peer: Any) -> Any | None:
    lock = getattr(peer, "_writer_lifecycle_lock", None)
    acquire = getattr(lock, "acquire", None)
    release = getattr(lock, "release", None)
    if callable(acquire) and callable(release):
        return lock
    return None


def register_provider_instance(provider: Any) -> None:
    """Register a live provider for same-process SQLite lock recovery."""

    registry_lock, registry = _current_provider_registry()
    with registry_lock:
        registry.add(provider)


def unregister_provider_instance(provider: Any) -> None:
    """Drop a provider from the same-process recovery registry."""

    registry_lock, registry = _current_provider_registry()
    with registry_lock:
        registry.discard(provider)


def live_providers_for_database(provider: Any) -> tuple[Any, ...]:
    """Return a stable snapshot of live same-database process peers.

    The weak registry remains the membership authority.  The returned tuple
    contains no paths or identities and is intended only for process-local
    lifecycle coordination.
    """

    db_path = getattr(provider, "_db_path", None)
    if db_path is None:
        return ()
    registry_lock, registry = _current_provider_registry()
    with registry_lock:
        peers = [
            item
            for item in list(registry)
            if not provider_shutdown_requested(item)
            and same_truth_database_path(getattr(item, "_db_path", None), db_path)
        ]
    return tuple(sorted(peers, key=id))


def rollback_peer_provider_transactions(provider: Any, context: str) -> dict[str, int]:
    """Rollback dirty same-process peer providers that share this SQLite DB.

    A recoverable `database is locked` error can be caused by another live
    Scope Recall provider instance in the same process, not by the current
    connection. The process-local registry lets store recovery clear those
    peer dirty transactions before probing/reopening the current connection.
    """
    db_path = getattr(provider, "_db_path", None)
    result = {
        "peer_providers_checked": 0,
        "peer_rollbacks": 0,
        "peer_rollback_errors": 0,
        "peer_busy_skipped": 0,
    }
    if db_path is None:
        return result
    registry_lock, registry = _current_provider_registry()
    with registry_lock:
        peers = [item for item in list(registry) if item is not provider]
    for peer in peers:
        if provider_shutdown_requested(peer):
            continue
        peer_db_path = getattr(peer, "_db_path", None)
        if peer_db_path is None or not same_truth_database_path(peer_db_path, db_path):
            continue
        result["peer_providers_checked"] += 1
        peer_lock = getattr(peer, "_lock", None)
        acquire = getattr(peer_lock, "acquire", None)
        release = getattr(peer_lock, "release", None)
        if not callable(acquire) or not callable(release):
            continue
        held_lifecycle = peer_writer_lifecycle_lock(peer)
        if held_lifecycle is not None and not try_acquire_nonblocking(
            held_lifecycle
        ):
            result["peer_busy_skipped"] += 1
            continue
        try:
            acquired = try_acquire_nonblocking(peer_lock)
            if not acquired:
                result["peer_busy_skipped"] += 1
                continue
            try:
                if provider_shutdown_requested(peer):
                    continue
                if getattr(peer, "_truth_writer_role", None) != "owner":
                    continue
                peer_conn = getattr(peer, "_conn", None)
                if peer_conn is None or not getattr(peer_conn, "in_transaction", False):
                    continue
                try:
                    peer_conn.rollback()
                    result["peer_rollbacks"] += 1
                except Exception:
                    result["peer_rollback_errors"] += 1
                    logger.exception(
                        "Scope Recall peer SQLite rollback failed after %s", context
                    )
                    quarantine = getattr(peer, "_quarantine_sqlite_connection", None)
                    if callable(quarantine):
                        quarantine(peer_conn, f"peer recovery: {context}")
            finally:
                release()
        finally:
            if held_lifecycle is not None:
                held_lifecycle.release()
    return result
