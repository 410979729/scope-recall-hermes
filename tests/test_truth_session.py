"""Tranche A: TruthSession owns the published SQLite connection lifetime."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from scope_recall._internal.runtime.storage import configure_published_writer_connection
from scope_recall._internal.runtime.truth_session import TruthSession
from scope_recall.provider import ScopeRecallMemoryProvider


class _Owner:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._db_path = None
        self._shutdown_requested = threading.Event()
        self._truth_writer_role = "owner"
        self._runtime_status = "active"
        self.opened = 0

    def _runtime_memory_disabled(self) -> bool:
        return False

    def _truth_writes_blocked(self) -> bool:
        return False

    def _open_runtime_connection(self) -> sqlite3.Connection:
        self.opened += 1
        conn = sqlite3.connect(":memory:")
        return conn


def test_truth_session_owns_close_and_does_not_leave_handle() -> None:
    session = TruthSession(_Owner())
    conn = sqlite3.connect(":memory:")
    session._conn = conn

    session.close()

    assert session._conn is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_truth_session_recover_rolls_back_and_probes_without_reopen() -> None:
    owner = _Owner()
    session = TruthSession(owner)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO t(id) VALUES (1)")
    assert conn.in_transaction
    session._conn = conn

    payload = session.recover_after_error(
        "unit-test",
        write_probe=lambda item: True,
    )

    assert payload["rolled_back"] is True
    assert payload["recovered"] is True
    assert payload["reopened"] is False
    assert conn.in_transaction is False
    assert session._conn is conn
    assert owner.opened == 0


def test_truth_session_preserves_connection_when_peer_remains_busy() -> None:
    owner = _Owner()
    session = TruthSession(owner)
    conn = sqlite3.connect(":memory:")
    session._conn = conn
    calls = {"peer": 0, "probe": 0}

    def busy_peer(_context: str) -> dict[str, int]:
        calls["peer"] += 1
        return {
            "peer_providers_checked": 1,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 1,
        }

    def failed_probe(item: sqlite3.Connection) -> bool:
        assert item is conn
        calls["probe"] += 1
        return False

    def unexpected_reopen() -> sqlite3.Connection:
        pytest.fail("an unresolved busy peer must not trigger reopen")

    payload = session.recover_after_error(
        "busy-peer-unit-test",
        peer_rollback=busy_peer,
        open_writer=unexpected_reopen,
        write_probe=failed_probe,
    )

    assert calls == {"peer": 2, "probe": 2}
    assert payload["recovered"] is False
    assert payload["reopened"] is False
    assert payload["reconnect_pending"] is False
    assert payload["peer_recovery_passes"] == 2
    assert payload["peer_busy_skipped"] == 1
    assert payload["peer_busy_skipped_total"] == 2
    assert payload["peer_recovery_deferred"] is True
    assert session._conn is conn
    conn.close()


def test_truth_session_clears_deferred_when_second_probe_recovers() -> None:
    owner = _Owner()
    session = TruthSession(owner)
    conn = sqlite3.connect(":memory:")
    session._conn = conn
    calls = {"peer": 0, "probe": 0}

    def busy_peer(_context: str) -> dict[str, int]:
        calls["peer"] += 1
        return {
            "peer_providers_checked": 1,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 0,
            "peer_busy_skipped": 1,
        }

    def eventually_available(item: sqlite3.Connection) -> bool:
        assert item is conn
        calls["probe"] += 1
        return calls["probe"] == 2

    payload = session.recover_after_error(
        "busy-peer-recovers-unit-test",
        peer_rollback=busy_peer,
        open_writer=lambda: pytest.fail("successful second probe must not reopen"),
        write_probe=eventually_available,
    )

    assert calls == {"peer": 2, "probe": 2}
    assert payload["recovered"] is True
    assert payload["peer_busy_skipped"] == 1
    assert payload["peer_recovery_deferred"] is False
    assert session._conn is conn
    conn.close()


def test_truth_session_preserves_connection_after_peer_rollback_error() -> None:
    owner = _Owner()
    session = TruthSession(owner)
    conn = sqlite3.connect(":memory:")
    session._conn = conn
    calls = {"peer": 0, "probe": 0}

    def failed_peer_rollback(_context: str) -> dict[str, int]:
        calls["peer"] += 1
        return {
            "peer_providers_checked": 1,
            "peer_rollbacks": 0,
            "peer_rollback_errors": 1,
            "peer_busy_skipped": 0,
        }

    def failed_probe(item: sqlite3.Connection) -> bool:
        assert item is conn
        calls["probe"] += 1
        return False

    payload = session.recover_after_error(
        "peer-rollback-error-unit-test",
        peer_rollback=failed_peer_rollback,
        open_writer=lambda: pytest.fail("uncertain peer state must not reopen"),
        write_probe=failed_probe,
    )

    assert calls == {"peer": 1, "probe": 1}
    assert payload["recovered"] is False
    assert payload["peer_rollback_errors"] == 1
    assert payload["peer_recovery_passes"] == 1
    assert payload["peer_recovery_deferred"] is True
    assert session._conn is conn
    conn.close()


def test_require_never_changes_join_state_even_if_conn_in_transaction() -> None:
    session = TruthSession(_Owner())
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    session._conn = conn

    required = session.require()

    assert required is conn
    assert session._joined_outer is False
    assert session._join_depth == 0
    assert conn.in_transaction is True


def test_joining_outer_makes_commit_noop_and_restores_after_exit() -> None:
    session = TruthSession(_Owner())
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.commit()
    session._conn = conn
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t(id) VALUES (7)")

    with session.joining_outer():
        assert session._joined_outer is True
        session.require()
        assert session._joined_outer is True
        session.commit()
        assert conn.in_transaction is True
        assert conn.execute("SELECT id FROM t").fetchone()[0] == 7

    assert session._joined_outer is False
    conn.execute("INSERT INTO t(id) VALUES (8)")
    session.commit()
    assert conn.in_transaction is False
    assert [row[0] for row in conn.execute("SELECT id FROM t ORDER BY id")] == [7, 8]


def test_joining_outer_nests_and_exceptions_restore_depth() -> None:
    session = TruthSession(_Owner())
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.commit()
    session._conn = conn
    conn.execute("INSERT INTO t(id) VALUES (1)")

    with session.joining_outer():
        assert session._join_depth == 1
        with session.joining_outer():
            assert session._join_depth == 2
            session.commit()
            assert conn.in_transaction is True
        assert session._join_depth == 1
        session.commit()
        assert conn.in_transaction is True

    assert session._join_depth == 0

    with pytest.raises(RuntimeError, match="boom"):
        with session.joining_outer():
            assert session._join_depth == 1
            raise RuntimeError("boom")

    assert session._join_depth == 0
    session.commit()
    assert conn.in_transaction is False
    assert conn.execute("SELECT id FROM t").fetchone()[0] == 1


def test_truth_session_commit_owns_only_session_started_txn() -> None:
    session = TruthSession(_Owner())
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.commit()
    session._conn = conn
    conn.execute("INSERT INTO t(id) VALUES (3)")
    assert conn.in_transaction
    assert session._joined_outer is False

    session.commit()

    assert conn.in_transaction is False
    assert conn.execute("SELECT id FROM t").fetchone()[0] == 3


def test_provider_conn_property_get_set_and_require_monkeypatch(monkeypatch) -> None:
    provider = ScopeRecallMemoryProvider()
    fake = object()

    provider._conn = fake  # type: ignore[assignment]
    assert provider._conn is fake
    assert provider._truth._conn is fake

    replacement = object()
    monkeypatch.setattr(provider, "_conn", replacement)
    assert provider._conn is replacement
    assert provider._truth._conn is replacement

    sentinel = sqlite3.connect(":memory:")
    monkeypatch.setattr(provider, "_require_conn", lambda: sentinel)
    assert provider._require_conn() is sentinel
    sentinel.close()


def test_provider_conn_property_works_on_new_without_init() -> None:
    provider = ScopeRecallMemoryProvider.__new__(ScopeRecallMemoryProvider)
    assert provider._conn is None
    fake = object()
    provider._conn = fake  # type: ignore[assignment]
    assert provider._conn is fake
    assert provider._truth._conn is fake


def test_provider_new_require_uses_session_owned_lock_fallback() -> None:
    provider = ScopeRecallMemoryProvider.__new__(ScopeRecallMemoryProvider)
    assert getattr(provider, "_lock", None) is None
    opened: list[sqlite3.Connection] = []

    def opener() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        opened.append(conn)
        return conn

    provider._open_runtime_connection = opener  # type: ignore[attr-defined]
    conn = provider._require_conn()
    assert opened == [conn]
    assert provider._truth.lock() is provider._truth._own_lock
    conn.close()


def test_storage_configure_publishes_through_session_not_provider_private() -> None:
    storage_path = Path(
        __file__
    ).resolve().parents[1] / "_internal" / "runtime" / "storage.py"
    source = storage_path.read_text(encoding="utf-8")
    assert "provider._conn =" not in source
    assert "provider._conn=" not in source

    provider = ScopeRecallMemoryProvider()
    opened: list[sqlite3.Connection] = []

    def connect_fn(*_args, **_kwargs):
        conn = sqlite3.connect(":memory:")
        opened.append(conn)
        return conn

    conn = configure_published_writer_connection(
        provider,
        timeout=1.0,
        connect_fn=connect_fn,
        authorizer_fn=lambda *_args, **_kwargs: None,
        schema_fn=lambda *_args, **_kwargs: None,
        journal_fn=lambda *_args, **_kwargs: None,
        ensure_triggers_fn=lambda *_args, **_kwargs: None,
    )

    assert opened == [conn]
    assert provider._truth._conn is conn
    assert provider._conn is conn
    conn.close()
