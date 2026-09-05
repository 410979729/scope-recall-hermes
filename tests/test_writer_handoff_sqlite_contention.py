"""Issue #71: nonblocking provider-lock serialization for SQLite getters."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import scope_recall._internal.runtime.writer_handoff as writer_handoff_module
from scope_recall._internal.runtime.writer_handoff import (
    _CONTENT_FREE_FAILURE_CODES,
    _connection_total_changes,
    _content_free_failure_code,
    _idle_veto,
    active_truth_work,
    initialize_writer_handoff_activity,
    maybe_schedule_idle_writer_handoff,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NONBLOCKING_BOUND_SECONDS = 0.5
_SUBPROCESS_TIMEOUT_SECONDS = 8.0


class _SqliteTouchTracker:
    """Record SQLite metadata getter access without changing values."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner
        self.touches: list[str] = []

    @property
    def total_changes(self) -> int:
        self.touches.append("total_changes")
        return int(self.inner.total_changes)

    @property
    def in_transaction(self) -> bool:
        self.touches.append("in_transaction")
        return bool(self.inner.in_transaction)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _EmptyQueue:
    def empty(self) -> bool:
        return True


def _idle_owner(
    *,
    conn: Any,
    lock: Any | None,
    storage_dir: Path | None = None,
    now: float | None = None,
) -> SimpleNamespace:
    observed = time.monotonic() if now is None else float(now)
    provider = SimpleNamespace(
        _config={"writer_lease": {"idle_release_seconds": 30}},
        _truth_writer_role="owner",
        _shutdown_requested=threading.Event(),
        _writer_handoff_activity_lock=threading.RLock(),
        _writer_handoff_last_user_activity=observed - 60,
        _writer_handoff_last_truth_activity=observed - 60,
        _writer_handoff_activity_generation=0,
        _writer_handoff_active_truth_work=0,
        _writer_handoff_last_probe=0.0,
        _writer_handoff_fenced=False,
        _foreground_busy_count=0,
        _write_queue=_EmptyQueue(),
        _capture_queue_processing=0,
        _journal_digest_thread=None,
        _writer_thread=_AliveThread(),
        _conn=conn,
        _storage_dir=storage_dir,
    )
    if lock is not None:
        provider._lock = lock
    initialize_writer_handoff_activity(provider)
    with provider._writer_handoff_activity_lock:
        provider._writer_handoff_last_user_activity = observed - 60
        provider._writer_handoff_last_truth_activity = observed - 60
    return provider


def _hold_lock(lock: threading.RLock) -> tuple[threading.Thread, threading.Event]:
    held = threading.Event()
    release = threading.Event()

    def run() -> None:
        with lock:
            held.set()
            release.wait(5.0)

    thread = threading.Thread(target=run, name="provider-lock-holder")
    thread.start()
    assert held.wait(timeout=2.0)
    return thread, release


def test_contended_metadata_reads_do_not_touch_sqlite_or_block() -> None:
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    tracker = _SqliteTouchTracker(raw)
    lock = threading.RLock()
    provider = _idle_owner(conn=tracker, lock=lock)
    thread, release = _hold_lock(lock)
    try:
        started = time.monotonic()
        changes = _connection_total_changes(provider)
        veto = _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(timeout=2.0)
        raw.close()

    assert changes is None
    assert veto == "busy_provider_lock"
    assert tracker.touches == []
    assert elapsed < _NONBLOCKING_BOUND_SECONDS


def test_missing_provider_lock_does_not_invent_a_guard_or_touch_sqlite() -> None:
    raw = sqlite3.connect(":memory:")
    tracker = _SqliteTouchTracker(raw)
    provider = _idle_owner(conn=tracker, lock=None)
    try:
        assert getattr(provider, "_lock", None) is None
        started = time.monotonic()
        changes = _connection_total_changes(provider)
        veto = _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
        elapsed = time.monotonic() - started
    finally:
        raw.close()

    assert changes is None
    assert veto == "transaction_unknown"
    assert tracker.touches == []
    assert elapsed < _NONBLOCKING_BOUND_SECONDS


