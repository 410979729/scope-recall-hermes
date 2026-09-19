"""The sqlite companion is published to through the same fenced write as the native store.

The worker's embed path writes every embedding through ``LanceIndexWriter``'s
fenced form, which asks the store for ``fenced_upsert_records``.  Only the
LanceDB driver had it, so on ``sqlite-bruteforce`` -- the documented no-extra
fallback and the automatic one where LanceDB cannot load -- every embed item
failed with a bare ``storage_unavailable`` and the companion stayed empty (#85).
These cases pin the contract the writer relies on: the guard is evaluated under
the store's own lock, an approving guard writes the whole group once, a
refusing guard writes nothing, and a spent deadline or a non-callable guard is
refused before anything is touched.
"""
from __future__ import annotations

import pytest

from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorRecord
from scope_recall.contracts import ContractError
from scope_recall.vector.store import build_vector_store


def _record(ref: str = "event-1", vector: tuple[float, ...] = (0.25, 0.75)) -> LanceVectorRecord:
    return LanceVectorRecord(
        "event", ref, 1, f"TEST:{ref}:1", "TEST-space", vector,
        "TEST-scope", "TEST-agent", "TEST-installation",
    )


@pytest.fixture
def store(tmp_path):
    companion = build_vector_store("sqlite-bruteforce", storage_dir=tmp_path, table_name="TEST_vectors",
                                   dimensions=2, metric="cosine")
    companion.open()
    try:
        yield companion
    finally:
        companion.close()


def test_the_writer_publishes_to_the_sqlite_companion(store):
    writer = LanceIndexWriter(store)
    assert writer.upsert_fenced(_record(), guard=lambda: True, remaining_seconds=1.0) is True
    assert store.list_ids() == ["TEST:event-1:1"]


def test_a_group_lands_as_one_write_under_one_guard_check(store):
    checks: list[bool] = []

    def guard() -> bool:
        checks.append(True)
        return True

    records = [_record("event-1"), _record("event-2", (0.5, 0.5))]
    assert LanceIndexWriter(store).upsert_fenced_many(records, guard=guard, remaining_seconds=1.0) is True
    assert sorted(store.list_ids()) == ["TEST:event-1:1", "TEST:event-2:1"]
    assert checks == [True]


def test_a_refusing_guard_writes_nothing(store):
    assert LanceIndexWriter(store).upsert_fenced(_record(), guard=lambda: False, remaining_seconds=1.0) is False
    assert store.list_ids() == []


def test_the_guard_is_evaluated_under_the_store_lock(store):
    def guard() -> bool:
        # A re-entrant lock: the guard runs with the write already serialized.
        assert store._lock.acquire(blocking=False)
        store._lock.release()
        return True

    row = dict(id="TEST:event-1:1", scope_id="TEST-scope", source="event", target="event-1",
               content="", summary="", updated_at="", vector=[0.25, 0.75])
    assert store.fenced_upsert_records([row], guard=guard, remaining_seconds=1.0) is True
    assert store.list_ids() == ["TEST:event-1:1"]


def test_a_spent_deadline_is_refused_before_anything_is_written(store):
    with pytest.raises(RuntimeError, match="fence deadline exhausted"):
        LanceIndexWriter(store).upsert_fenced(_record(), guard=lambda: True, remaining_seconds=0.0)
    assert store.list_ids() == []


def test_a_guard_that_is_not_callable_is_rejected(store):
    with pytest.raises(TypeError):
        store.fenced_upsert_records([{"id": "TEST:event-1:1", "scope_id": "s", "vector": [0.0, 1.0]}],
                                    guard=None, remaining_seconds=1.0)  # type: ignore[arg-type]
    assert store.list_ids() == []


def test_an_empty_group_is_a_successful_no_op(store):
    assert store.fenced_upsert_records([], guard=lambda: (_ for _ in ()).throw(AssertionError("not asked")),
                                       remaining_seconds=1.0) is True


def test_a_companion_without_the_fenced_write_still_names_the_gap():
    """A store that cannot fence is refused with a field the worker now records."""

    class Unfenced:
        pass

    with pytest.raises(ContractError) as refused:
        LanceIndexWriter(Unfenced()).upsert_fenced(_record(), guard=lambda: True, remaining_seconds=1.0)
    assert (refused.value.code, refused.value.field) == ("STORAGE_UNAVAILABLE", "fenced_upsert_unsupported")
