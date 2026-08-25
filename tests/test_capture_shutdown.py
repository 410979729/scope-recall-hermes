"""Fail-closed capture-writer shutdown contracts."""
from __future__ import annotations

import queue
import threading
import time

import pytest

import scope_recall.capture as capture
from scope_recall.capture_control import new_write_control_queue


class _Provider:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._write_queue: queue.Queue[object] = new_write_control_queue()
        self._writer_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._maintenance_stop = threading.Event()
        self._shutdown_requested = threading.Event()
        self._writer_lifecycle_lock = threading.RLock()
        self._writer_failed_writes = 0
        self._writer_reported_failures = 0
        self._writer_last_error_type = ""
        self._last_relation_rebuild_drain = 0.0
        self._config = {"relation_extraction_enabled": True}

    @staticmethod
    def _rollback_conn_after_error(_context: str) -> None:
        return None


def test_shutdown_blocks_new_idle_maintenance_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _Provider()
    violations: list[str] = []

    def drain(current: _Provider) -> None:
        if current._maintenance_stop.is_set():
            violations.append("maintenance_started_after_shutdown")

    monkeypatch.setattr(capture, "_drain_relation_rebuild_debt", drain)
    capture.start_writer(provider)
    time.sleep(0.05)

    capture.shutdown_writer(provider, timeout=1.0)

    assert provider._writer_thread is None
    assert violations == []


def test_shutdown_timeout_keeps_live_thread_visible_for_safe_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    started = threading.Event()
    release = threading.Event()

    def blocked_drain(_current: _Provider) -> None:
        started.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(capture, "_drain_relation_rebuild_debt", blocked_drain)
    capture.start_writer(provider)
    assert started.wait(timeout=1.0)

    with pytest.raises(RuntimeError, match="did not acknowledge"):
        capture.shutdown_writer(provider, timeout=0.05)
    assert provider._writer_thread is not None
    assert provider._writer_thread.is_alive()

    release.set()
    capture.shutdown_writer(provider, timeout=1.0)
    assert provider._writer_thread is None


def test_enqueue_after_writer_shutdown_fails_instead_of_silently_losing_write() -> None:
    provider = _Provider()
    capture.start_writer(provider)
    capture.shutdown_writer(provider, timeout=1.0)

    with pytest.raises(RuntimeError, match="shutting down"):
        capture.enqueue_store(
            provider,
            content="This write must not disappear after shutdown.",
            source="test",
            target="memory",
            session_id="shutdown-race",
        )


def test_idle_maintenance_skips_durable_unit_after_shutdown_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    provider._truth_writer_role = "owner"
    provider._conn = object()
    provider._require_conn = lambda: provider._conn
    mutations: list[str] = []
    monkeypatch.setattr(capture, "relation_frequency_debt_exists", lambda _conn: True)
    monkeypatch.setattr(
        capture,
        "drain_relation_frequency_work",
        lambda *_args, **_kwargs: mutations.append("frequency") or {},
    )
    monkeypatch.setattr(capture, "relation_rebuild_debt_exists", lambda _conn: False)

    provider._shutdown_requested.set()
    capture._drain_relation_rebuild_debt(provider)

    assert mutations == []


def test_idle_maintenance_unit_holds_lifecycle_against_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    provider._truth_writer_role = "owner"
    provider._conn = object()
    provider._require_conn = lambda: provider._conn
    entered_unit = threading.Event()
    release_unit = threading.Event()
    setter_started = threading.Event()
    setter_finished = threading.Event()
    mutations: list[str] = []

    def pausing_frequency_work(*_args, **_kwargs):
        entered_unit.set()
        assert release_unit.wait(timeout=2.0)
        mutations.append("frequency")
        return {}

    monkeypatch.setattr(capture, "relation_frequency_debt_exists", lambda _conn: True)
    monkeypatch.setattr(capture, "drain_relation_frequency_work", pausing_frequency_work)
    monkeypatch.setattr(capture, "relation_rebuild_debt_exists", lambda _conn: False)

    def run_drain() -> None:
        capture._drain_relation_rebuild_debt(provider)

    def run_shutdown_setter() -> None:
        setter_started.set()
        with provider._writer_lifecycle_lock:
            provider._shutdown_requested.set()
        setter_finished.set()

    worker = threading.Thread(target=run_drain, name="idle-maintenance")
    worker.start()
    assert entered_unit.wait(timeout=2.0)

    setter = threading.Thread(target=run_shutdown_setter, name="maintenance-shutdown-setter")
    setter.start()
    assert setter_started.wait(timeout=2.0)
    acquired = provider._writer_lifecycle_lock.acquire(blocking=False)
    if acquired:
        provider._writer_lifecycle_lock.release()
    assert acquired is False
    assert setter_finished.is_set() is False
    assert provider._shutdown_requested.is_set() is False

    release_unit.set()
    worker.join(timeout=2.0)
    setter.join(timeout=2.0)
    assert worker.is_alive() is False
    assert setter.is_alive() is False
    assert mutations == ["frequency"]
    assert setter_finished.is_set()
    assert provider._shutdown_requested.is_set()

    capture._drain_relation_rebuild_debt(provider)
    assert mutations == ["frequency"]


def test_flush_rejects_dead_writer_with_pending_work() -> None:
    provider = _Provider()
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    provider._writer_thread = worker
    provider._write_queue.put(object())

    assert capture.flush_writer(provider, timeout=0.01) is False