def test_uncontended_and_reentrant_reads_use_the_published_connection() -> None:
    raw = sqlite3.connect(":memory:")
    tracker = _SqliteTouchTracker(raw)
    lock = threading.RLock()
    provider = _idle_owner(conn=tracker, lock=lock)
    try:
        assert _connection_total_changes(provider) == (tracker, 0)
        assert _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False) == ""
        assert tracker.touches == ["total_changes", "in_transaction"]

        tracker.touches.clear()
        with lock:
            raw.execute("BEGIN")
            assert _connection_total_changes(provider) == (tracker, 0)
            assert (
                _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
                == "transaction_open"
            )
            raw.rollback()
        assert tracker.touches == ["total_changes", "in_transaction"]
    finally:
        raw.close()


def test_missing_or_closed_published_connection_is_unknown() -> None:
    raw = sqlite3.connect(":memory:")
    lock = threading.RLock()
    provider = _idle_owner(conn=raw, lock=lock)
    try:
        raw.close()
        assert _connection_total_changes(provider) is None
        assert _idle_veto(
            provider, now=time.monotonic(), writer_may_be_stopped=False
        ) in {"transaction_unknown", "connection_missing"}

        provider._conn = None
        assert _connection_total_changes(provider) is None
        assert (
            _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
            == "connection_missing"
        )
    finally:
        try:
            raw.close()
        except Exception:
            pass


def test_replaced_connection_is_resolved_only_after_lock() -> None:
    stale = _SqliteTouchTracker(sqlite3.connect(":memory:"))
    live = sqlite3.connect(":memory:")
    live.execute("CREATE TABLE replaced_probe(value TEXT NOT NULL)")
    live.execute("INSERT INTO replaced_probe(value) VALUES ('live')")
    live.commit()
    lock = threading.RLock()
    provider = _idle_owner(conn=stale, lock=lock)
    try:
        with lock:
            provider._conn = live
            observed = _connection_total_changes(provider)
        assert observed is not None
        observed_conn, observed_changes = observed
        assert observed_conn is live
        assert observed_changes == int(live.total_changes)
        assert observed_changes > 0
        assert stale.touches == []
    finally:
        stale.inner.close()
        live.close()


def test_reliable_noop_preserves_truth_clock_and_write_refreshes_it() -> None:
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE truth_probe(value TEXT NOT NULL)")
    raw.commit()
    provider = _idle_owner(conn=raw, lock=threading.RLock())
    old_clock = float(provider._writer_handoff_last_truth_activity)
    old_generation = int(provider._writer_handoff_activity_generation)
    try:
        with active_truth_work(provider):
            assert provider._writer_handoff_active_truth_work == 1
        assert provider._writer_handoff_active_truth_work == 0
        assert provider._writer_handoff_last_truth_activity == old_clock
        assert provider._writer_handoff_activity_generation == old_generation

        with active_truth_work(provider):
            raw.execute("INSERT INTO truth_probe(value) VALUES ('written')")
            raw.commit()
        assert provider._writer_handoff_active_truth_work == 0
        assert provider._writer_handoff_last_truth_activity > old_clock
        assert provider._writer_handoff_activity_generation == old_generation + 1
    finally:
        raw.close()


def test_replaced_equal_counters_do_not_prove_noop() -> None:
    first = sqlite3.connect(":memory:")
    second = sqlite3.connect(":memory:")
    provider = _idle_owner(conn=first, lock=threading.RLock())
    old_clock = float(provider._writer_handoff_last_truth_activity)
    old_generation = int(provider._writer_handoff_activity_generation)
    try:
        assert first is not second
        assert first.total_changes == 0
        assert second.total_changes == 0
        with active_truth_work(provider):
            assert provider._writer_handoff_active_truth_work == 1
            with provider._lock:
                provider._conn = second
            assert provider._lock.acquire(blocking=False)
            provider._lock.release()
        assert provider._writer_handoff_active_truth_work == 0
        assert provider._writer_handoff_last_truth_activity > old_clock
        assert provider._writer_handoff_activity_generation == old_generation + 1
    finally:
        first.close()
        second.close()


def test_unknown_samples_do_not_prove_noop() -> None:
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    provider = _idle_owner(conn=raw, lock=threading.RLock())
    old_clock = float(provider._writer_handoff_last_truth_activity)
    old_generation = int(provider._writer_handoff_activity_generation)
    thread, release = _hold_lock(provider._lock)
    try:
        with active_truth_work(provider):
            assert provider._writer_handoff_active_truth_work == 1
        assert provider._writer_handoff_active_truth_work == 0
        assert provider._writer_handoff_last_truth_activity > old_clock
        assert provider._writer_handoff_activity_generation == old_generation + 1
    finally:
        release.set()
        thread.join(timeout=2.0)
        raw.close()


