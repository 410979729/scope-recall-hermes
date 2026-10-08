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
from scope_recall.runtime.vector_upkeep import INDEX_RECHECK, RESERVE_SECONDS, compact_if_due, index_if_due
from scope_recall.vector import compaction as vc


def _footprint(fragments, manifests=1, transactions=1, size=0):
    return vc.VectorFootprint(fragments=fragments, manifests=manifests, transactions=transactions, bytes=size)


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
        "fragments": 0,
        "manifests": 0,
        "transactions": 0,
        "bytes": 0,
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


def test_a_failed_import_embedding_backfill_is_reported_beside_the_store(tmp_path):
    """A backfill that failed was written down, tried again on every pass, and read by nothing."""
    from scope_recall.runtime.vector_upkeep import backfill_if_due

    space = tmp_path / "vectors" / "TEST-space"
    (space / "lancedb" / "scope_recall.lance").mkdir(parents=True)

    class _Unreadable:
        def read(self, *_args, **_kwargs):
            raise RuntimeError("TEST store unavailable")

    context = types.SimpleNamespace(allowed_scope_ids=frozenset({"TEST-scope"}), project_id=None, branch_id=None)
    receipt = backfill_if_due(_Unreadable(), context, types.SimpleNamespace(storage_dir=space))
    assert receipt["outcome"] == "failed"
    [report] = vc.instance_vector_footprints(tmp_path)
    assert (report["embed_backfill_outcome"], report["embed_backfill_error"]) == ("failed", "RuntimeError")
    assert report["last_embed_backfill_at"] == receipt["checked_at"]


def test_a_backfill_no_worker_looks_at_any_more_is_not_the_store_s_outcome(tmp_path):
    """Each worker partition keeps its own state: one whose worker no longer runs (a retry lane, a workspace used
    once) would have kept the store at its last outcome for good."""
    from datetime import datetime, timedelta, timezone

    space = tmp_path / "vectors" / "TEST-space"
    (space / "lancedb" / "scope_recall.lance").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    for scopes, checked, outcome, total in (
        (["TEST-a"], now - timedelta(days=5), "failed", 3),
        (["TEST-b"], now - timedelta(hours=1), "finished", 5),
    ):
        vc.write_state(
            space,
            {
                "checked_at": checked.isoformat(),
                "outcome": outcome,
                "queued_total": total,
                "error": "RuntimeError" if outcome == "failed" else None,
            },
            filename=vc.embed_backfill_filename(scopes, None, None),
            schema=vc.EMBED_BACKFILL_STATE_SCHEMA,
        )
    # An rc10 test store's single file is not a partition's.
    vc.write_state(
        space,
        {"checked_at": now.isoformat(), "outcome": "failed"},
        filename="embed-backfill-state.json",
        schema=vc.EMBED_BACKFILL_STATE_SCHEMA,
    )
    [report] = vc.instance_vector_footprints(tmp_path)
    assert (report["embed_backfill_outcome"], report["embed_backfill_error"]) == ("finished", None)
    assert report["embed_backfill_queued_total"] == 8


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
    from scope_recall.vector.store import LanceVectorStore

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
    from scope_recall.vector.store import LanceVectorStore

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
    from scope_recall.vector.store import LanceVectorStore

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


# --------------------------------------------------------------------------
# The nearest-neighbour index: built once the table needs one, by a pass with the time for it
# --------------------------------------------------------------------------


