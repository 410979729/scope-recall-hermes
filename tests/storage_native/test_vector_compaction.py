"""Vector compaction: the policy, and the real LanceDB behaviour it relies on.

The policy half runs anywhere.  The native half needs the approved LanceDB
install, because the properties worth asserting -- that fragments actually
collapse, that no row is lost, that a reader follows the table forward -- are
properties of LanceDB, not of our arithmetic about it.
"""
from __future__ import annotations

import importlib.util
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scope_recall import vector_compaction as vc
from scope_recall.runtime.vector_upkeep import RESERVE_SECONDS, compact_if_due


def _footprint(fragments, manifests=1, transactions=1, size=0):
    return vc.VectorFootprint(fragments=fragments, manifests=manifests,
                              transactions=transactions, bytes=size)


def _state(finished_at):
    return {"schema": vc.STATE_SCHEMA, "finished_at": finished_at}


# --------------------------------------------------------------------------
# Policy: when is a compaction due?
# --------------------------------------------------------------------------

def test_a_small_store_is_left_alone():
    assert vc.compaction_due(_footprint(vc.FRAGMENT_THRESHOLD), {}) is None


def test_crossing_the_threshold_names_its_reason():
    reason = vc.compaction_due(_footprint(vc.FRAGMENT_THRESHOLD + 1), {})
    assert reason == f"fragments_above_threshold:{vc.FRAGMENT_THRESHOLD + 1}"


def test_the_cooldown_prevents_compacting_on_every_drain():
    now = datetime.now(timezone.utc)
    recent = _state((now - vc.COOLDOWN / 2).isoformat())
    assert vc.compaction_due(_footprint(5000), recent, now=now) is None
    elapsed = _state((now - vc.COOLDOWN - timedelta(seconds=1)).isoformat())
    assert vc.compaction_due(_footprint(5000), elapsed, now=now) is not None


def test_an_unreadable_or_absent_state_does_not_block_compaction(tmp_path):
    assert vc.read_state(tmp_path) == {}
    (tmp_path / vc.STATE_FILENAME).write_text("{ truncated", encoding="utf-8")
    assert vc.read_state(tmp_path) == {}
    (tmp_path / vc.STATE_FILENAME).write_text('{"schema": "someone-elses"}', encoding="utf-8")
    assert vc.read_state(tmp_path) == {}
    assert vc.compaction_due(_footprint(5000), vc.read_state(tmp_path)) is not None


def test_state_round_trips_and_replaces_cleanly(tmp_path):
    vc.write_state(tmp_path, {"finished_at": "2026-09-14T00:00:00+00:00", "fragments": 1})
    vc.write_state(tmp_path, {"finished_at": "2026-09-14T01:00:00+00:00", "fragments": 2})
    state = vc.read_state(tmp_path)
    assert state["fragments"] == 2 and state["finished_at"].startswith("2026-09-14T01")
    assert list(tmp_path.glob("*.partial")) == []


def test_footprint_of_a_missing_store_is_zero(tmp_path):
    assert vc.measure_footprint(tmp_path / "lancedb", "scope_recall").as_dict() == {
        "fragments": 0, "manifests": 0, "transactions": 0, "bytes": 0,
    }


def test_footprint_counts_files_and_bytes(tmp_path):
    table = vc.table_directory(tmp_path / "lancedb", "scope_recall")
    for sub, count in (("data", 3), ("_versions", 2), ("_transactions", 1)):
        (table / sub).mkdir(parents=True)
        for index in range(count):
            (table / sub / f"{index}.bin").write_bytes(b"x" * 10)
    measured = vc.measure_footprint(tmp_path / "lancedb", "scope_recall")
    assert (measured.fragments, measured.manifests, measured.transactions) == (3, 2, 1)
    assert measured.bytes == 60


# --------------------------------------------------------------------------
# Orchestration: never fail a drain, never act without budget
# --------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, failure: Exception | None = None):
        self.calls = 0
        self._failure = failure

    def compact(self) -> dict[str, int]:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return {}


def _oversized(tmp_path):
    """A store whose footprint is over the threshold, without any LanceDB."""
    table = vc.table_directory(tmp_path / "lancedb", "scope_recall")
    (table / "data").mkdir(parents=True)
    for index in range(vc.FRAGMENT_THRESHOLD + 1):
        (table / "data" / f"{index}.lance").write_bytes(b"x")
    return types.SimpleNamespace(storage_dir=tmp_path, table_name="scope_recall")


def test_upkeep_is_skipped_when_the_drain_has_no_budget_left(tmp_path):
    store = _FakeStore()
    assert compact_if_due(store, _oversized(tmp_path), available_seconds=RESERVE_SECONDS - 0.1) is None
    assert store.calls == 0


def test_upkeep_is_skipped_when_the_backend_cannot_compact(tmp_path):
    backend = types.SimpleNamespace()  # e.g. the sqlite-bruteforce store
    assert compact_if_due(backend, _oversized(tmp_path), available_seconds=60) is None


