"""Private-only fakes must stay fail-closed after the public-port cut."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from _scope_recall_public_memory_port import attach_public_truth_ports
from scope_recall.memory_ops import dedupe_memories
from scope_recall.memory_queries import stats_payload


class _PrivateOnlyFake:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn


def test_private_only_fake_still_raises_typeerror_for_public_ports() -> None:
    conn = sqlite3.connect(":memory:")
    fake = _PrivateOnlyFake(conn)

    with pytest.raises(TypeError, match="MemoryCommandPort.query_lock is required"):
        dedupe_memories(fake, dry_run=True, scope_only=False)

    with pytest.raises(TypeError, match="MemoryQueryPort.query_connection is required"):
        stats_payload(fake)

    attached = attach_public_truth_ports(_PrivateOnlyFake(conn))
    assert callable(attached.query_lock)
    assert callable(attached.query_connection)