class _FakeIndexStore:
    """``looks`` is what the store reports without building: needs_build, present, below_threshold."""

    def __init__(self, rows: int, failure: Exception | None = None, looks: str = "needs_build"):
        self.rows = rows
        self.failure = failure
        self.looks = looks
        self.builds: list[tuple[int, float]] = []

    def ensure_vector_index(self, *, min_rows: int, timeout_seconds: float, build: bool = True) -> dict:
        if not build:
            return {"outcome": self.looks, "rows": self.rows}
        self.builds.append((min_rows, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return {"outcome": "built", "rows": self.rows}


def _vectors(tmp_path, dimensions: int = 3072):
    return types.SimpleNamespace(storage_dir=tmp_path, table_name="scope_recall", dimensions=dimensions)


def test_an_index_is_built_once_the_table_needs_one_and_the_pass_has_the_time(tmp_path):
    """The pilot's store: 78,403 rows of 3,072 dimensions, built in 7.7 s on a copy when quiet and 25.6 s when not;
    the estimate is twice the slow rate.  A pass that could not finish it is not started on it: the watchdog would
    end the pass and the build would start over."""
    from scope_recall.vector.store import VECTOR_INDEX_MIN_ROWS

    now = datetime.now(timezone.utc)
    small = _FakeIndexStore(VECTOR_INDEX_MIN_ROWS - 1, looks="below_threshold")
    assert index_if_due(small, _vectors(tmp_path), available_seconds=110, now=now)["outcome"] == "below_threshold"
    assert small.builds == []
    big = _FakeIndexStore(78_403)
    now += INDEX_RECHECK["below_threshold"]
    tight = index_if_due(big, _vectors(tmp_path), available_seconds=60, now=now)
    assert tight["outcome"] == "deferred" and big.builds == []
    now += INDEX_RECHECK["deferred"]
    assert index_if_due(big, _vectors(tmp_path), available_seconds=110, now=now)["outcome"] == "built"
    assert len(big.builds) == 1 and big.builds[0][1] <= 110
    assert index_if_due(big, _vectors(tmp_path), available_seconds=110, now=now + timedelta(hours=1)) is None
    state = vc.read_state(tmp_path, filename=vc.INDEX_STATE_FILENAME, schema=vc.INDEX_STATE_SCHEMA)
    assert state["outcome"] == "built" and state["rows"] == 78_403


def test_a_failed_index_build_is_recorded_and_not_tried_on_every_pass(tmp_path):
    store = _FakeIndexStore(78_403, RuntimeError("lance said no"))
    now = datetime.now(timezone.utc)
    receipt = index_if_due(store, _vectors(tmp_path), available_seconds=110, now=now)
    assert receipt["outcome"] == "failed" and receipt["error"] == "RuntimeError"
    assert index_if_due(store, _vectors(tmp_path), available_seconds=110, now=now + timedelta(hours=1)) is None
    assert len(store.builds) == 1
    later = index_if_due(store, _vectors(tmp_path), available_seconds=110, now=now + INDEX_RECHECK["failed"])
    assert later["outcome"] == "failed" and len(store.builds) == 2


def test_a_build_the_watchdog_ended_is_not_started_again_on_the_next_pass(tmp_path):
    """Off Windows the build runs in the worker's own process, and a pass the watchdog ended mid-build wrote no
    receipt: the next pass started the same build, and the next."""

    class Killed(_FakeIndexStore):
        def ensure_vector_index(self, *, min_rows, timeout_seconds, build=True):
            if not build:
                return {"outcome": "needs_build", "rows": self.rows}
            self.builds.append((min_rows, timeout_seconds))
            raise KeyboardInterrupt("TEST watchdog")

    store = Killed(78_403)
    now = datetime.now(timezone.utc)
    with pytest.raises(KeyboardInterrupt):
        index_if_due(store, _vectors(tmp_path), available_seconds=110, now=now)
    state = vc.read_state(tmp_path, filename=vc.INDEX_STATE_FILENAME, schema=vc.INDEX_STATE_SCHEMA)
    assert state["outcome"] == "started"
    assert index_if_due(store, _vectors(tmp_path), available_seconds=110, now=now + timedelta(hours=1)) is None
    assert len(store.builds) == 1


def test_an_index_in_place_is_looked_at_whatever_the_store_s_size(tmp_path):
    """The estimate came before any look: past about 90,000 vectors no pass would have asked the store, merged a
    segment or replaced an index of another kind, and the doctor would have shown a healthy index as deferred."""
    huge = _FakeIndexStore(5_000_000, looks="present")
    receipt = index_if_due(huge, _vectors(tmp_path), available_seconds=5)
    assert receipt["outcome"] == "present" and huge.builds == []


def test_index_upkeep_without_a_store_that_can_index_is_a_no_op(tmp_path):
    assert index_if_due(None, _vectors(tmp_path), available_seconds=110) is None
    assert index_if_due(types.SimpleNamespace(count_rows=lambda: 1), _vectors(tmp_path), available_seconds=110) is None
    assert index_if_due(_FakeIndexStore(1), None, available_seconds=110) is None


def _spread_rows(count: int, dimensions: int = 8) -> list[dict]:
    import random

    generator = random.Random(20260928)
    return [
        {
            "id": f"TEST-vector-{index}",
            "scope_id": "TEST-scope",
            "source": "TEST-source",
            "target": "TEST-target",
            "content": f"TEST content {index}",
            "summary": "TEST summary",
            "updated_at": "2026-09-28T00:00:00+00:00",
            "vector": [generator.uniform(-1.0, 1.0) for _ in range(dimensions)],
        }
        for index in range(count)
    ]


@pytest_native
def test_the_index_is_built_used_and_kept_current(tmp_path):
    """Without the index every search read every vector (750 ms over the pilot's 78,000).  With it a search returns
    the same nearest rows, the hit carries only what the port reads, and compaction adds what came after."""
    from scope_recall.vector.store import LanceVectorStore

    rows = _spread_rows(1200)
    store = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=8)
    store.open()
    try:
        store.upsert_records(rows)
        assert store.ensure_vector_index(min_rows=2000)["outcome"] == "below_threshold"
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "built"
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "present"
        hits = store.search(rows[17]["vector"], scope_id="TEST-scope", limit=5)
        assert hits[0]["_distance"] < 1e-6
        assert hits[0]["id"] == "TEST-vector-17"
        assert set(hits[0]) == {"id", "scope_id", "source", "target", "_distance"}
        later = _spread_rows(1300)[1200:]
        store.upsert_records(later)
        store.compact()
        assert store.search(later[3]["vector"], scope_id="TEST-scope", limit=1)[0]["id"] == "TEST-vector-1203"
        assert store.search_scopes(later[3]["vector"], scope_ids=["TEST-scope"], limit=1)[0]["id"] == "TEST-vector-1203"
    finally:
        store.close()


@pytest_native
def test_a_vector_index_of_another_kind_is_replaced(tmp_path):
    """The pilot's store was first indexed with HNSW by hand.  Half its rows share their vector with another row, and
    over such duplicates the graph missed nearest rows and once returned 2 rows for 10: the kept index is IVF_SQ."""
    from scope_recall.vector.store import LanceVectorStore

    rows = _spread_rows(1200)
    rows += [{**row, "id": f"TEST-copy-{index}"} for index, row in enumerate(rows[:600])]
    store = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=8)
    store.open()
    try:
        store.upsert_records(rows)
        store._fresh_table().create_index(metric="cosine", vector_column_name="vector", index_type="IVF_HNSW_SQ")
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "rebuilt"
        kinds = [str(index.index_type) for index in store._fresh_table().list_indices()]
        assert kinds == ["IvfSq"], kinds
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "present"
        for probe in (rows[17], rows[600]):
            hits = store.search(probe["vector"], scope_id="TEST-scope", limit=2)
            assert {hit["_distance"] for hit in hits} and min(hit["_distance"] for hit in hits) < 1e-6
    finally:
        store.close()


