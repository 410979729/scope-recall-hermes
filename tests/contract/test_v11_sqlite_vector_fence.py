"""The sqlite companion publishes a fenced group exactly like the native store.

The worker's embed path always writes through ``LanceIndexWriter``'s fenced
form, so a companion without ``fenced_upsert_records`` cannot be published to at
all: every embed item fails with the generic ``storage_unavailable`` code and
the index stays empty.  That is the state of the ``sqlite-bruteforce`` backend
-- the documented no-extra fallback, and the automatic fallback when LanceDB
cannot load -- so these cases pin the contract the writer relies on: the guard
is evaluated under the store's own lock, an approving guard writes the whole
group, and a refusing one writes nothing.
"""
from __future__ import annotations

import pytest

from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorRecord
from scope_recall.vector.sqlite_store import SQLiteBruteForceVectorStore


def _record(ref: str = "event-1", vector: tuple[float, ...] = (0.25, 0.75)) -> LanceVectorRecord:
    return LanceVectorRecord(
        "event", ref, 1, f"TEST:{ref}:1", "TEST-space", vector,
        "TEST-scope", "TEST-agent", "TEST-installation",
    )


def _store(tmp_path) -> SQLiteBruteForceVectorStore:
    store = SQLiteBruteForceVectorStore(tmp_path / "vectors.sqlite3",
                                        table_name="TEST_vectors", dimensions=2)
    store.open()
    return store


def test_fenced_upsert_writes_while_the_guard_approves(tmp_path):
    store = _store(tmp_path)
    writer = LanceIndexWriter(store)
    assert writer.upsert_fenced(_record(), guard=lambda: True, remaining_seconds=1.0) is True
    assert store.list_ids() == ["TEST:event-1:1"]


def test_fenced_upsert_writes_nothing_when_the_guard_refuses(tmp_path):
    store = _store(tmp_path)
    writer = LanceIndexWriter(store)
    assert writer.upsert_fenced(_record(), guard=lambda: False, remaining_seconds=1.0) is False
    assert store.list_ids() == []


def test_fenced_group_lands_as_one_write(tmp_path):
    store = _store(tmp_path)
    writer = LanceIndexWriter(store)
    records = [_record("event-1"), _record("event-2", (0.5, 0.5))]
    assert writer.upsert_fenced_many(records, guard=lambda: True, remaining_seconds=1.0) is True
    assert sorted(store.list_ids()) == ["TEST:event-1:1", "TEST:event-2:1"]


def test_fenced_upsert_refuses_a_spent_deadline(tmp_path):
    store = _store(tmp_path)
    writer = LanceIndexWriter(store)
    with pytest.raises(RuntimeError):
        writer.upsert_fenced(_record(), guard=lambda: True, remaining_seconds=0.0)
    assert store.list_ids() == []


def test_fenced_upsert_requires_a_callable_guard(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(TypeError):
        store.fenced_upsert_records([_record()], guard=None, remaining_seconds=1.0)  # type: ignore[arg-type]
    assert store.list_ids() == []
