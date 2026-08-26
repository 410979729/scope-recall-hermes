"""Yuheng r2 P0 counterexamples: positive write-authority lifecycle.

These five cases failed on the mixed-closure freeze and passed on public
1.9.3. They prove the helper is actually used at the call sites, not only
defined.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import scope_recall.capture as capture_module
import scope_recall.provider as provider_module
from scope_recall.capture import (
    WRITE_AUTHORITY_BUSY,
    _drain_relation_rebuild_debt,
    store_now,
)


class _TrackingRLock:
    """Record acquire order and whether this lock is still held at each event."""

    def __init__(self, label: str, order: list[str], inner: threading.RLock | None = None) -> None:
        self.label = label
        self.order = order
        self.depth = 0
        self._inner = inner or threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.order.append(self.label)
        acquired = self._inner.acquire(blocking, timeout)
        if acquired:
            self.depth += 1
        return acquired

    def release(self) -> None:
        self._inner.release()
        self.depth -= 1

    def __enter__(self) -> "_TrackingRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class _ReaderCaptureProvider:
    def __init__(self) -> None:
        self._config: dict = {"relation_extraction_enabled": True}
        self._truth_writer_role = "reader"
        self._shutdown_requested = threading.Event()
        self._writer_lifecycle_lock = threading.RLock()
        self._lock = threading.RLock()
        self._scope_id = "scope-local"
        self._shared_scope_id = "scope-shared"
        self._shared_pool_scope_id = ""
        self._scope = SimpleNamespace(
            platform="cli",
            user_id="lease-user",
            chat_id="",
            thread_id="",
            gateway_session_key="",
            agent_identity="tester",
            agent_workspace="hermes",
        )
        self._conn_requested = False

    def _truth_writes_blocked(self) -> bool:
        return True

    def _require_conn(self):
        self._conn_requested = True
        raise AssertionError("reader must not open a writable capture connection")


def test_capture_store_now_rejects_reader_before_store_row(monkeypatch):
    provider = _ReaderCaptureProvider()
    called = {"store_row": 0}

    def boom(*_args, **_kwargs):
        called["store_row"] += 1
        raise AssertionError("store_row must not run for a reader")

    monkeypatch.setattr(capture_module, "store_row", boom)
    with pytest.raises(RuntimeError, match=WRITE_AUTHORITY_BUSY):
        store_now(
            provider,
            content="Reader capture must not persist this durable workflow note.",
            source="turn-user",
            target="ops",
            session_id="reader-store",
        )
    assert called["store_row"] == 0
    assert provider._conn_requested is False


def test_idle_relation_maintenance_rejects_reader_before_bounded_drain(monkeypatch):
    provider = _ReaderCaptureProvider()
    probed = {"count": 0}

    def mark(*_args, **_kwargs):
        probed["count"] += 1
        return False

    monkeypatch.setattr(capture_module, "drain_relation_frequency_work", mark)
    _drain_relation_rebuild_debt(provider)
    assert probed["count"] == 0
    assert provider._conn_requested is False


def test_provider_direct_store_rejects_reader_before_memory_op(monkeypatch):
    entered = {"count": 0}

    def boom(*_args, **_kwargs):
        entered["count"] += 1
        raise AssertionError("store_memory_now must not run for a reader")

    monkeypatch.setattr(provider_module, "store_memory_now", boom)
    inst = provider_module.ScopeRecallMemoryProvider()
    inst._truth_writer_role = "reader"
    with pytest.raises(RuntimeError, match=WRITE_AUTHORITY_BUSY):
        inst._store_now(
            content="Reader provider must not persist this durable workflow note.",
            source="tool-store",
            target="ops",
            session_id="reader-direct",
        )
    assert entered["count"] == 0


def test_sqlite_rollback_enters_lifecycle_before_connection_lock():
    order: list[str] = []
    held_at_connection: list[int] = []
    inst = provider_module.ScopeRecallMemoryProvider()
    lifecycle = _TrackingRLock("lifecycle", order)
    inst._writer_lifecycle_lock = lifecycle

    class _ConnectionLock(_TrackingRLock):
        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            held_at_connection.append(lifecycle.depth)
            return super().acquire(blocking, timeout)

    inst._lock = _ConnectionLock("connection", order)
    inst._conn = None
    inst._rollback_conn_after_error("unit")
    assert order, "rollback must acquire locks"
    assert order[0] == "lifecycle"
    assert "connection" in order
    assert order.index("lifecycle") < order.index("connection")
    assert held_at_connection, "connection lock must be acquired"
    assert all(depth >= 1 for depth in held_at_connection), (
        "lifecycle must still be held when entering the connection lock"
    )


def test_sqlite_recovery_enters_lifecycle_before_peer_recovery(monkeypatch):
    order: list[str] = []
    held_at_peer: list[int] = []
    inst = provider_module.ScopeRecallMemoryProvider()
    lifecycle = _TrackingRLock("lifecycle", order)
    inst._writer_lifecycle_lock = lifecycle
    inst._lock = _TrackingRLock("connection", order)
    inst._conn = None

    def peer(_context: str) -> dict[str, int]:
        held_at_peer.append(lifecycle.depth)
        order.append("peer")
        return {
            "peer_providers_checked": 0,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 0,
        }

    monkeypatch.setattr(inst, "_rollback_peer_provider_transactions", peer)
    inst._recover_sqlite_connection_after_error("unit")
    assert order[0] == "lifecycle"
    assert "peer" in order
    assert order.index("lifecycle") < order.index("peer")
    assert held_at_peer, "peer recovery must run"
    assert all(depth >= 1 for depth in held_at_peer), (
        "lifecycle must still be held when peer recovery starts"
    )


def test_require_conn_reader_does_not_open_writable_pager():
    inst = provider_module.ScopeRecallMemoryProvider()
    inst._conn = None
    inst._truth_writer_role = "reader"
    inst._runtime_status = "active_read_only"
    opened = {"count": 0}

    def boom() -> object:
        opened["count"] += 1
        raise AssertionError("reader must not open a writable truth pager")

    inst._open_runtime_connection = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="truth_writer_busy"):
        inst._require_conn()
    assert opened["count"] == 0
