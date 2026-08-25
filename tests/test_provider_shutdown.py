"""Provider shutdown contracts for background journal maintenance.

Shutdown must quiesce new work and keep shared resources alive when a worker
cannot acknowledge the stop request within the caller's deadline.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

import scope_recall.capture as capture
import scope_recall.provider as provider_module
from scope_recall._internal.runtime import peer_recovery as peer_recovery_mod
from scope_recall.models import RuntimeScope
from scope_recall.provider import ScopeRecallMemoryProvider
from writer_lease import TruthWriterLease

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BlockingClosable:
    def __init__(self) -> None:
        self.close_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def close(self) -> None:
        self.close_calls += 1
        self.entered.set()
        self.release.wait(timeout=2.0)


def _provider(tmp_path) -> ScopeRecallMemoryProvider:
    provider = ScopeRecallMemoryProvider()
    provider._hermes_home = tmp_path
    provider._scope = RuntimeScope(agent_context="primary")
    provider._config = {
        "journal": {
            "enabled": True,
            "background_digest_enabled": True,
            "background_digest_synchronous": True,
            "digest_interval_hours": 1,
            "extractor": "heuristic",
        }
    }
    # Keep the regression behavioral against pre-fix implementations that do
    # not yet own this event.
    provider._shutdown_requested = threading.Event()
    # Digest and other durable write surfaces fail closed unless this runtime
    # is the truth owner. Shutdown tests exercise the writer-capable path.
    provider._truth_writer_role = "owner"
    return provider


def test_shutdown_request_blocks_new_background_digest(tmp_path, monkeypatch) -> None:
    provider = _provider(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        provider_module,
        "run_journal_digest",
        lambda **_kwargs: calls.append("digest") or {"ok": True},
    )

    provider._shutdown_requested.set()
    provider._maybe_start_background_journal_digest()

    assert calls == []
    assert provider._last_journal_digest_status == "never_run"


def test_synchronous_digest_is_registered_as_in_flight(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    started = threading.Event()
    released = threading.Event()

    def blocked_run(**_kwargs):
        started.set()
        released.wait(timeout=2.0)
        return {"ok": True}

    monkeypatch.setattr(provider_module, "run_journal_digest", blocked_run)
    worker = threading.Thread(target=provider._maybe_start_background_journal_digest)
    worker.start()
    assert started.wait(timeout=1.0)

    assert provider._journal_digest_thread is worker
    provider._shutdown_requested.set()
    released.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert provider._journal_digest_thread is None


def test_shutdown_timeout_keeps_connections_open_for_safe_retry(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _Closable()
    provider._conn = connection
    provider._vector_store = vector_store
    released = threading.Event()
    started = threading.Event()

    def blocked_digest() -> None:
        started.set()
        released.wait(timeout=2.0)

    worker = threading.Thread(target=blocked_digest, name="blocked-journal-digest")
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    unregister_calls: list[str] = []
    monkeypatch.setattr(provider_module, "shutdown_writer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider,
        "_unregister_provider_instance",
        lambda: unregister_calls.append("unregistered"),
    )

    with pytest.raises(RuntimeError, match="journal digest did not acknowledge"):
        provider.shutdown(timeout=0.01)

    assert connection.closed is False
    assert vector_store.closed is False
    assert provider._conn is connection
    assert provider._vector_store is vector_store
    assert unregister_calls == []

    released.set()
    provider.shutdown(timeout=1.0)
    assert connection.closed is True
    assert vector_store.closed is True
    assert provider._conn is None
    assert provider._vector_store is None
    assert unregister_calls == ["unregistered"]


def test_public_shutdown_lifecycle_lock_wait_obeys_total_deadline(tmp_path) -> None:
    provider = _provider(tmp_path)
    lifecycle_held = threading.Event()
    release_lifecycle = threading.Event()

    def hold_lifecycle() -> None:
        with provider._writer_lifecycle_lock:
            lifecycle_held.set()
            release_lifecycle.wait(timeout=2.0)

    holder = threading.Thread(target=hold_lifecycle, name="shutdown-lifecycle-holder")
    holder.start()
    assert lifecycle_held.wait(timeout=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="shutdown deadline"):
            provider.shutdown(timeout=0.05)
        assert time.monotonic() - started < 0.25
        assert provider._shutdown_requested.is_set() is False
        assert bool(getattr(provider, "_shutdown_finalized", False)) is False
    finally:
        release_lifecycle.set()
        holder.join(timeout=1.0)

    provider.shutdown(timeout=1.0)
    assert bool(getattr(provider, "_shutdown_finalized", False)) is True


def test_public_shutdown_shares_one_deadline_between_writer_and_digest(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    writer_timeouts: list[float] = []
    digest_timeouts: list[float] = []

    def slow_writer(_provider, *, timeout: float) -> None:
        writer_timeouts.append(timeout)
        time.sleep(0.07)

    def slow_digest(timeout: float) -> None:
        digest_timeouts.append(timeout)
        time.sleep(max(0.0, timeout - 0.02))

    monkeypatch.setattr(provider_module, "shutdown_writer", slow_writer)
    monkeypatch.setattr(provider._background_work(), "join_digest", slow_digest)
    monkeypatch.setattr(
        provider,
        "_cleanup_failed_writer_initialization",
        lambda **_kwargs: True,
    )

    started = time.monotonic()
    provider.shutdown(timeout=0.1)
    elapsed = time.monotonic() - started

    assert len(writer_timeouts) == 1
    assert len(digest_timeouts) == 1
    assert 0.0 <= digest_timeouts[0] < 0.06
    assert elapsed < 0.15
    assert provider._shutdown_finalized is True


def test_public_shutdown_total_deadline_covers_blocking_vector_cleanup(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _BlockingClosable()
    provider._conn = connection
    provider._vector_store = vector_store
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )
    errors: list[BaseException] = []
    finished = threading.Event()

    def run_shutdown() -> None:
        try:
            provider.shutdown(timeout=0.05)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=run_shutdown, name="blocking-vector-shutdown")
    started = time.monotonic()
    caller.start()
    assert vector_store.entered.wait(timeout=1.0)
    try:
        assert finished.wait(timeout=0.25), "public shutdown exceeded its total deadline"
        caller.join(timeout=0.1)
        assert caller.is_alive() is False
        assert time.monotonic() - started < 0.35
        assert len(errors) == 1
        assert "shutdown deadline" in str(errors[0])
        cleanup_worker = provider._shutdown_cleanup_thread
        assert cleanup_worker.is_alive()
        assert bool(getattr(provider, "_shutdown_finalized", False)) is False
        assert provider._vector_store is vector_store
        assert connection.closed is False

        with pytest.raises(RuntimeError, match="shutdown deadline"):
            provider.shutdown(timeout=0.01)
        assert provider._shutdown_cleanup_thread is cleanup_worker
        assert vector_store.close_calls == 1
    finally:
        vector_store.release.set()
        caller.join(timeout=1.0)

    provider.shutdown(timeout=1.0)
    assert provider._shutdown_cleanup_thread is cleanup_worker
    assert vector_store.close_calls == 1
    assert provider._vector_store is None
    assert provider._conn is None
    assert connection.closed is True
    assert provider._shutdown_finalized is True


def test_public_shutdown_total_deadline_covers_provider_lock_cleanup(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    provider._conn = connection
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_provider_lock() -> None:
        with provider._lock:
            lock_held.set()
            release_lock.wait(timeout=2.0)

    holder = threading.Thread(target=hold_provider_lock, name="provider-lock-holder")
    holder.start()
    assert lock_held.wait(timeout=1.0)

    errors: list[BaseException] = []
    finished = threading.Event()

    def run_shutdown() -> None:
        try:
            provider.shutdown(timeout=0.05)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=run_shutdown, name="provider-lock-shutdown")
    started = time.monotonic()
    caller.start()
    try:
        assert finished.wait(timeout=0.25), "public shutdown exceeded its total deadline"
        caller.join(timeout=0.1)
        assert caller.is_alive() is False
        assert time.monotonic() - started < 0.35
        assert len(errors) == 1
        assert "shutdown deadline" in str(errors[0])
        cleanup_worker = provider._shutdown_cleanup_thread
        assert cleanup_worker.is_alive()
        assert bool(getattr(provider, "_shutdown_finalized", False)) is False
        assert provider._conn is connection
        assert connection.closed is False

        with pytest.raises(RuntimeError, match="shutdown deadline"):
            provider.shutdown(timeout=0.01)
        assert provider._shutdown_cleanup_thread is cleanup_worker
    finally:
        release_lock.set()
        holder.join(timeout=1.0)
        caller.join(timeout=1.0)

    provider.shutdown(timeout=1.0)
    assert provider._shutdown_cleanup_thread is cleanup_worker
    assert provider._conn is None
    assert connection.closed is True
    assert provider._shutdown_finalized is True


def test_public_shutdown_retries_completed_cleanup_error(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )
    cleanup_calls: list[str] = []

    def transient_cleanup(**_kwargs) -> bool:
        cleanup_calls.append("cleanup")
        if len(cleanup_calls) == 1:
            raise RuntimeError("injected transient cleanup failure")
        return True

    monkeypatch.setattr(
        provider,
        "_cleanup_failed_writer_initialization",
        transient_cleanup,
    )

    with pytest.raises(RuntimeError, match="transient cleanup failure"):
        provider.shutdown(timeout=1.0)
    first_worker = provider._shutdown_cleanup_thread
    assert first_worker.is_alive() is False
    assert bool(getattr(provider, "_shutdown_finalized", False)) is False

    provider.shutdown(timeout=1.0)

    assert cleanup_calls == ["cleanup", "cleanup"]
    assert provider._shutdown_cleanup_thread is not first_worker
    assert provider._shutdown_cleanup_thread.is_alive() is False
    assert provider._shutdown_finalized is True


def test_shutdown_during_digest_skips_post_digest_promotion(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    started = threading.Event()
    released = threading.Event()
    promotions: list[str] = []

    def blocked_run(**_kwargs):
        started.set()
        released.wait(timeout=2.0)
        return {"ok": True}

    monkeypatch.setattr(provider_module, "run_journal_digest", blocked_run)
    monkeypatch.setattr(
        provider,
        "_maybe_run_auto_experience_promotion",
        lambda *, trigger: promotions.append(trigger),
    )

    worker = threading.Thread(
        target=provider._run_background_journal_digest,
        args=(dict(provider._config["journal"]),),
    )
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    provider._shutdown_requested.set()
    released.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert promotions == []


class _TrackedLease:
    def __init__(self) -> None:
        self.released = False
        self.acquired = True

    def release(self) -> None:
        self.released = True
        self.acquired = False


class _AlwaysFailingClose:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("injected sqlite close failure")


class _FailingVector:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("injected vector close failure")


def test_shutdown_releases_truth_lease_when_vector_close_fails(
    tmp_path, monkeypatch, caplog
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _FailingVector()
    lease = _TrackedLease()
    provider._conn = connection
    provider._vector_store = vector_store
    provider._truth_writer_lease = lease
    provider._truth_writer_role = "owner"
    unregister_calls: list[str] = []
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        provider,
        "_unregister_provider_instance",
        lambda: unregister_calls.append("unregistered"),
    )

    observed: list[str] = []
    with caplog.at_level(logging.ERROR):
        try:
            provider.shutdown(timeout=1.0)
        except Exception as exc:
            observed.append(str(exc))

    assert connection.closed is True
    assert provider._conn is None
    assert lease.released is True
    assert provider._truth_writer_lease is None
    assert provider._truth_writer_role == "unknown"
    assert unregister_calls == ["unregistered"]
    assert provider._vector_store is None
    assert vector_store.close_calls == 1
    combined = " ".join(observed) + " " + caplog.text
    assert "injected vector close failure" in combined


def test_shutdown_raises_when_truth_teardown_incomplete(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    connection = _AlwaysFailingClose()
    lease = _TrackedLease()
    provider._conn = connection
    provider._truth_writer_lease = lease
    provider._truth_writer_role = "owner"
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )

    with pytest.raises(RuntimeError, match="teardown incomplete"):
        provider.shutdown(timeout=1.0)

    assert connection.close_calls >= 1
    assert provider._conn is connection
    assert lease.released is False
    assert lease.acquired is True
    assert provider._truth_writer_lease is lease
    assert provider._truth_writer_role != "owner"
    assert provider._truth_writer_role == "unknown"


def _live_provider(tmp_path) -> ScopeRecallMemoryProvider:
    config = tmp_path / "scope-recall" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    loaded = load_memory_provider("scope-recall")
    assert loaded is not None
    loaded.initialize(
        "shutdown-block",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="shutdown-user",
        chat_id="shutdown-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    return loaded


def _child_acquire_status(storage_dir: Path) -> str:
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role="probe")
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        if result["status"] == "acquired":
            lease.release()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = (child.stdout.readline() or "").strip()
        child.wait(timeout=10)
        return line
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_digest_timeout_blocks_durable_tools_while_retaining_lease(
    tmp_path, monkeypatch
) -> None:
    provider = _live_provider(tmp_path)
    storage = tmp_path / "scope-recall"
    stored = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {
                "content": "The shutdown sentinel remains until writes are allowed again.",
                "target": "ops",
            },
        )
    )
    assert stored.get("id")
    before = provider._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    started = threading.Event()
    released = threading.Event()

    def blocked_digest() -> None:
        started.set()
        released.wait(timeout=5.0)

    worker = threading.Thread(target=blocked_digest, name="blocked-journal-digest")
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)
    monkeypatch.setattr(
        provider_module, "shutdown_writer", lambda *_args, **_kwargs: None
    )
    try:
        with pytest.raises(RuntimeError, match="journal digest did not acknowledge"):
            provider.shutdown(timeout=0.01)
        assert provider._shutdown_requested.is_set()
        assert provider._truth_writer_role == "owner"
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert provider._truth_writes_blocked() is True
        blocked = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "must not store after shutdown requested",
                    "target": "ops",
                },
            )
        )
        assert "truth_writer_busy" in str(blocked.get("error") or "")
        after = provider._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        assert after == before
        search = json.loads(
            provider.handle_tool_call(
                "scope_recall_search",
                {"query": "shutdown sentinel remains"},
            )
        )
        assert search.get("count", 0) >= 1
        assert _child_acquire_status(storage) == "STATUS:busy"
        released.set()
        worker.join(timeout=1.0)
        provider.shutdown(timeout=1.0)
        assert provider._truth_writer_lease is None
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        released.set()
        if worker.is_alive():
            worker.join(timeout=1.0)
        try:
            provider.shutdown(timeout=1.0)
        except Exception:
            pass
        leftover = TruthWriterLease(storage, role="provider")
        if leftover.acquire().get("status") == "acquired":
            leftover.release()


def _count_memories(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def _release_live_provider(provider, tmp_path) -> None:
    try:
        provider.shutdown(timeout=1.0)
    except Exception:
        pass
    leftover = TruthWriterLease(tmp_path / "scope-recall", role="provider")
    if leftover.acquire().get("status") == "acquired":
        leftover.release()


def test_direct_store_after_shutdown_rejects_with_zero_row_delta(tmp_path) -> None:
    provider = _live_provider(tmp_path)
    try:
        conn = provider._conn
        assert conn is not None
        before = _count_memories(conn)
        provider._shutdown_requested.set()
        assert provider._truth_writes_blocked() is True
        with pytest.raises(RuntimeError, match="truth_writer_busy"):
            provider._store_now(
                content="Direct store after shutdown must not persist a memories row.",
                source="tool-store",
                target="ops",
                session_id=provider._session_id,
            )
        assert provider._conn is conn
        assert _count_memories(conn) == before
    finally:
        _release_live_provider(provider, tmp_path)


def test_demoted_retained_pager_rejects_direct_and_queued_store(
    tmp_path, monkeypatch
) -> None:
    import scope_recall.capture as capture

    provider = _live_provider(tmp_path)
    try:
        conn = provider._conn
        assert conn is not None
        before = _count_memories(conn)
        provider._truth_writer_role = "unknown"
        assert provider._truth_writes_blocked() is True
        with pytest.raises(RuntimeError, match="truth_writer_busy"):
            provider._store_now(
                content="Direct store after demotion must not persist a memories row.",
                source="tool-store",
                target="ops",
                session_id=provider._session_id,
            )
        assert provider._conn is conn
        assert _count_memories(conn) == before

        provider._truth_writer_role = "owner"
        store_row_calls: list[str] = []
        store_now_calls: list[str] = []
        first_job_finished = threading.Event()
        second_job_finished = threading.Event()
        writer_globals = provider._writer_thread._target.__globals__
        original_store_row = writer_globals["store_row"]
        original_store_now = writer_globals["store_now"]

        def failing_first_store_row(*args, **kwargs):
            store_row_calls.append("store_row")
            if len(store_row_calls) == 1:
                provider._truth_writer_role = "unknown"
                raise RuntimeError("injected first queued store failure")
            return original_store_row(*args, **kwargs)

        def tracked_store_now(provider_arg, **kwargs):
            store_now_calls.append("store_now")
            try:
                return original_store_now(provider_arg, **kwargs)
            finally:
                if len(store_now_calls) == 1:
                    first_job_finished.set()
                elif len(store_now_calls) >= 2:
                    second_job_finished.set()

        monkeypatch.setitem(writer_globals, "store_row", failing_first_store_row)
        monkeypatch.setitem(writer_globals, "store_now", tracked_store_now)
        monkeypatch.setattr(capture, "store_row", failing_first_store_row)

        provider._write_queue.put(
            {
                "kind": "store",
                "content": "First queued store fails at the rollback boundary and demotes.",
                "source": "test",
                "target": "memory",
                "session_id": provider._session_id,
                "metadata": {},
            }
        )
        assert first_job_finished.wait(timeout=2.0)
        assert provider._truth_writer_role == "unknown"
        assert provider._conn is conn

        provider._write_queue.put(
            {
                "kind": "store",
                "content": "Second queued store must not enter store_row after demotion.",
                "source": "test",
                "target": "memory",
                "session_id": provider._session_id,
                "metadata": {},
            }
        )
        assert second_job_finished.wait(timeout=2.0)
        assert provider.flush(timeout=2.0) is False
        assert store_row_calls == ["store_row"]
        assert provider._conn is conn
        assert _count_memories(conn) == before
    finally:
        provider._truth_writer_role = "owner"
        _release_live_provider(provider, tmp_path)


def test_in_flight_store_commits_before_shutdown_flag(tmp_path, monkeypatch) -> None:
    import scope_recall.capture as capture

    provider = _live_provider(tmp_path)
    try:
        conn = provider._conn
        assert conn is not None
        before = _count_memories(conn)
        entered_store = threading.Event()
        release_store = threading.Event()
        setter_started = threading.Event()
        setter_finished = threading.Event()
        store_now_fn = provider._store_now.__globals__["store_memory_now"].__globals__[
            "store_now"
        ]
        capture_globals = store_now_fn.__globals__
        original_store_row = capture_globals["store_row"]

        def pausing_store_row(*args, **kwargs):
            entered_store.set()
            assert release_store.wait(timeout=2.0)
            return original_store_row(*args, **kwargs)

        monkeypatch.setitem(capture_globals, "store_row", pausing_store_row)
        monkeypatch.setattr(capture, "store_row", pausing_store_row)

        store_errors: list[BaseException] = []
        store_outcomes: list[tuple[str, bool, str]] = []

        def run_store() -> None:
            try:
                store_outcomes.append(
                    provider._store_now(
                        content="In-flight store must commit before shutdown becomes visible.",
                        source="tool-store",
                        target="ops",
                        session_id=provider._session_id,
                    )
                )
            except BaseException as exc:
                store_errors.append(exc)

        def run_shutdown_setter() -> None:
            setter_started.set()
            with provider._writer_lifecycle_lock:
                provider._shutdown_requested.set()
            setter_finished.set()

        store_thread = threading.Thread(target=run_store, name="in-flight-store")
        store_thread.start()
        assert entered_store.wait(timeout=2.0)

        setter = threading.Thread(target=run_shutdown_setter, name="shutdown-setter")
        setter.start()
        assert setter_started.wait(timeout=2.0)
        acquired = provider._writer_lifecycle_lock.acquire(blocking=False)
        if acquired:
            provider._writer_lifecycle_lock.release()
        assert acquired is False
        assert setter_finished.is_set() is False
        assert provider._shutdown_requested.is_set() is False

        release_store.set()
        store_thread.join(timeout=2.0)
        setter.join(timeout=2.0)
        assert store_thread.is_alive() is False
        assert setter.is_alive() is False
        assert store_errors == []
        assert store_outcomes
        assert setter_finished.is_set()
        assert provider._shutdown_requested.is_set()
        assert _count_memories(conn) == before + 1

        with pytest.raises(RuntimeError, match="truth_writer_busy"):
            provider._store_now(
                content="Store after the shutdown flag is set must not add another row.",
                source="tool-store",
                target="ops",
                session_id=provider._session_id,
            )
        assert _count_memories(conn) == before + 1
    finally:
        _release_live_provider(provider, tmp_path)


def test_write_tool_dispatch_holds_lifecycle_through_handler(monkeypatch) -> None:
    from scope_recall.tooling import ScopeRecallToolService

    class _Owner:
        def __init__(self) -> None:
            self._writer_lifecycle_lock = threading.RLock()
            self._shutdown_requested = threading.Event()
            self._truth_writer_role = "owner"

        def _truth_writes_blocked(self) -> bool:
            return (
                self._shutdown_requested.is_set() or self._truth_writer_role != "owner"
            )

    provider = _Owner()
    service = ScopeRecallToolService(provider)
    entered_handler = threading.Event()
    release_handler = threading.Event()
    setter_started = threading.Event()
    setter_finished = threading.Event()

    def blocking_handler(args: dict) -> str:
        del args
        entered_handler.set()
        assert release_handler.wait(timeout=2.0)
        return json.dumps({"stored": True})

    monkeypatch.setattr(service, "_handle_store", blocking_handler)

    def run_handle() -> None:
        service.handle(
            "scope_recall_store",
            {
                "content": "Write dispatch must hold lifecycle through the handler.",
                "target": "ops",
            },
        )

    def run_shutdown_setter() -> None:
        setter_started.set()
        with provider._writer_lifecycle_lock:
            provider._shutdown_requested.set()
        setter_finished.set()

    worker = threading.Thread(target=run_handle, name="write-dispatch")
    worker.start()
    assert entered_handler.wait(timeout=2.0)

    setter = threading.Thread(target=run_shutdown_setter, name="dispatch-shutdown-setter")
    setter.start()
    assert setter_started.wait(timeout=2.0)
    acquired = provider._writer_lifecycle_lock.acquire(blocking=False)
    if acquired:
        provider._writer_lifecycle_lock.release()
    assert acquired is False
    assert setter_finished.is_set() is False
    assert provider._shutdown_requested.is_set() is False

    release_handler.set()
    worker.join(timeout=2.0)
    setter.join(timeout=2.0)
    assert worker.is_alive() is False
    assert setter.is_alive() is False
    assert setter_finished.is_set()
    assert provider._shutdown_requested.is_set()


def test_readonly_search_available_while_write_lifecycle_held(tmp_path) -> None:
    provider = _live_provider(tmp_path)
    try:
        stored = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "The shutdown sentinel remains until writes are allowed again.",
                    "target": "ops",
                },
            )
        )
        assert stored.get("id")
        provider._shutdown_requested.set()
        assert provider._truth_writes_blocked() is True

        held = threading.Event()
        release_holder = threading.Event()
        search_done = threading.Event()
        search_payload: dict = {}

        def hold_write_lifecycle() -> None:
            with provider._writer_lifecycle_lock:
                held.set()
                release_holder.wait(timeout=2.0)

        def run_search() -> None:
            payload = json.loads(
                provider.handle_tool_call(
                    "scope_recall_search",
                    {"query": "shutdown sentinel remains"},
                )
            )
            search_payload.update(payload)
            search_done.set()

        holder = threading.Thread(target=hold_write_lifecycle, name="hold-write-lifecycle")
        holder.start()
        assert held.wait(timeout=2.0)

        searcher = threading.Thread(target=run_search, name="readonly-search")
        searcher.start()
        assert search_done.wait(timeout=2.0)
        assert search_payload.get("count", 0) >= 1
        assert "truth_writer_busy" not in str(search_payload.get("error") or "")

        release_holder.set()
        holder.join(timeout=2.0)
        searcher.join(timeout=2.0)
    finally:
        _release_live_provider(provider, tmp_path)


class _ObservedAcquireRLock:
    """Expose when one named thread actually attempts lifecycle acquisition."""

    def __init__(
        self,
        inner,
        *,
        observed_thread_name: str,
        attempted: threading.Event,
    ) -> None:
        self.inner = inner
        self.observed_thread_name = observed_thread_name
        self.attempted = attempted

    def acquire(self, *args, **kwargs) -> bool:
        if threading.current_thread().name == self.observed_thread_name:
            self.attempted.set()
        return bool(self.inner.acquire(*args, **kwargs))

    def release(self) -> None:
        self.inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.release()


class _CloseFailingProxy:
    """Retain a live file-backed pager while making cleanup close fail."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner

    def close(self) -> None:
        raise OSError("injected close failure")

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class _InspectCloseFailingProxy(_CloseFailingProxy):
    """Fail transaction inspection so rollback demotes through close failure."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        super().__init__(inner)
        self.inspected = threading.Event()

    @property
    def in_transaction(self) -> bool:
        self.inspected.set()
        raise sqlite3.ProgrammingError("injected inspect failure")


def test_authorized_semantic_merge_serializes_recovery_demotion(
    tmp_path, monkeypatch
) -> None:
    """Recovery must not publish unknown until the authorized merge unit releases."""

    provider = _live_provider(tmp_path)
    provider._config["relation_extraction_enabled"] = False
    provider._maintenance_stop.set()
    raw_conn = provider._conn
    assert raw_conn is not None
    proxy = _CloseFailingProxy(raw_conn)
    provider._conn = proxy
    entered_candidate = threading.Event()
    release_candidate = threading.Event()
    recovery_started = threading.Event()
    recovery_lock_attempted = threading.Event()
    recovery_entered = threading.Event()
    recovery_finished = threading.Event()
    store_finished = threading.Event()
    original_lifecycle_lock = provider._writer_lifecycle_lock
    provider._writer_lifecycle_lock = _ObservedAcquireRLock(
        original_lifecycle_lock,
        observed_thread_name="concurrent-recovery",
        attempted=recovery_lock_attempted,
    )
    store_errors: list[str] = []
    recovery_result: dict[str, object] = {}
    merged_content = "Updated after the authorized merge unit finished."
    store_memory_now = provider._store_now.__globals__["store_memory_now"]
    merge_globals = store_memory_now.__globals__
    original_find = merge_globals["find_semantic_merge_candidate"]

    def paused_find(*_args, **_kwargs):
        entered_candidate.set()
        assert release_candidate.wait(timeout=5.0)
        return seed_id, "Original role race memory.", merged_content

    def failed_probe(_conn) -> bool:
        recovery_entered.set()
        return False

    monkeypatch.setitem(merge_globals, "find_semantic_merge_candidate", paused_find)
    monkeypatch.setattr(provider, "_sqlite_write_probe", failed_probe)
    store_thread: threading.Thread | None = None
    recovery_thread: threading.Thread | None = None
    try:
        seed_id, inserted, _outcome = provider._store_now(
            content="Original role race memory.",
            source="tool-store",
            target="ops",
            session_id=provider._session_id,
        )
        assert inserted

        def run_store() -> None:
            try:
                provider._store_now(
                    content="New semantic evidence for role race.",
                    source="tool-store",
                    target="ops",
                    session_id=provider._session_id,
                    semantic_merge=True,
                )
            except Exception as exc:
                store_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                store_finished.set()

        def run_recovery() -> None:
            recovery_started.set()
            try:
                recovery_result.update(
                    provider._recover_sqlite_connection_after_error(
                        "authorized merge demotion race"
                    )
                )
            finally:
                recovery_finished.set()

        store_thread = threading.Thread(target=run_store, name="authorized-merge")
        store_thread.start()
        assert entered_candidate.wait(timeout=3.0)
        acquired = provider._writer_lifecycle_lock.acquire(blocking=False)
        if acquired:
            provider._writer_lifecycle_lock.release()
        assert acquired is False

        recovery_thread = threading.Thread(
            target=run_recovery, name="concurrent-recovery"
        )
        recovery_thread.start()
        assert recovery_started.wait(timeout=2.0)
        assert recovery_lock_attempted.wait(timeout=2.0)
        assert recovery_entered.is_set() is False
        assert recovery_finished.is_set() is False
        assert provider._truth_writer_role == "owner"
        assert provider._conn is proxy
        assert provider._truth_writer_lease is not None

        release_candidate.set()
        store_thread.join(timeout=5.0)
        recovery_thread.join(timeout=5.0)
        assert store_thread.is_alive() is False
        assert recovery_thread.is_alive() is False
        assert store_errors == []
        assert store_finished.is_set()
        assert recovery_entered.is_set()
        assert recovery_finished.is_set()
        row = raw_conn.execute(
            "SELECT content FROM memories WHERE id = ?", (seed_id,)
        ).fetchone()
        assert row is not None
        assert str(row[0]) == merged_content
        assert provider._truth_writer_role == "unknown"
        assert provider._conn is proxy
        assert provider._truth_writer_lease is not None
        assert recovery_result.get("reconnect_pending") is True
    finally:
        merge_globals["find_semantic_merge_candidate"] = original_find
        release_candidate.set()
        if store_thread is not None and store_thread.is_alive():
            store_thread.join(timeout=2.0)
        if recovery_thread is not None and recovery_thread.is_alive():
            recovery_thread.join(timeout=2.0)
        provider._writer_lifecycle_lock = original_lifecycle_lock
        provider._conn = raw_conn
        provider._truth_writer_role = "owner"
        _release_live_provider(provider, tmp_path)


def test_rollback_after_error_close_failure_waits_for_lifecycle(tmp_path) -> None:
    """Concurrent rollback demotion must wait for the same lifecycle RLock."""

    provider = _live_provider(tmp_path)
    provider._maintenance_stop.set()
    raw_conn = provider._conn
    assert raw_conn is not None
    proxy = _InspectCloseFailingProxy(raw_conn)
    provider._conn = proxy
    held = threading.Event()
    release_holder = threading.Event()
    rollback_started = threading.Event()
    rollback_lock_attempted = threading.Event()
    rollback_finished = threading.Event()
    original_lifecycle_lock = provider._writer_lifecycle_lock
    provider._writer_lifecycle_lock = _ObservedAcquireRLock(
        original_lifecycle_lock,
        observed_thread_name="concurrent-rollback",
        attempted=rollback_lock_attempted,
    )
    holder: threading.Thread | None = None
    rollbacker: threading.Thread | None = None

    def hold_lifecycle() -> None:
        with provider._writer_lifecycle_lock:
            held.set()
            assert release_holder.wait(timeout=5.0)

    def run_rollback() -> None:
        rollback_started.set()
        try:
            provider._rollback_conn_after_error("concurrent rollback demotion")
        finally:
            rollback_finished.set()

    try:
        holder = threading.Thread(target=hold_lifecycle, name="lifecycle-holder")
        holder.start()
        assert held.wait(timeout=2.0)
        acquired = provider._writer_lifecycle_lock.acquire(blocking=False)
        if acquired:
            provider._writer_lifecycle_lock.release()
        assert acquired is False

        rollbacker = threading.Thread(target=run_rollback, name="concurrent-rollback")
        rollbacker.start()
        assert rollback_started.wait(timeout=2.0)
        assert rollback_lock_attempted.wait(timeout=2.0)
        assert proxy.inspected.is_set() is False
        assert rollback_finished.is_set() is False
        assert provider._truth_writer_role == "owner"
        assert provider._conn is proxy
        assert provider._truth_writer_lease is not None

        release_holder.set()
        holder.join(timeout=5.0)
        rollbacker.join(timeout=5.0)
        assert holder.is_alive() is False
        assert rollbacker.is_alive() is False
        assert proxy.inspected.is_set()
        assert rollback_finished.is_set()
        assert provider._truth_writer_role == "unknown"
        assert provider._conn is proxy
        assert provider._truth_writer_lease is not None
    finally:
        release_holder.set()
        if holder is not None and holder.is_alive():
            holder.join(timeout=2.0)
        if rollbacker is not None and rollbacker.is_alive():
            rollbacker.join(timeout=2.0)
        provider._writer_lifecycle_lock = original_lifecycle_lock
        provider._conn = raw_conn
        provider._truth_writer_role = "owner"
        _release_live_provider(provider, tmp_path)


def test_shutdown_timeout_keeps_lease_registry_and_finalized_false_for_retry(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _Closable()
    provider._conn = connection
    provider._vector_store = vector_store
    released_lease = {"n": 0}

    class Lease:
        def release(self) -> None:
            released_lease["n"] += 1

    lease = Lease()
    provider._truth_writer_lease = lease
    released = threading.Event()
    started = threading.Event()

    def blocked_digest() -> None:
        started.set()
        released.wait(timeout=2.0)

    worker = threading.Thread(target=blocked_digest, name="blocked-journal-digest")
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    unregister_calls: list[str] = []
    monkeypatch.setattr(provider_module, "shutdown_writer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        peer_recovery_mod,
        "unregister_provider_instance",
        lambda *_args, **_kwargs: unregister_calls.append("unregistered"),
    )

    with pytest.raises(RuntimeError, match="journal digest did not acknowledge"):
        provider.shutdown(timeout=0.01)

    assert connection.closed is False
    assert vector_store.closed is False
    assert provider._conn is connection
    assert provider._vector_store is vector_store
    assert provider._truth_writer_lease is lease
    assert released_lease["n"] == 0
    assert unregister_calls == []
    assert bool(getattr(provider, "_shutdown_finalized", False)) is False

    released.set()
    provider.shutdown(timeout=1.0)
    assert connection.closed is True
    assert vector_store.closed is True
    assert provider._truth_writer_lease is None
    assert released_lease["n"] == 1
    assert unregister_calls == ["unregistered"]
    assert bool(getattr(provider, "_shutdown_finalized", False)) is True


def test_writer_shutdown_barrier_keeps_resources_for_retry(
    tmp_path,
    monkeypatch,
) -> None:
    """Capture-writer non-ack must fail closed so a later shutdown can finish."""

    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _Closable()
    provider._conn = connection
    provider._vector_store = vector_store
    released_lease = {"n": 0}

    class Lease:
        def release(self) -> None:
            released_lease["n"] += 1

    lease = Lease()
    provider._truth_writer_lease = lease
    started = threading.Event()
    released = threading.Event()

    def blocked_drain(_current) -> None:
        started.set()
        released.wait(timeout=2.0)

    monkeypatch.setattr(capture, "_drain_relation_rebuild_debt", blocked_drain)
    unregister_calls: list[str] = []
    monkeypatch.setattr(
        peer_recovery_mod,
        "unregister_provider_instance",
        lambda *_args, **_kwargs: unregister_calls.append("unregistered"),
    )

    capture.start_writer(provider)
    assert started.wait(timeout=1.0)

    with pytest.raises(RuntimeError, match="did not acknowledge"):
        provider.shutdown(timeout=0.05)

    assert connection.closed is False
    assert vector_store.closed is False
    assert provider._conn is connection
    assert provider._vector_store is vector_store
    assert provider._truth_writer_lease is lease
    assert released_lease["n"] == 0
    assert unregister_calls == []
    assert bool(getattr(provider, "_shutdown_finalized", False)) is False
    assert provider._writer_thread is not None
    assert provider._writer_thread.is_alive()

    released.set()
    provider.shutdown(timeout=1.0)
    assert connection.closed is True
    assert vector_store.closed is True
    assert provider._truth_writer_lease is None
    assert released_lease["n"] == 1
    assert unregister_calls == ["unregistered"]
    assert bool(getattr(provider, "_shutdown_finalized", False)) is True
    assert provider._writer_thread is None
