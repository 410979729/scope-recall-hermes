"""Derived bytes cannot change authoritative scores or hide maintenance work."""

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from scope_recall.core.deadline import RequestDeadline, remaining_seconds, using_request_deadline
from scope_recall.runtime.vector_upkeep import next_vector_wake

from tests.contract.test_mapped_vectors import embedding, finish_index, mapped_store, vector_row


def overwrite(path, values):
    """Change bytes in an owned synthetic cache without changing its length."""
    size = path.stat().st_size
    with path.open("r+b") as stream:
        stream.write(values.tobytes())
        stream.flush()
    assert path.stat().st_size == size


@pytest.mark.parametrize("damage", ["prefix_nan", "norm_nan", "raw_finite", "sequence", "sequence_future"])
def test_same_length_corruption_falls_back_and_schedules_rebuild(tmp_path, damage):
    """Read-only recalls quarantine detected corruption for every reader and the worker."""
    from scope_recall.vector.store import build_vector_store

    query = embedding((0, 1.0)).tolist()
    with mapped_store(tmp_path) as writer:
        writer.upsert_records([vector_row("TEST-a", query), vector_row("TEST-b", embedding((1, 1.0)))])
        finish_index(writer)
        expected = writer.search(query, scope_id="TEST-scope", limit=2)
        generation = writer._mapped.generation
        writer._mapped.close()
        if damage == "prefix_nan":
            overwrite(next(writer._mapped.root.glob("*.prefix.f32")), np.array([np.nan], dtype="<f4"))
        elif damage == "norm_nan":
            overwrite(next(writer._mapped.root.glob("*.norms.f64")), np.array([np.nan], dtype="<f8"))
        elif damage in {"sequence", "sequence_future"}:
            value = -1 if damage == "sequence" else 2**62
            overwrite(next(writer._mapped.root.glob("*.seqs.i64")), np.array([value], dtype="<i8"))
        else:
            overwrite(next(writer._mapped.root.glob("*.raw.f32")), np.array([0.0, 1.0], dtype="<f4"))
        reader = build_vector_store("sqlite", storage_dir=tmp_path, table_name="vectors", dimensions=3072)
        reader.open_existing()
        try:
            assert reader.search(query, scope_id="TEST-scope", limit=2) == expected
            assert not reader.diagnostics()["cache_ready"]
            assert reader.diagnostics()["cache_error"]
            assert not writer.diagnostics()["cache_ready"]
            config = SimpleNamespace(vector=SimpleNamespace(storage_dir=tmp_path))
            assert next_vector_wake(config, datetime.now(UTC))[1] == "vector_index"
            finish_index(writer)
            assert reader.search(query, scope_id="TEST-scope", limit=2) == expected
            assert reader.diagnostics()["cache_ready"]
            assert reader._mapped.generation != generation
        finally:
            reader.close()


def test_background_page_checksum_detects_finite_unreturned_changes(tmp_path):
    """Worker auditing covers finite corruption outside the final result set."""
    with mapped_store(tmp_path) as store:
        store.upsert_records([vector_row("TEST-a", embedding((0, 1.0)))])
        finish_index(store)
        path = next(store._mapped.root.glob("*.prefix.f32"))
        overwrite(path, np.array([0.25], dtype="<f4"))
        store._require_conn().execute("INSERT OR REPLACE INTO vector_meta VALUES('cache_verify_after','0')")
        store._require_conn().commit()
        receipt = store.maintain_index()
        assert receipt["outcome"] == "corrupt"
        assert not store.diagnostics()["cache_ready"]
        finish_index(store)
        assert store.diagnostics()["cache_ready"]


def test_retired_file_failure_stays_scheduled_until_reader_releases(tmp_path, monkeypatch):
    """A caught-up watermark cannot hide a failed generation cleanup."""
    from pathlib import Path

    with mapped_store(tmp_path) as store:
        store.upsert_records([vector_row("TEST-a", embedding((0, 1.0)))])
        finish_index(store)
        old = list(store._mapped.root.iterdir())
        blocked = {str(path) for path in old}
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if str(path) in blocked:
                raise PermissionError("TEST mapped reader")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        conn = store._require_conn()
        conn.execute("UPDATE vector_meta SET value='1' WHERE key='cache_dimensions'")
        conn.commit()
        finish_index(store)
        config = SimpleNamespace(vector=SimpleNamespace(storage_dir=tmp_path))
        wake = next_vector_wake(config, datetime.now(UTC))
        assert wake is not None and wake[1] == "vector_collect"
        assert store.diagnostics()["collection_pending"]
        blocked.clear()
        store.maintain_index()
        assert not any(path.exists() for path in old)
        assert not store.diagnostics()["collection_pending"]


def test_collection_checks_current_generation_inside_write_transaction(tmp_path, monkeypatch):
    """A second maintainer cannot publish between the generation check and reclamation."""
    with mapped_store(tmp_path) as store:
        conn = store._require_conn()
        original = store._mapped._collect
        protected = []

        def collect(*args, **kwargs):
            protected.append(conn.in_transaction)
            return original(*args, **kwargs)

        monkeypatch.setattr(store._mapped, "_collect", collect)
        store.maintain_index()
        assert protected and all(protected)


