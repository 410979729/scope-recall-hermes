"""Gate G4 regressions: no network I/O inside truth transactions, and truth
write transactions stay short under concurrent writers (issue #47 root cause).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

from scope_recall.transaction_guard import (
    OpenTransactionAtNetworkBoundaryError,
    TruthTransactionTimer,
    assert_no_open_transaction,
    reset_transaction_duration_stats,
    transaction_duration_stats,
)


def test_guard_rejects_open_transaction():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    assert_no_open_transaction(conn, "test boundary")  # autocommit: fine
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(OpenTransactionAtNetworkBoundaryError, match="test boundary"):
        assert_no_open_transaction(conn, "test boundary")
    conn.rollback()
    assert_no_open_transaction(conn, "test boundary")
    conn.close()


def test_guard_tolerates_missing_connection():
    assert_no_open_transaction(None, "no connection")


def test_transaction_timer_records_max_and_is_idempotent():
    import time

    reset_transaction_duration_stats()
    timer = TruthTransactionTimer("unit context")
    time.sleep(0.005)
    timer.stop()
    timer.stop()
    stats = transaction_duration_stats()
    assert stats["max_context"] == "unit context"
    assert stats["max_ms"] > 0
    assert stats["slow_count"] == 0


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _initialize(provider, hermes_home: Path, session: str, chat: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="soak-user",
        chat_id=chat,
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


def test_concurrent_writers_soak_produces_no_failed_writes(tmp_path):
    """Two same-process writer providers + parallel stores + digest churn.

    The pre-M0/M1 shape produced `database is locked` warnings roughly once
    per idle tick under this load. The invariant now: zero failed writes,
    truth stays consistent, and no write transaction crosses the slow budget.
    """

    reset_transaction_duration_stats()
    _write_config(tmp_path, {"vector": {"enabled": False}})
    first = load_memory_provider("scope-recall")
    second = load_memory_provider("scope-recall")
    assert first is not None and second is not None
    errors: list[str] = []
    try:
        _initialize(first, tmp_path, "soak-a", "chat-a")
        _initialize(second, tmp_path, "soak-b", "chat-b")
        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"

        def store_batch(provider, label: str, count: int) -> None:
            for index in range(count):
                receipt = json.loads(
                    provider.handle_tool_call(
                        "scope_recall_store",
                        {
                            "content": (
                                f"soak {label} row {index}: concurrent write "
                                "path stability check with enough length"
                            ),
                            "target": "ops",
                        },
                    )
                )
                if not receipt.get("id"):
                    errors.append(f"{label}:{index}:{receipt}")

        def capture_batch(provider, label: str, count: int) -> None:
            for index in range(count):
                provider.sync_turn(
                    f"soak journal user line {label} {index} long enough to pass filters",
                    f"soak journal assistant line {label} {index} long enough to pass filters",
                )

        threads = [
            threading.Thread(target=store_batch, args=(first, "p1", 40)),
            threading.Thread(target=store_batch, args=(second, "p2", 40)),
            threading.Thread(target=capture_batch, args=(first, "c1", 30)),
            threading.Thread(target=capture_batch, args=(second, "c2", 30)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, f"store failures under concurrency: {errors[:5]}"

        assert first.flush(timeout=10.0) is True
        assert second.flush(timeout=10.0) is True

        stats = json.loads(first.handle_tool_call("scope_recall_stats", {}))
        writer_stats = stats["background_writer"]
        assert writer_stats["failed_writes"] == 0
        assert stats["write_transactions"]["slow_count"] == 0

        conn = sqlite3.connect(tmp_path / "scope-recall" / "memory.sqlite3")
        try:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            stored = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE content LIKE 'soak p%'"
            ).fetchone()[0]
            assert stored == 80
        finally:
            conn.close()
    finally:
        for provider in (first, second):
            try:
                provider.shutdown()
            except Exception:
                pass
