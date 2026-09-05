"""Owned helper-reaper lifecycle after a request-budget timeout.

Foreground callers may return at the deadline. The store must keep the detached
reaper until teardown finishes, so explicit close and reopen cannot abandon it.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest


def _wait_lance_helper_drain(*, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    leftover: list[str] = []
    while time.monotonic() < deadline:
        leftover = [
            thread.name
            for thread in threading.enumerate()
            if thread.is_alive() and thread.name.startswith("scope-recall-lance-")
        ]
        if not leftover:
            return
        time.sleep(0.05)
    raise AssertionError(f"native helper threads still alive: {leftover}")


def _good_lance_worker() -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import json,sys\n"
            "for _ in range(8):\n"
            "    line=sys.stdin.buffer.readline()\n"
            "    if not line:\n"
            "        break\n"
            "    r=json.loads(line)\n"
            "    method=r.get('method')\n"
            "    if method=='count_rows':\n"
            "        result=0\n"
            "    elif method=='search':\n"
            "        result=[]\n"
            "    else:\n"
            "        result=True\n"
            "    print(json.dumps({'id':r['id'],'ok':True,'result':result}), flush=True)\n"
        ),
    ]


def _sleeper_worker() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def _timeout_owned_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_store: object,
    *,
    stop,
):
    monkeypatch.setattr(process_store, "_stop_worker", stop)
    monkeypatch.setattr(process_store, "_worker_command", _sleeper_worker)
    store = process_store.ProcessLanceVectorStore(
        tmp_path / "lancedb",
        table_name="memories",
        dimensions=3,
    )
    created: list[object] = []
    starts: list[int] = []
    original_start = store._start

    def tracking_start() -> None:
        starts.append(1)
        original_start()
        created.append(store._process)

    store._start = tracking_start
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    with using_request_deadline(RequestDeadline.from_budget(0.12)):
        with pytest.raises(RuntimeError, match="SQLite truth is intact"):
            store.count_rows()
    return store, created, starts


def test_timeout_slow_cleanup_explicit_close_joins_owned_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store

    real_stop = process_store._stop_worker
    reap_started = threading.Event()
    release = threading.Event()

    def slow_stop(process: object) -> None:
        reap_started.set()
        release.wait(timeout=5)
        real_stop(process)

    store, created, _starts = _timeout_owned_helper(
        tmp_path, monkeypatch, process_store, stop=slow_stop
    )
    try:
        assert reap_started.wait(timeout=2)
        assert created
        helper = created[0]
        assert helper.poll() is None

        def unblock() -> None:
            time.sleep(0.3)
            release.set()

        threading.Thread(target=unblock, name="reaper-release", daemon=True).start()
        started = time.monotonic()
        store.close()
        elapsed = time.monotonic() - started
        assert helper.poll() is not None
        assert elapsed >= 0.2
        _wait_lance_helper_drain()
    finally:
        release.set()


def test_timeout_slow_cleanup_short_budget_open_does_not_start_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store
    from scope_recall._internal.recall.deadline import (
        RequestDeadline,
        using_request_deadline,
    )

    real_stop = process_store._stop_worker
    reap_started = threading.Event()
    release = threading.Event()

    def delayed_stop(process: object) -> None:
        reap_started.set()
        release.wait(timeout=5)
        real_stop(process)

    store, created, starts = _timeout_owned_helper(
        tmp_path, monkeypatch, process_store, stop=delayed_stop
    )
    try:
        assert reap_started.wait(timeout=2)
        assert created
        helper = created[0]
        assert helper.poll() is None
        assert starts == [1]
        with using_request_deadline(RequestDeadline.from_budget(0.05)):
            with pytest.raises(RuntimeError, match="teardown is still pending"):
                store.open()
        assert starts == [1]
        assert store._process is None
        assert helper.poll() is None
    finally:
        release.set()
        try:
            store.close()
        except RuntimeError:
            pass
        if created and created[0].poll() is None:
            real_stop(created[0])
        _wait_lance_helper_drain()


def test_reopen_succeeds_after_owned_reaper_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store

    real_stop = process_store._stop_worker
    reap_started = threading.Event()
    release = threading.Event()

    def delayed_stop(process: object) -> None:
        reap_started.set()
        release.wait(timeout=5)
        real_stop(process)

    store, created, starts = _timeout_owned_helper(
        tmp_path, monkeypatch, process_store, stop=delayed_stop
    )
    try:
        assert reap_started.wait(timeout=2)
        assert created[0].poll() is None
        release.set()
        _wait_lance_helper_drain()
        assert created[0].poll() is not None
        monkeypatch.setattr(process_store, "_stop_worker", real_stop)
        monkeypatch.setattr(process_store, "_worker_command", _good_lance_worker)
        store.open()
        try:
            assert store.count_rows() == 0
            assert starts == [1, 1]
        finally:
            store.close()
            _wait_lance_helper_drain()
    finally:
        release.set()


def test_helper_cleanup_failure_is_visible_not_unobserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.lance_process_store as process_store

    real_stop = process_store._stop_worker
    observed: list[BaseException] = []

    def on_unraisable(args: object) -> None:
        exc = getattr(args, "exc_value", None)
        if isinstance(exc, BaseException):
            observed.append(exc)

    def on_thread_exception(args: object) -> None:
        exc = getattr(args, "exc_value", None)
        if isinstance(exc, BaseException):
            observed.append(exc)

    monkeypatch.setattr(sys, "unraisablehook", on_unraisable)
    monkeypatch.setattr(threading, "excepthook", on_thread_exception)

    def failing_stop(_process: object) -> None:
        raise RuntimeError("injected helper teardown failure")

    store, created, _starts = _timeout_owned_helper(
        tmp_path, monkeypatch, process_store, stop=failing_stop
    )
    try:
        with pytest.raises(RuntimeError, match="teardown failed") as caught:
            store.close()
        assert caught.value.__cause__ is not None
        assert "injected helper teardown failure" in str(caught.value.__cause__)
        time.sleep(0.1)
        assert observed == []
        assert created
        assert created[0].poll() is None
    finally:
        monkeypatch.setattr(process_store, "_stop_worker", real_stop)
        try:
            store.close()
        except RuntimeError:
            pass
        if created and created[0].poll() is None:
            real_stop(created[0])
        _wait_lance_helper_drain()
