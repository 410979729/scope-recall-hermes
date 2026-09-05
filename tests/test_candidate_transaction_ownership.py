"""Fault-inject the event-candidate SQLite unit without a second transaction framework."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from scope_recall.candidate_extraction import ExtractedCandidate
from scope_recall.candidate_store import store_event_candidates
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_recovery import rollback_if_active


class _SQLiteBoundaryProxy:
    """Forward a live connection while injecting one-shot SQL/commit/rollback faults."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        faults: dict[str, BaseException],
        *,
        sticky: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self._conn = conn
        self._faults = dict(faults)
        self._sticky = set(sticky)

    def _raise_if(self, key: str) -> None:
        error = self._faults.get(key) if key in self._sticky else self._faults.pop(key, None)
        if error is not None:
            raise error

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        kind = _sql_kind(sql)
        if kind:
            self._raise_if(kind)
        return self._conn.execute(sql, parameters)

    def commit(self) -> None:
        self._raise_if("COMMIT")
        self._conn.commit()

    def rollback(self) -> None:
        self._raise_if("ROLLBACK")
        self._conn.rollback()

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _sql_kind(sql: str) -> str:
    text = " ".join(str(sql).split()).upper()
    if text.startswith("BEGIN"):
        return "BEGIN"
    if text.startswith("SAVEPOINT"):
        return "SAVEPOINT"
    if text.startswith("RELEASE"):
        return "RELEASE"
    if text.startswith("ROLLBACK TO"):
        return "ROLLBACK_TO_SAVEPOINT"
    return ""


