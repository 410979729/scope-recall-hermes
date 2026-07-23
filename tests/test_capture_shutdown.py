"""Fail-closed capture-writer shutdown contracts."""
from __future__ import annotations

import queue
import threading
import time

import pytest

import scope_recall.capture as capture


class _Provider:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._write_queue: queue.Queue[object] = queue.Queue()
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


def test_flush_rejects_dead_writer_with_pending_work() -> None:
    provider = _Provider()
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    provider._writer_thread = worker
    provider._write_queue.put(object())

    assert capture.flush_writer(provider, timeout=0.01) is False