def test_default_candidate_and_thread_diagnostics_have_one_source(tmp_path):
    """The configured search budget is also the budget an operator sees."""
    with mapped_store(tmp_path) as store:
        details = store.diagnostics()
        assert details["candidate_target"] == 1024
        assert 1 <= details["compute_threads"] <= 4


def test_parallel_blocks_keep_deadline_context_and_join_before_return(monkeypatch):
    """Only mapped numerical blocks run off-thread, with the caller's deadline intact."""
    import scope_recall.vector.search as search

    main = threading.get_ident()
    seen, active = [], set()
    lock = threading.Lock()
    original = np.einsum

    def observed(expression, *args, **kwargs):
        if expression == "ij,j->i":
            identity = threading.get_ident()
            with lock:
                active.add(identity)
                seen.append((identity, remaining_seconds()))
            try:
                time.sleep(0.01)
                return original(expression, *args, **kwargs)
            finally:
                with lock:
                    active.remove(identity)
        return original(expression, *args, **kwargs)

    monkeypatch.setattr(search, "compute_threads", lambda: 4, raising=False)
    monkeypatch.setattr(np, "einsum", observed)
    part = SimpleNamespace(count=32768, coarse=np.ones((32768, 1), dtype="<f4"))
    deadline = RequestDeadline.from_absolute(100.5, clock=lambda: 100.0)
    with using_request_deadline(deadline):
        assert len(search._coarse_scores([part], np.array([1.0, 0.0], dtype="<f4"))) == 32768
    assert len({identity for identity, _ in seen if identity != main}) >= 2
    assert all(value == 0.5 for _, value in seen)
    assert not active


def test_background_checksum_batches_resume_across_pages(tmp_path, monkeypatch):
    """A bounded audit eventually reaches a damaged page past its first batch."""
    import scope_recall.vector.cache as cache

    with mapped_store(tmp_path, dimensions=8) as store:
        values = embedding((0, 1.0), dimensions=8)
        store.upsert_records(vector_row(f"TEST-{i}", values) for i in range(1300))
        finish_index(store)
        path = next(store._mapped.root.glob("*.raw.f32"))
        with path.open("r+b") as stream:
            stream.seek(1024 * 8 * 4)
            stream.write(np.array([0.25], dtype="<f4").tobytes())
        conn = store._require_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO vector_meta VALUES(?,?)",
            [
                ("cache_verify_after", "0"),
                ("cache_verify_page", "0"),
            ],
        )
        conn.commit()
        monkeypatch.setattr(cache, "VERIFY_PAGES", 1)
        for _ in range(2):
            receipt = store.maintain_index()
            assert receipt["outcome"] == "indexed" and receipt["verified_pages"] == 1
        assert store.maintain_index()["outcome"] == "corrupt"
        assert not store.diagnostics()["cache_ready"]


@pytest.mark.parametrize(("physical", "expected"), [(1, 1), (2, 2), (12, 4)])
def test_scan_threads_never_exceed_physical_cores(monkeypatch, physical, expected):
    """Small machines retain one worker per physical core, larger ones cap at four."""
    from scope_recall.vector import defaults

    defaults.compute_threads.cache_clear()
    monkeypatch.setattr(defaults, "_physical_cores", lambda: physical)
    try:
        assert defaults.compute_threads() == expected
    finally:
        defaults.compute_threads.cache_clear()


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_failed_parallel_scan_waits_for_every_started_block(monkeypatch, failure):
    """Cancellation never leaves a numerical task accessing a released mapping."""
    import scope_recall.vector.search as search
    from scope_recall.vector.cache import CacheCorrupt

    active, seen = set(), []
    lock = threading.Lock()
    tick = [100.0]

    def block(*args):
        identity = threading.get_ident()
        with lock:
            first = not seen
            seen.append(identity)
            active.add(identity)
        try:
            time.sleep(0.02)
            if first:
                if failure == "error":
                    raise CacheCorrupt("non_finite_scores")
                tick[0] = 100.6
            search.check_deadline()
        finally:
            with lock:
                active.remove(identity)

    monkeypatch.setattr(search, "compute_threads", lambda: 4)
    monkeypatch.setattr(search, "_score_block", block)
    part = SimpleNamespace(count=32768, coarse=np.ones((32768, 1), dtype="<f4"))
    deadline = RequestDeadline.from_absolute(100.5, clock=lambda: tick[0])
    with using_request_deadline(deadline), pytest.raises(CacheCorrupt if failure == "error" else TimeoutError):
        search._coarse_scores([part], np.array([1.0, 0.0], dtype="<f4"))
    assert len(set(seen)) >= 2
    assert not active


def test_checksum_audit_keeps_progressing_while_rows_are_published(tmp_path, monkeypatch):
    """Continuous publication cannot indefinitely postpone a due integrity check."""
    import scope_recall.vector.cache as cache

    with mapped_store(tmp_path, dimensions=8) as store:
        values = embedding((0, 1.0), dimensions=8)
        store.upsert_records([vector_row("TEST-first", values)])
        finish_index(store)
        store._require_conn().execute("UPDATE vector_meta SET value='0' WHERE key='cache_verify_after'")
        store._require_conn().commit()
        monkeypatch.setattr(cache, "VERIFY_PAGES", 1)
        store.upsert_records([vector_row("TEST-new", values)])
        receipt = store.maintain_index()
        assert receipt["rows"] == 1 and receipt["verified_pages"] == 1