def _connect(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _close(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error:
        pass
    conn.close()


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )


def _candidate(content: str) -> ExtractedCandidate:
    return ExtractedCandidate(
        target="user",
        content=content,
        memory_type="preference",
        confidence=0.9,
        evidence_refs=["session:txn:turn:1"],
    )


def _store(conn, *, dry_run: bool = False, content: str = "") -> dict[str, Any]:
    body = content or (
        "Transaction-boundary candidate stores durable preference evidence."
    )
    return store_event_candidates(
        conn,
        candidates=[_candidate(body)],
        scope=_scope(),
        scope_id="scope-a",
        session_id="txn",
        dry_run=dry_run,
    )


def _assert_original_error(exc: BaseException, needle: str) -> None:
    assert needle in str(exc)
    assert "no such savepoint" not in str(exc).lower()


def test_begin_failure_preserves_original_error_and_leaves_connection_idle(tmp_path):
    conn = _connect(tmp_path)
    proxy = _SQLiteBoundaryProxy(
        conn,
        {"BEGIN": sqlite3.OperationalError("injected begin failure")},
    )

    with pytest.raises(sqlite3.OperationalError, match="injected begin failure") as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected begin failure")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_savepoint_failure_after_owned_begin_rolls_back_owned_transaction(tmp_path):
    conn = _connect(tmp_path)
    proxy = _SQLiteBoundaryProxy(
        conn,
        {"SAVEPOINT": sqlite3.OperationalError("injected savepoint failure")},
    )

    with pytest.raises(sqlite3.OperationalError, match="injected savepoint failure") as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected savepoint failure")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_savepoint_failure_on_borrowed_transaction_preserves_caller_writes(tmp_path):
    conn = _connect(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO memories (
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content,
            summary, created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES (
            'caller-prior', 'scope-a', 'telegram', 'user-a', 'chat-a', '', '',
            'yuheng', 'hermes', 'prior', 'user', 'user',
            'Caller prior write must survive borrowed savepoint startup failure.',
            'Caller prior write must survive borrowed savepoint startup failure.',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0,
            'prior-key', '{"lifecycle":"promoted"}'
        )
        """
    )
    proxy = _SQLiteBoundaryProxy(
        conn,
        {"SAVEPOINT": sqlite3.OperationalError("injected borrowed savepoint failure")},
    )

    with pytest.raises(
        sqlite3.OperationalError, match="injected borrowed savepoint failure"
    ) as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected borrowed savepoint failure")
    assert conn.in_transaction is True
    assert (
        conn.execute("SELECT COUNT(*) FROM memories WHERE id='caller-prior'").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM memories WHERE source='event-digest'").fetchone()[0]
        == 0
    )
    conn.commit()
    assert (
        conn.execute("SELECT COUNT(*) FROM memories WHERE id='caller-prior'").fetchone()[0]
        == 1
    )
    _close(conn)


def test_sqlite_insert_abort_rolls_back_owned_unit(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        """
        CREATE TEMP TRIGGER fail_candidate_insert
        BEFORE INSERT ON memories
        BEGIN
            SELECT RAISE(ABORT, 'injected memory insert failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected memory insert failure") as caught:
        _store(conn)

    _assert_original_error(caught.value, "injected memory insert failure")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_candidate_insert")
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_sqlite_audit_abort_rolls_back_owned_unit(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        """
        CREATE TEMP TRIGGER fail_candidate_audit
        BEFORE INSERT ON governance_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'injected audit failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure") as caught:
        _store(conn)

    _assert_original_error(caught.value, "injected audit failure")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_candidate_audit")
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_release_failure_preserves_original_error_and_owned_rollback(tmp_path):
    conn = _connect(tmp_path)
    proxy = _SQLiteBoundaryProxy(
        conn,
        {"RELEASE": sqlite3.OperationalError("injected release failure")},
    )

    with pytest.raises(sqlite3.OperationalError, match="injected release failure") as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected release failure")
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_owned_commit_failure_does_not_touch_released_savepoint(tmp_path):
    conn = _connect(tmp_path)
    proxy = _SQLiteBoundaryProxy(
        conn,
        {"COMMIT": sqlite3.OperationalError("injected commit failure")},
    )

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure") as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected commit failure")
    assert caught.value.__cause__ is None or "no such savepoint" not in str(
        caught.value.__cause__
    ).lower()
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert _store(conn)["inserted"] == 1
    _close(conn)


def test_borrowed_insert_failure_does_not_rollback_caller_prior_writes(tmp_path):
    conn = _connect(tmp_path)
    store_row(
        conn,
        memory_id="caller-committed",
        scope_id="scope-a",
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="committed",
        source="user",
        target="user",
        content="Already committed caller row stays outside the borrowed unit.",
        metadata='{"lifecycle":"promoted"}',
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO memories (
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content,
            summary, created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES (
            'caller-open', 'scope-a', 'telegram', 'user-a', 'chat-a', '', '',
            'yuheng', 'hermes', 'open', 'user', 'user',
            'Open caller write must remain after candidate-unit abort.',
            'Open caller write must remain after candidate-unit abort.',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0,
            'open-key', '{"lifecycle":"promoted"}'
        )
        """
    )
    conn.execute(
        """
        CREATE TEMP TRIGGER fail_borrowed_candidate_insert
        BEFORE INSERT ON memories
        WHEN NEW.source = 'event-digest'
        BEGIN
            SELECT RAISE(ABORT, 'injected borrowed insert failure');
        END
        """
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="injected borrowed insert failure"
    ) as caught:
        _store(conn)

    _assert_original_error(caught.value, "injected borrowed insert failure")
    assert conn.in_transaction is True
    ids = {
        str(row["id"])
        for row in conn.execute("SELECT id FROM memories").fetchall()
    }
    assert ids == {"caller-committed", "caller-open"}
    conn.commit()
    assert {
        str(row["id"])
        for row in conn.execute("SELECT id FROM memories").fetchall()
    } == {"caller-committed", "caller-open"}
    _close(conn)


def test_owned_rollback_failure_is_visible_and_connection_stays_unusable(tmp_path):
    conn = _connect(tmp_path)
    proxy = _SQLiteBoundaryProxy(
        conn,
        {
            "COMMIT": sqlite3.OperationalError("injected commit failure"),
            "ROLLBACK": sqlite3.OperationalError("injected rollback failure"),
        },
        sticky={"ROLLBACK"},
    )

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure") as caught:
        _store(proxy)

    _assert_original_error(caught.value, "injected commit failure")
    assert caught.value.__cause__ is not None
    assert "injected rollback failure" in str(caught.value.__cause__)
    assert conn.in_transaction is True
    with pytest.raises(sqlite3.OperationalError, match="injected rollback failure"):
        rollback_if_active(proxy)
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.in_transaction is False
    assert _store(conn)["inserted"] == 1
    _close(conn)