def test_upkeep_without_a_vector_store_is_a_no_op(tmp_path):
    assert compact_if_due(None, _oversized(tmp_path), available_seconds=60) is None
    assert compact_if_due(_FakeStore(), None, available_seconds=60) is None


def test_a_failing_compaction_is_recorded_and_does_not_raise(tmp_path):
    config = _oversized(tmp_path)
    store = _FakeStore(RuntimeError("lance said no"))
    receipt = compact_if_due(store, config, available_seconds=60)
    assert receipt["outcome"] == "failed" and receipt["error"] == "RuntimeError"
    assert store.calls == 1
    # The failure still starts the cooldown, so a broken store is retried on a
    # schedule instead of on every single drain.
    assert compact_if_due(store, config, available_seconds=60) is None
    assert store.calls == 1


# --------------------------------------------------------------------------
# Native: the LanceDB properties the policy depends on
# --------------------------------------------------------------------------

pytest_native = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None or importlib.util.find_spec("pyarrow") is None,
    reason="approved NativePY LanceDB is required for this seam",
)

_DIMENSIONS = 4


def _row(index: int) -> dict:
    return {
        "id": f"TEST-vector-{index}",
        "scope_id": "TEST-scope",
        "source": "TEST-source",
        "target": "TEST-target",
        "content": f"TEST content {index}",
        "summary": "TEST summary",
        "updated_at": "2026-09-14T00:00:00+00:00",
        "vector": [float(index), 0.0, 0.0, 1.0],
    }


def _open_store(tmp_path, rows: int):
    from scope_recall.vector_store import LanceVectorStore

    store = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=_DIMENSIONS)
    store.open()
    # One commit per row, exactly as publication does it -- that is what makes
    # fragments accumulate in the first place.
    for index in range(rows):
        store.upsert_records([_row(index)])
    return store


@pytest_native
def test_compaction_collapses_fragments_without_losing_a_row(tmp_path):
    rows = vc.FRAGMENT_THRESHOLD + 5
    store = _open_store(tmp_path, rows)
    try:
        before = vc.measure_footprint(tmp_path / "lancedb", "scope_recall")
        assert before.fragments > vc.FRAGMENT_THRESHOLD

        config = types.SimpleNamespace(storage_dir=tmp_path, table_name="scope_recall")
        receipt = compact_if_due(store, config, available_seconds=60)

        assert receipt["outcome"] == "compacted"
        assert receipt["fragments"] < before.fragments
        assert receipt["manifests"] < before.manifests
        assert store.count_rows() == rows
        assert sorted(store.list_ids()) == sorted(_row(i)["id"] for i in range(rows))
        hits = store.search([1.0, 0.0, 0.0, 1.0], scope_id="TEST-scope", limit=3)
        assert len(hits) == 3
    finally:
        store.close()


@pytest_native
def test_a_second_compaction_is_a_no_op_within_the_cooldown(tmp_path):
    store = _open_store(tmp_path, vc.FRAGMENT_THRESHOLD + 2)
    try:
        config = types.SimpleNamespace(storage_dir=tmp_path, table_name="scope_recall")
        assert compact_if_due(store, config, available_seconds=60) is not None
        assert compact_if_due(store, config, available_seconds=60) is None
    finally:
        store.close()


@pytest_native
def test_a_reader_follows_the_table_forward_after_another_writer_commits(tmp_path):
    """An open table pins its version; reads must refresh or go permanently stale.

    This is the defect that let a long-running host keep answering from the
    vectors it saw at startup while the worker kept publishing new ones.
    """
    from scope_recall.vector_store import LanceVectorStore

    writer = _open_store(tmp_path, 2)
    reader = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=_DIMENSIONS)
    reader.open_existing()
    try:
        assert reader.count_rows() == 2
        writer.upsert_records([_row(99)])
        assert reader.count_rows() == 3
        assert "TEST-vector-99" in reader.list_ids()
        hits = reader.search([99.0, 0.0, 0.0, 1.0], scope_id="TEST-scope", limit=1)
        assert hits and hits[0]["id"] == "TEST-vector-99"
    finally:
        reader.close()
        writer.close()


@pytest_native
def test_a_reader_survives_a_compaction_performed_by_another_writer(tmp_path):
    """Compaction drops superseded versions; a pinned reader would break."""
    from scope_recall.vector_store import LanceVectorStore

    rows = vc.FRAGMENT_THRESHOLD + 3
    writer = _open_store(tmp_path, rows)
    reader = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=_DIMENSIONS)
    reader.open_existing()
    try:
        assert reader.count_rows() == rows
        config = types.SimpleNamespace(storage_dir=tmp_path, table_name="scope_recall")
        assert compact_if_due(writer, config, available_seconds=60)["outcome"] == "compacted"
        assert reader.count_rows() == rows
        assert len(reader.search([1.0, 0.0, 0.0, 1.0], scope_id="TEST-scope", limit=2)) == 2
    finally:
        reader.close()
        writer.close()
