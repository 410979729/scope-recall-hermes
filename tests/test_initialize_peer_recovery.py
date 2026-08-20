"""P0-C / issue #43: initialize-time same-process dirty-peer recovery.

On SQLite lock contention only, initialize rolls back registered peers that
share the same canonical database and retries `_open_runtime_connection`
exactly once when that recovery rolled back at least one idle peer and left
no busy or failed peer. Zero peer rollbacks, a remaining busy peer, or a
rollback error stay fail-closed. Non-lock errors do not retry. Unrelated
databases are left alone. A read-only follower must not enter the writable
open/retry path. Concurrent reader promotion must publish exactly one lease.
A later writer-initialization failure must release the partial writer before
the OS lease.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

import scope_recall.provider as provider_module
import writer_lease as writer_lease_module
from writer_lease import TruthWriterLease


def _live_peer_recovery(provider):
    """Resolve the owner module bound to this isolated plugin namespace."""

    module = inspect.getmodule(type(provider))
    assert module is not None
    name = f"{module.__name__.rsplit('.', 1)[0]}._internal.runtime.peer_recovery"
    recovery = sys.modules.get(name)
    if recovery is None:
        recovery = __import__(name, fromlist=["PROVIDER_REGISTRY"])
    return recovery

READ_ONLY_STATUS = "active_read_only"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_PROVIDER = (_REPO_ROOT / "provider.py").resolve()
_RECOVERED_RETRY_WARNING = (
    "Scope Recall initialize recovered from same-process peer "
    "SQLite lock contention; retrying startup once"
)


def _assert_workspace_provider(provider) -> None:
    """Fail closed if this node is exercising a different provider.py."""

    module_file = Path(provider_module.__file__).resolve()
    class_file = Path(inspect.getfile(type(provider))).resolve()
    assert module_file == _WORKSPACE_PROVIDER
    assert class_file == _WORKSPACE_PROVIDER


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    _assert_workspace_provider(provider)
    return provider


def _truth_connection_error_type(provider):
    """Resolve the exception class from the connector this provider actually calls.

    Hermes intentionally loads plugins under an isolated module namespace, so the
    live class is not identical to a second import through ``scope_recall``.
    """

    live_module = sys.modules[type(provider).__module__]
    connector = live_module.connect_truth_database
    error_type = connector.__globals__["TruthDatabaseConnectionError"]
    assert isinstance(error_type, type)
    assert issubclass(error_type, RuntimeError)
    return error_type


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _initialize(provider, hermes_home: Path, session: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="lease-user",
        chat_id="lease-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _child_acquire_status(storage_dir: Path) -> str:
    """Ask a new process whether it can take the writer lease."""

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


@contextlib.contextmanager
def _external_lease_holder(storage_dir: Path, *, role: str = "external-process"):
    storage_dir.mkdir(parents=True, exist_ok=True)
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role={role!r})
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        print("RELEASED", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        yield child
    finally:
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.close()
                assert child.stdout.readline().strip() == "RELEASED"
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)


def test_initialize_retries_open_once_after_peer_rollback(tmp_path, caplog):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    calls = {"open": 0, "rollback": 0}
    real_open = provider._open_runtime_connection

    def flaky_open():
        calls["open"] += 1
        if calls["open"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_open()

    def recovered_rollback(context):
        calls["rollback"] += 1
        assert context == "initialize"
        return {
            "peer_providers_checked": 1,
            "peer_rollbacks": 1,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 0,
        }

    provider._open_runtime_connection = flaky_open
    provider._rollback_peer_provider_transactions = recovered_rollback
    try:
        with caplog.at_level(logging.WARNING):
            _initialize(provider, tmp_path, "retry-once")
        assert provider.runtime_status == "active"
        assert calls["open"] == 2
        assert calls["rollback"] == 1
        assert _RECOVERED_RETRY_WARNING in caplog.text
    finally:
        provider.shutdown()


def test_initialize_does_not_retry_without_peer_rollback(tmp_path, caplog):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    calls = {"open": 0, "rollback": 0}

    def locked_open():
        calls["open"] += 1
        raise sqlite3.OperationalError("database is locked")

    def busy_skipped_rollback(context):
        calls["rollback"] += 1
        assert context == "initialize"
        return {
            "peer_providers_checked": 1,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 1,
        }

    provider._open_runtime_connection = locked_open
    provider._rollback_peer_provider_transactions = busy_skipped_rollback
    with caplog.at_level(logging.WARNING):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _initialize(provider, tmp_path, "no-peer-rollback")
    assert calls["open"] == 1
    assert calls["rollback"] == 1
    assert provider._truth_writer_lease is None
    assert provider._truth_writer_role == "unknown"
    assert _RECOVERED_RETRY_WARNING not in caplog.text


@pytest.mark.parametrize(
    "recovery",
    [
        {
            "peer_providers_checked": 2,
            "peer_rollbacks": 1,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 1,
        },
        {
            "peer_providers_checked": 2,
            "peer_rollbacks": 1,
            "peer_rollback_errors": 1,
            "peer_busy_skipped": 0,
        },
    ],
    ids=["rollback-and-busy", "rollback-and-rollback-error"],
)
def test_initialize_does_not_retry_when_peer_recovery_is_incomplete(
    tmp_path, caplog, monkeypatch, recovery
):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    calls = {"open": 0, "rollback": 0}
    captured: dict[str, object] = {}
    runtime_module = inspect.getmodule(type(provider))
    assert runtime_module is not None
    real_acquire = runtime_module.TruthWriterLease.acquire

    def tracking_acquire(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        captured["lease"] = self
        captured["status"] = result.get("status")
        return result

    def locked_open():
        calls["open"] += 1
        raise sqlite3.OperationalError("database is locked")

    def mixed_rollback(context):
        calls["rollback"] += 1
        assert context == "initialize"
        return dict(recovery)

    monkeypatch.setattr(runtime_module.TruthWriterLease, "acquire", tracking_acquire)
    provider._open_runtime_connection = locked_open
    provider._rollback_peer_provider_transactions = mixed_rollback
    with caplog.at_level(logging.WARNING):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _initialize(provider, tmp_path, "incomplete-peer-recovery")
    assert calls["open"] == 1
    assert calls["rollback"] == 1
    assert captured.get("status") == "acquired"
    lease = captured.get("lease")
    assert isinstance(lease, runtime_module.TruthWriterLease)
    assert lease.acquired is False
    assert provider._truth_writer_lease is None
    assert provider._truth_writer_role == "unknown"
    assert _RECOVERED_RETRY_WARNING not in caplog.text
    again = TruthWriterLease(tmp_path / "scope-recall", role="post-incomplete-recovery")
    assert again.acquire()["status"] == "acquired"
    again.release()


def test_failed_post_connect_initialization_releases_writer_resources(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    runtime_module = inspect.getmodule(type(provider))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    captured: dict[str, object] = {"conn": None, "schema_calls": 0}
    real_open = provider._open_runtime_connection
    real_schema = runtime_module.ensure_journal_schema

    def capturing_open():
        conn = real_open()
        captured["conn"] = conn
        return conn

    def fail_second_schema(conn, commit=True):
        captured["schema_calls"] = int(captured["schema_calls"]) + 1
        if int(captured["schema_calls"]) >= 2:
            captured["conn_at_failure"] = provider._conn
            raise RuntimeError("injected post-connect schema failure")
        return real_schema(conn, commit=commit)

    provider._open_runtime_connection = capturing_open
    monkeypatch.setattr(runtime_module, "ensure_journal_schema", fail_second_schema)
    with pytest.raises(RuntimeError, match="injected post-connect schema failure"):
        _initialize(provider, tmp_path, "post-connect-fail")

    opened = captured["conn"]
    assert opened is not None
    assert captured["conn_at_failure"] is opened
    assert provider._conn is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened.execute("SELECT 1")
    thread = provider._writer_thread
    assert thread is None or not thread.is_alive()
    assert provider._vector_store is None
    assert provider not in _live_peer_recovery(provider).PROVIDER_REGISTRY
    assert provider._truth_writer_lease is None
    assert provider._truth_writer_role == "unknown"

    lease = TruthWriterLease(tmp_path / "scope-recall", role="post-fail-reacquire")
    assert lease.acquire()["status"] == "acquired"
    lease.release()


def test_initialize_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    calls = {"open": 0, "rollback": 0}

    def failing_open():
        calls["open"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    def counting_rollback(context):
        del context
        calls["rollback"] += 1
        return {
            "peer_providers_checked": 0,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 0,
        }

    provider._open_runtime_connection = failing_open
    provider._rollback_peer_provider_transactions = counting_rollback
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _initialize(provider, tmp_path, "non-lock")
    assert calls["open"] == 1
    assert calls["rollback"] == 0
    assert provider._truth_writer_lease is None


def test_initialize_recovers_real_dirty_same_process_peer(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    monkeypatch.setattr(provider_module, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    owner = _provider()
    peer = _provider()
    try:
        _initialize(owner, tmp_path, "dirty-owner")
        with owner._lock:
            conn = owner._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS initialize_dirty_probe(label TEXT PRIMARY KEY)"
            )
            conn.execute(
                "INSERT INTO initialize_dirty_probe(label) VALUES ('held')"
            )
            assert conn.in_transaction is True
        _initialize(peer, tmp_path, "dirty-peer")
        assert peer.runtime_status == "active"
        assert peer._truth_writer_role == "owner"
        with owner._lock:
            assert owner._require_conn().in_transaction is False
        probe = sqlite3.connect(peer._db_path, timeout=0.2)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()
    finally:
        for item in (peer, owner):
            try:
                item.shutdown()
            except Exception:
                pass


def test_initialize_does_not_rollback_reader_peer_transaction(tmp_path, caplog):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "reader-peer-seed")
    seeder.shutdown()

    reader = _provider()
    writer = _provider()
    calls = {"open": 0, "rollback": 0}
    recovery: dict[str, int] = {}
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "reader-dirty-peer")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            with reader._lock:
                conn = reader._conn
                assert conn is not None
                conn.execute("BEGIN")
                assert conn.in_transaction is True

        real_open = writer._open_runtime_connection
        real_rollback = writer._rollback_peer_provider_transactions

        def locked_open():
            calls["open"] += 1
            if calls["open"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_open()

        def wrapping_rollback(context):
            calls["rollback"] += 1
            result = real_rollback(context)
            recovery.update(result)
            return result

        writer._open_runtime_connection = locked_open
        writer._rollback_peer_provider_transactions = wrapping_rollback
        with caplog.at_level(logging.WARNING):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                _initialize(writer, tmp_path, "writer-sees-reader")
        assert calls["open"] == 1
        assert calls["rollback"] == 1
        assert recovery.get("peer_rollbacks") == 0
        with reader._lock:
            assert reader._conn is not None
            assert reader._conn.in_transaction is True
        assert writer._truth_writer_role == "unknown"
        assert _RECOVERED_RETRY_WARNING not in caplog.text
    finally:
        with reader._lock:
            conn = reader._conn
            if conn is not None:
                try:
                    if bool(getattr(conn, "in_transaction", False)):
                        conn.rollback()
                except Exception:
                    pass
        for item in (writer, reader):
            try:
                item.shutdown()
            except Exception:
                pass


def test_initialize_recovery_ignores_unrelated_database(tmp_path, monkeypatch):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    _write_config(home_a, {"vector": {"enabled": False}})
    _write_config(home_b, {"vector": {"enabled": False}})
    monkeypatch.setattr(provider_module, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    provider_a = _provider()
    provider_b = _provider()
    try:
        _initialize(provider_a, home_a, "unrelated-a")
        with provider_a._lock:
            conn_a = provider_a._require_conn()
            conn_a.execute("BEGIN IMMEDIATE")
            conn_a.execute(
                "CREATE TABLE IF NOT EXISTS unrelated_probe(label TEXT PRIMARY KEY)"
            )
            conn_a.execute("INSERT INTO unrelated_probe(label) VALUES ('held')")
            assert conn_a.in_transaction is True
        _initialize(provider_b, home_b, "unrelated-b")
        assert provider_b.runtime_status == "active"
        with provider_a._lock:
            assert provider_a._require_conn().in_transaction is True
            provider_a._require_conn().rollback()
    finally:
        for item in (provider_b, provider_a):
            try:
                item.shutdown()
            except Exception:
                pass


def test_read_only_follower_does_not_enter_writable_initialize_retry(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "seed-follower")
    seeder.shutdown()

    reader = _provider()
    calls = {"writer": 0, "open": 0, "rollback": 0}
    real_writer = reader._initialize_writer_runtime
    real_open = reader._open_runtime_connection
    real_rollback = reader._rollback_peer_provider_transactions

    def wrapped_writer():
        calls["writer"] += 1
        return real_writer()

    def wrapped_open():
        calls["open"] += 1
        return real_open()

    def wrapped_rollback(context):
        calls["rollback"] += 1
        return real_rollback(context)

    reader._initialize_writer_runtime = wrapped_writer
    reader._open_runtime_connection = wrapped_open
    reader._rollback_peer_provider_transactions = wrapped_rollback
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "follower-session")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            assert calls["writer"] == 0
            assert calls["open"] == 0
            assert calls["rollback"] == 0
            search = json.loads(
                reader.handle_tool_call(
                    "scope_recall_search",
                    {"query": "seed follower"},
                )
            )
            assert isinstance(search, dict)
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


class _CloseFails:
    """Stand-in that keeps the real writer connection open when close is refused."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise sqlite3.OperationalError("injected sqlite close failure")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _CloseTracks:
    """Count close() while delegating to the real SQLite connection."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self._inner.close()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_promotion_failure_after_writer_conn_assigned_returns_readonly(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "promote-seed")
    stored = json.loads(
        seeder.handle_tool_call(
            "scope_recall_store",
            {
                "content": "Promotion cleanup must return this Aurora runbook to recall.",
                "target": "ops",
            },
        )
    )
    assert stored.get("id")
    seeder.shutdown()

    reader = _provider()
    runtime_module = inspect.getmodule(type(reader))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    captured: dict[str, object] = {}

    def fail_start_writer(provider_arg):
        captured["conn"] = provider_arg._conn
        captured["lease"] = provider_arg._truth_writer_lease
        raise RuntimeError("injected promotion start_writer failure")

    monkeypatch.setattr(runtime_module, "start_writer", fail_start_writer)
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "promote-reader")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
        reader.on_turn_start(2, "promote after external writer exit")

        opened = captured["conn"]
        assert opened is not None
        assert opened is not reader._conn
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened.execute("SELECT 1")
        thread = reader._writer_thread
        assert thread is None or not thread.is_alive()
        assert reader._vector_store is None
        assert reader._truth_writer_lease is None
        assert reader._truth_writer_role == "reader"
        assert reader.runtime_status == READ_ONLY_STATUS

        probe = TruthWriterLease(storage, role="post-promote-fail")
        assert probe.acquire()["status"] == "acquired"
        probe.release()

        search = json.loads(
            reader.handle_tool_call(
                "scope_recall_search",
                {"query": "Aurora runbook"},
            )
        )
        assert search.get("count", 0) >= 1
        blocked = json.loads(
            reader.handle_tool_call(
                "scope_recall_store",
                {"content": "must not store after failed promotion", "target": "ops"},
            )
        )
        assert "truth_writer_busy" in str(blocked.get("error") or "")
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


def test_promotion_incomplete_cleanup_stays_fail_closed(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "promote-incomplete-seed")
    seeder.shutdown()

    reader = _provider()
    runtime_module = inspect.getmodule(type(reader))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    captured: dict[str, object] = {}

    def fail_start_writer(provider_arg):
        real_conn = provider_arg._conn
        captured["real_conn"] = real_conn
        wrapped = _CloseFails(real_conn)
        captured["wrapped"] = wrapped
        provider_arg._conn = wrapped
        captured["lease"] = provider_arg._truth_writer_lease
        raise RuntimeError("injected promotion close-failure path")

    monkeypatch.setattr(runtime_module, "start_writer", fail_start_writer)
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "promote-incomplete-reader")
            assert reader._truth_writer_role == "reader"
        reader.on_turn_start(3, "promote with incomplete cleanup")

        wrapped = captured["wrapped"]
        assert wrapped is reader._conn
        assert wrapped.close_calls >= 1
        assert reader._truth_writer_role != "owner"
        assert reader._truth_writer_role == "unknown"
        lease = reader._truth_writer_lease
        assert lease is not None
        assert lease.acquired is True
        assert reader.runtime_status != "active"
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        real_conn = captured.get("real_conn")
        if isinstance(reader._conn, _CloseFails):
            reader._conn = real_conn
        try:
            reader.shutdown()
        except Exception:
            pass
        if real_conn is not None:
            try:
                real_conn.close()
            except Exception:
                pass


def test_promotion_reader_close_failure_retains_acquired_lease(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "promote-reader-close-seed")
    seeder.shutdown()

    reader = _provider()
    runtime_module = inspect.getmodule(type(reader))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    captured: dict[str, object] = {"lease": None, "real_conn": None, "writer_calls": 0}
    real_acquire = runtime_module.TruthWriterLease.acquire

    def tracking_acquire(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        if result.get("status") == "acquired":
            captured["lease"] = self
        return result

    def forbidden_writer():
        captured["writer_calls"] = int(captured["writer_calls"]) + 1
        raise AssertionError(
            "writer runtime must not start after reader close failure"
        )

    monkeypatch.setattr(runtime_module.TruthWriterLease, "acquire", tracking_acquire)
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "promote-reader-close-fail")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            assert reader._conn is not None
            real_conn = reader._conn
            captured["real_conn"] = real_conn
            wrapped = _CloseFails(real_conn)
            captured["wrapped"] = wrapped
            reader._conn = wrapped
        reader._initialize_writer_runtime = forbidden_writer
        reader.on_turn_start(4, "promote after reader close will fail")

        assert int(captured["writer_calls"]) == 0
        wrapped = captured["wrapped"]
        assert isinstance(wrapped, _CloseFails)
        assert wrapped.close_calls >= 1
        lease = reader._truth_writer_lease
        assert lease is not None
        assert lease is captured["lease"]
        assert lease.acquired is True
        assert reader._truth_writer_role != "owner"
        assert reader._truth_writer_role == "unknown"
        assert reader.runtime_status != "active"
        blocked = json.loads(
            reader.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "must not store after reader close failure",
                    "target": "ops",
                },
            )
        )
        assert "truth_writer_busy" in str(blocked.get("error") or "")
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        real_conn = captured.get("real_conn")
        if isinstance(reader._conn, _CloseFails):
            reader._conn = real_conn if isinstance(real_conn, sqlite3.Connection) else None
        try:
            reader.shutdown()
        except Exception:
            pass
        lease = reader._truth_writer_lease or captured.get("lease")
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass
        if isinstance(real_conn, sqlite3.Connection):
            try:
                real_conn.close()
            except Exception:
                pass


def test_concurrent_reader_promotion_publishes_one_lease(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "promote-race-seed")
    seeder.shutdown()

    reader = _provider()
    runtime_module = inspect.getmodule(type(reader))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    constructed: list[object] = []
    acquire_calls: list[object] = []
    init_calls = {"count": 0}
    first_in_acquire = threading.Event()
    allow_first_acquire = threading.Event()
    errors: list[BaseException] = []
    wrapped: _CloseTracks | None = None
    baseline = len(writer_lease_module._PROCESS_REGISTRY)

    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "promote-race-reader")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            assert reader._conn is not None
            wrapped = _CloseTracks(reader._conn)
            reader._conn = wrapped
            real_writer = reader._initialize_writer_runtime

            def tracking_writer():
                init_calls["count"] += 1
                return real_writer()

            reader._initialize_writer_runtime = tracking_writer

        real_init = runtime_module.TruthWriterLease.__init__
        real_acquire = runtime_module.TruthWriterLease.acquire

        def tracking_init(self, *args, **kwargs):
            constructed.append(self)
            return real_init(self, *args, **kwargs)

        def gated_acquire(self, *args, **kwargs):
            acquire_calls.append(self)
            if len(acquire_calls) == 1:
                first_in_acquire.set()
                assert allow_first_acquire.wait(timeout=5.0)
            return real_acquire(self, *args, **kwargs)

        monkeypatch.setattr(runtime_module.TruthWriterLease, "__init__", tracking_init)
        monkeypatch.setattr(runtime_module.TruthWriterLease, "acquire", gated_acquire)

        def worker() -> None:
            try:
                reader._maybe_promote_to_writer()
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=worker, name="promote-first")
        second = threading.Thread(target=worker, name="promote-second")
        first.start()
        assert first_in_acquire.wait(timeout=5.0)
        second.start()
        second.join(timeout=0.3)
        assert second.is_alive()
        allow_first_acquire.set()
        first.join(timeout=10.0)
        second.join(timeout=10.0)
        assert not first.is_alive()
        assert not second.is_alive()
        if errors:
            raise errors[0]

        provider_leases = [
            lease for lease in constructed if getattr(lease, "_role", "") == "provider"
        ]
        connection_leases = [
            lease
            for lease in constructed
            if getattr(lease, "_role", "") == "truth_connection"
        ]
        provider_acquires = [
            lease for lease in acquire_calls if getattr(lease, "_role", "") == "provider"
        ]
        connection_acquires = [
            lease
            for lease in acquire_calls
            if getattr(lease, "_role", "") == "truth_connection"
        ]
        # One serialized promotion owns one provider lease. Opening its writable
        # pager also creates exactly one connection-level pin; that pin is a
        # safety reference, not a second promotion or a second OS lock.
        assert len(provider_leases) == 1
        assert len(provider_acquires) == 1
        assert len(connection_leases) == 1
        assert len(connection_acquires) == 1
        assert wrapped is not None
        assert wrapped.close_calls == 1
        assert init_calls["count"] == 1
        assert reader._truth_writer_role == "owner"
        published = reader._truth_writer_lease
        assert published is provider_leases[0]
        assert published is not None
        assert published.acquired is True
        state = writer_lease_module._PROCESS_REGISTRY.get(published._registry_key)
        assert state is not None
        assert state.holders == 1
        assert state.connection_pins == 1
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
    finally:
        allow_first_acquire.set()
        try:
            reader.shutdown()
        except Exception:
            pass
        for lease in constructed:
            try:
                lease.release()
            except Exception:
                pass
        if wrapped is not None:
            try:
                wrapped._inner.close()
            except Exception:
                pass
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline


def test_reader_does_not_promote_when_shutdown_requested(tmp_path, monkeypatch):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "promote-shutdown-seed")
    seeder.shutdown()

    reader = _provider()
    runtime_module = inspect.getmodule(type(reader))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    constructed: list[object] = []
    acquire_calls: list[object] = []

    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "promote-shutdown-reader")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"

        real_init = runtime_module.TruthWriterLease.__init__
        real_acquire = runtime_module.TruthWriterLease.acquire

        def tracking_init(self, *args, **kwargs):
            constructed.append(self)
            return real_init(self, *args, **kwargs)

        def tracking_acquire(self, *args, **kwargs):
            acquire_calls.append(self)
            return real_acquire(self, *args, **kwargs)

        monkeypatch.setattr(runtime_module.TruthWriterLease, "__init__", tracking_init)
        monkeypatch.setattr(runtime_module.TruthWriterLease, "acquire", tracking_acquire)
        reader._shutdown_requested.set()
        reader._maybe_promote_to_writer()

        assert reader._truth_writer_role == "reader"
        assert reader._shutdown_requested.is_set()
        assert constructed == []
        assert acquire_calls == []
        assert reader._truth_writer_lease is None
        assert reader.runtime_status == READ_ONLY_STATUS
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


def _alias_storage_directory(real_storage: Path, alias_storage: Path) -> Path:
    """Create a same-directory alias for the live truth storage path.

    Windows junctions are the supported same-file alias. POSIX directory
    symlinks are rejected by ``truth_connection._harden_mutable_truth_path``
    and exist here only so tests can prove that fail-closed contract.
    """

    alias_storage.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias_storage), str(real_storage)],
            check=False,
            capture_output=True,
        )
        if created.returncode != 0 or not alias_storage.exists():
            detail = (created.stderr or created.stdout or b"").decode(
                "utf-8", errors="replace"
            )
            pytest.skip(f"could not create junction alias: {detail}")
        return alias_storage
    alias_storage.symlink_to(real_storage, target_is_directory=True)
    return alias_storage


def test_initialize_skips_shutdown_requested_owner_peer(tmp_path, caplog):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    owner = _provider()
    writer = _provider()
    calls = {"open": 0, "rollback": 0}
    recovery: dict[str, int] = {}
    try:
        _initialize(owner, tmp_path, "shutdown-owner")
        with owner._lock:
            conn = owner._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS shutdown_peer_probe(label TEXT PRIMARY KEY)"
            )
            conn.execute("INSERT INTO shutdown_peer_probe(label) VALUES ('held')")
            assert conn.in_transaction is True
        owner._shutdown_requested.set()

        real_open = writer._open_runtime_connection
        real_rollback = writer._rollback_peer_provider_transactions

        def locked_open():
            calls["open"] += 1
            if calls["open"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_open()

        def wrapping_rollback(context):
            calls["rollback"] += 1
            result = real_rollback(context)
            recovery.update(result)
            return result

        writer._open_runtime_connection = locked_open
        writer._rollback_peer_provider_transactions = wrapping_rollback
        with caplog.at_level(logging.WARNING):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                _initialize(writer, tmp_path, "writer-sees-shutdown-owner")
        assert calls["open"] == 1
        assert calls["rollback"] == 1
        assert recovery.get("peer_rollbacks") == 0
        with owner._lock:
            assert owner._require_conn().in_transaction is True
        assert writer._truth_writer_role == "unknown"
        assert _RECOVERED_RETRY_WARNING not in caplog.text
    finally:
        with owner._lock:
            conn = owner._conn
            if conn is not None:
                try:
                    if bool(getattr(conn, "in_transaction", False)):
                        conn.rollback()
                except Exception:
                    pass
        for item in (writer, owner):
            try:
                item.shutdown()
            except Exception:
                pass


def test_shutdown_requested_peer_is_excluded_before_lock_probe(tmp_path):
    db_path = tmp_path / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    db_path.touch()
    shutdown = threading.Event()
    shutdown.set()

    class NeverAcquireLock:
        def __init__(self) -> None:
            self.acquire_calls = 0

        def acquire(self, *args, **kwargs) -> bool:
            self.acquire_calls += 1
            raise AssertionError("shutdown peer lock must not be probed")

        def release(self) -> None:
            raise AssertionError("unacquired shutdown peer lock must not be released")

    class ShutdownPeer:
        def __init__(self) -> None:
            self._db_path = db_path
            self._shutdown_requested = shutdown
            self._truth_writer_role = "owner"
            self._lock = NeverAcquireLock()
            self._conn = None

    writer = _provider()
    writer._db_path = db_path
    runtime_module = inspect.getmodule(type(writer))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    peer = ShutdownPeer()
    recovery = _live_peer_recovery(writer)
    with recovery.PROVIDER_REGISTRY_LOCK:
        recovery.PROVIDER_REGISTRY.add(peer)
        assert peer in recovery.PROVIDER_REGISTRY
    assert recovery.same_truth_database_path(peer._db_path, writer._db_path)
    try:
        result = writer._rollback_peer_provider_transactions("pre-lock-shutdown")
        assert result == {
            "peer_providers_checked": 0,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 0,
        }
        assert peer._lock.acquire_calls == 0
    finally:
        with recovery.PROVIDER_REGISTRY_LOCK:
            recovery.PROVIDER_REGISTRY.discard(peer)


def test_concurrent_initialize_publishes_one_runtime_and_does_not_leak_lease(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    runtime_module = inspect.getmodule(type(provider))
    assert runtime_module is not None
    assert Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    results: list[str] = []
    errors: list[BaseException] = []
    gate = threading.Lock()
    started = threading.Barrier(2)

    def worker(session: str) -> None:
        started.wait()
        try:
            _initialize(provider, tmp_path, session)
            with gate:
                results.append("ok")
        except BaseException as exc:
            with gate:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"concurrent-init-{index}",))
        for index in range(2)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15.0)
            assert not thread.is_alive()
        assert results == ["ok"]
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert provider.runtime_status == "active"
        assert provider._truth_writer_role == "owner"
        published = provider._truth_writer_lease
        assert published is not None
        assert published.acquired is True
        state = writer_lease_module._PROCESS_REGISTRY.get(published._registry_key)
        assert state is not None
        assert state.holders == 1
        assert state.connection_pins == 1
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1
        assert provider in _live_peer_recovery(provider).PROVIDER_REGISTRY
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
        assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
        assert provider not in _live_peer_recovery(provider).PROVIDER_REGISTRY


@pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "Windows junctions are the supported same-file alias for initialize "
        "peer recovery; POSIX directory symlinks are rejected by "
        "truth_connection._harden_mutable_truth_path"
    ),
)
def test_initialize_recovers_same_file_alias_peer(tmp_path, monkeypatch):
    real_home = tmp_path / "real-home"
    alias_home = tmp_path / "alias-home"
    _write_config(real_home, {"vector": {"enabled": False}})
    owner = _provider()
    peer = _provider()
    runtime_module = inspect.getmodule(type(owner))
    assert runtime_module is inspect.getmodule(type(peer))
    assert runtime_module is not None
    monkeypatch.setattr(runtime_module, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    try:
        _initialize(owner, real_home, "alias-owner")
        real_storage = real_home / "scope-recall"
        alias_storage = _alias_storage_directory(
            real_storage, alias_home / "scope-recall"
        )
        real_db = real_storage / "memory.sqlite3"
        alias_db = alias_storage / "memory.sqlite3"
        assert Path(real_db) != Path(alias_db)
        assert os.path.samefile(real_db, alias_db)
        assert not alias_storage.is_symlink()
        with owner._lock:
            conn = owner._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS alias_peer_probe(label TEXT PRIMARY KEY)"
            )
            conn.execute("INSERT INTO alias_peer_probe(label) VALUES ('held')")
            assert conn.in_transaction is True
        _initialize(peer, alias_home, "alias-peer")
        assert peer.runtime_status == "active"
        assert peer._truth_writer_role == "owner"
        assert Path(peer._db_path) != Path(owner._db_path)
        assert os.path.samefile(peer._db_path, owner._db_path)
        with owner._lock:
            assert owner._require_conn().in_transaction is False
    finally:
        for item in (peer, owner):
            try:
                item.shutdown()
            except Exception:
                pass


def test_truth_connection_error_type_follows_live_provider_namespace():
    """Exception identity must follow the isolated plugin module under test."""

    provider = _provider()
    try:
        live_module = sys.modules[type(provider).__module__]
        error_type = _truth_connection_error_type(provider)
        assert error_type is live_module.connect_truth_database.__globals__[
            "TruthDatabaseConnectionError"
        ]
        assert error_type.__name__ == "TruthDatabaseConnectionError"
    finally:
        provider.shutdown()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX directory-symlink fail-closed contract",
)
def test_initialize_rejects_posix_directory_symlink_storage_alias(tmp_path):
    """Parent-directory symlinks are not a supported same-file alias on POSIX."""

    real_home = tmp_path / "real-home"
    alias_home = tmp_path / "alias-home"
    _write_config(real_home, {"vector": {"enabled": False}})
    real_storage = real_home / "scope-recall"
    alias_storage = _alias_storage_directory(
        real_storage, alias_home / "scope-recall"
    )
    alias_db = alias_storage / "memory.sqlite3"
    assert alias_storage.is_symlink()
    assert alias_db.parent.is_symlink()

    peer = _provider()
    owner = _provider()
    try:
        with pytest.raises(
            _truth_connection_error_type(peer),
            match="SQLite truth storage cannot use symlink paths",
        ):
            _initialize(peer, alias_home, "alias-peer")
        assert peer.runtime_status != "active"
        assert peer._truth_writer_role == "unknown"
        assert peer._truth_writer_lease is None

        _initialize(owner, real_home, "real-owner")
        assert owner.runtime_status == "active"
        assert owner._truth_writer_role == "owner"
    finally:
        for item in (owner, peer):
            try:
                item.shutdown()
            except Exception:
                pass


class _PeerTouchTracker:
    """Count rollback/close/inspect touches on a retained peer pager."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner
        self.touches: list[str] = []

    def rollback(self) -> None:
        self.touches.append("rollback")
        self.inner.rollback()

    def close(self) -> None:
        self.touches.append("close")
        self.inner.close()

    @property
    def in_transaction(self) -> bool:
        self.touches.append("in_transaction")
        return bool(self.inner.in_transaction)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_peer_rollback_skips_when_peer_lifecycle_held(tmp_path):
    """Busy peer lifecycle must skip rollback without DB/lifecycle inversion."""

    _write_config(tmp_path, {"vector": {"enabled": False}})
    writer = _provider()
    peer = _provider()
    held = threading.Event()
    recovery_done = threading.Event()
    holder_acquired_db = threading.Event()
    release_holder = threading.Event()
    recovery_report: dict[str, int] = {}
    holder: threading.Thread | None = None
    recovery: threading.Thread | None = None
    tracker: _PeerTouchTracker | None = None
    try:
        _initialize(writer, tmp_path, "lifecycle-busy-writer")
        _initialize(peer, tmp_path, "lifecycle-busy-peer")
        writer._config["relation_extraction_enabled"] = False
        peer._config["relation_extraction_enabled"] = False
        writer._maintenance_stop.set()
        peer._maintenance_stop.set()
        assert writer._truth_writer_role == "owner"
        assert peer._truth_writer_role == "owner"
        with peer._lock:
            conn = peer._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS peer_lifecycle_probe(label TEXT PRIMARY KEY)"
            )
            conn.execute("INSERT INTO peer_lifecycle_probe(label) VALUES ('held')")
            assert conn.in_transaction is True
            tracker = _PeerTouchTracker(conn)
            peer._conn = tracker

        def hold_peer_lifecycle() -> None:
            with peer._writer_lifecycle_lock:
                held.set()
                assert recovery_done.wait(timeout=5.0)
                acquired = peer._lock.acquire(timeout=2.0)
                if acquired:
                    holder_acquired_db.set()
                    peer._lock.release()
                assert release_holder.wait(timeout=5.0)

        def run_peer_recovery() -> None:
            assert held.wait(timeout=2.0)
            recovery_report.update(
                writer._rollback_peer_provider_transactions("peer lifecycle busy")
            )
            recovery_done.set()

        holder = threading.Thread(target=hold_peer_lifecycle, name="peer-lifecycle-holder")
        holder.start()
        assert held.wait(timeout=2.0)
        recovery = threading.Thread(target=run_peer_recovery, name="peer-lifecycle-recovery")
        recovery.start()
        assert recovery_done.wait(timeout=2.0), "peer recovery deadlocked on lifecycle/DB"
        assert recovery_report["peer_providers_checked"] >= 1
        assert recovery_report["peer_busy_skipped"] == 1
        assert recovery_report["peer_rollbacks"] == 0
        assert recovery_report["peer_rollback_errors"] == 0
        assert tracker is not None
        assert tracker.touches == []
        assert holder_acquired_db.wait(timeout=2.0)
        acquired = peer._writer_lifecycle_lock.acquire(blocking=False)
        if acquired:
            peer._writer_lifecycle_lock.release()
        assert acquired is False
        assert tracker.inner.in_transaction is True

        release_holder.set()
        holder.join(timeout=5.0)
        recovery.join(timeout=5.0)
        assert holder.is_alive() is False
        assert recovery.is_alive() is False

        after = writer._rollback_peer_provider_transactions("peer lifecycle released")
        assert after["peer_providers_checked"] >= 1
        assert after["peer_rollbacks"] == 1
        assert after["peer_busy_skipped"] == 0
        assert after["peer_rollback_errors"] == 0
        assert "rollback" in tracker.touches
        assert tracker.inner.in_transaction is False
    finally:
        release_holder.set()
        recovery_done.set()
        if holder is not None and holder.is_alive():
            holder.join(timeout=2.0)
        if recovery is not None and recovery.is_alive():
            recovery.join(timeout=2.0)
        if tracker is not None:
            peer._conn = tracker.inner
            try:
                if bool(getattr(tracker.inner, "in_transaction", False)):
                    tracker.inner.rollback()
            except Exception:
                pass
        for item in (writer, peer):
            try:
                item.shutdown()
            except Exception:
                pass