@pytest_native
def test_index_segments_past_the_limit_are_built_again_as_one(tmp_path, monkeypatch):
    """Each compaction that indexed new rows added a segment and nothing merged them: searches slowed as they
    piled up (78 ms warm at 120 segments over 78,000 vectors, against 47)."""
    from scope_recall.vector import store as store_module
    from scope_recall.vector.store import LanceVectorStore

    monkeypatch.setattr(store_module, "MAX_INDEX_SEGMENTS", 2)
    rows = _spread_rows(1600)
    store = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=8)
    store.open()
    try:
        store.upsert_records(rows[:1200])
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "built"
        for start in (1200, 1300, 1400, 1500):
            store.upsert_records(rows[start : start + 100])
            store.compact()
        segments = store._fresh_table().index_stats("vector_idx").num_indices
        assert segments > 2, segments
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "rebuilt"
        assert store._fresh_table().index_stats("vector_idx").num_indices == 1
        assert store.ensure_vector_index(min_rows=1000)["outcome"] == "present"
        assert store.search(rows[1450]["vector"], scope_id="TEST-scope", limit=1)[0]["id"] == "TEST-vector-1450"
    finally:
        store.close()


pytest_windows_helper = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None or __import__("sys").platform != "win32",
    reason="the helper process store is the Windows one",
)


@pytest_windows_helper
def test_a_helper_started_ahead_is_the_one_the_store_uses(tmp_path):
    """A Claude Code or Codex hook starts the helper when the hook starts; the store takes that helper instead of
    starting its own when its recall reaches the vector search.  None is kept ready after it: a server's runtimes
    share the store that took it (3.4.9)."""
    from scope_recall.vector import process_store
    from scope_recall.vector.store import LanceVectorStore

    seed = LanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=8)
    seed.open()
    seed.upsert_records(_spread_rows(1200))
    seed.close()
    try:
        process_store.prestart()
        spare = process_store._spare
        assert spare is not None and spare.poll() is None
        store = process_store.ProcessLanceVectorStore(tmp_path / "lancedb", table_name="scope_recall", dimensions=8)
        store.open_existing()
        try:
            assert store._process is spare and process_store._spare is None
            assert store.ensure_vector_index(min_rows=1000, timeout_seconds=120)["outcome"] == "built"
            assert (
                store.search(_spread_rows(1200)[5]["vector"], scope_id="TEST-scope", limit=1)[0]["id"]
                == "TEST-vector-5"
            )
        finally:
            store.close()
        assert process_store._spare is None, "taken once: a server's runtimes share the store that took it"
    finally:
        process_store.discard_spare()