def test_nested_work_and_exception_keep_accounting_balanced() -> None:
    raw = sqlite3.connect(":memory:")
    provider = _idle_owner(conn=raw, lock=threading.RLock())
    try:
        with active_truth_work(provider):
            assert provider._writer_handoff_active_truth_work == 1
            with active_truth_work(provider):
                assert provider._writer_handoff_active_truth_work == 2
            assert provider._writer_handoff_active_truth_work == 1
        assert provider._writer_handoff_active_truth_work == 0
        assert int(getattr(provider._writer_handoff_thread_work, "depth", 0) or 0) == 0

        try:
            with active_truth_work(provider):
                assert provider._writer_handoff_active_truth_work == 1
                raise RuntimeError("injected_truth_work_failure")
        except RuntimeError as exc:
            assert str(exc) == "injected_truth_work_failure"
        assert provider._writer_handoff_active_truth_work == 0
        assert int(getattr(provider._writer_handoff_thread_work, "depth", 0) or 0) == 0
    finally:
        raw.close()


def test_busy_provider_fail_closes_handoff_without_starting_worker(
    tmp_path: Path, monkeypatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        writer_handoff_module,
        "_handoff_thread_main",
        lambda *_args, **_kwargs: started.append("handoff"),
    )
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    lock = threading.RLock()
    provider = _idle_owner(conn=raw, lock=lock, storage_dir=tmp_path / "scope-recall")
    thread, release = _hold_lock(lock)
    try:
        assert (
            _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
            == "busy_provider_lock"
        )
        assert maybe_schedule_idle_writer_handoff(provider) is False
        assert started == []
        assert (
            _content_free_failure_code(
                RuntimeError("busy_provider_lock"), phase="preflight"
            )
            == "busy_provider_lock"
        )
        assert "busy_provider_lock" in _CONTENT_FREE_FAILURE_CODES
    finally:
        release.set()
        thread.join(timeout=2.0)
        raw.close()


def test_sqlite_callback_subprocess_returns_unknown_without_hanging() -> None:
    script = r"""
import json, sqlite3, sys, threading, time, types
repo = sys.argv[1]
pkg = types.ModuleType("scope_recall")
pkg.__path__ = [repo]
sys.modules["scope_recall"] = pkg
from scope_recall._internal.runtime.writer_handoff import (
    _connection_total_changes,
    _idle_veto,
    initialize_writer_handoff_activity,
)
conn = sqlite3.connect(":memory:", check_same_thread=False)
lock = threading.RLock()
entered = threading.Event()
release = threading.Event()
now = time.monotonic()
provider = types.SimpleNamespace(
    _conn=conn,
    _lock=lock,
    _truth_writer_role="owner",
    _config={"writer_lease": {"idle_release_seconds": 30}},
    _shutdown_requested=threading.Event(),
    _foreground_busy_count=0,
    _write_queue=types.SimpleNamespace(empty=lambda: True),
    _capture_queue_processing=0,
    _journal_digest_thread=None,
    _writer_thread=types.SimpleNamespace(is_alive=lambda: True),
)
initialize_writer_handoff_activity(provider)
with provider._writer_handoff_activity_lock:
    provider._writer_handoff_last_user_activity = now - 60
    provider._writer_handoff_last_truth_activity = now - 60

def sql_callback():
    entered.set()
    release.wait(2.0)
    return 1

conn.create_function("hold_mutex", 0, sql_callback)

def query():
    with lock:
        conn.execute("select hold_mutex()").fetchall()

thread = threading.Thread(target=query, daemon=True)
thread.start()
assert entered.wait(2.0)
started = time.monotonic()
try:
    changes = _connection_total_changes(provider)
    veto = _idle_veto(provider, now=time.monotonic(), writer_may_be_stopped=False)
    elapsed = time.monotonic() - started
    print(json.dumps({
        "changes": changes,
        "veto": veto,
        "elapsed": elapsed,
        "sqlite": sqlite3.sqlite_version,
    }))
finally:
    release.set()
    thread.join(2.0)
    conn.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(_REPO_ROOT)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["changes"] is None
    assert payload["veto"] == "busy_provider_lock"
    assert float(payload["elapsed"]) < _NONBLOCKING_BOUND_SECONDS
