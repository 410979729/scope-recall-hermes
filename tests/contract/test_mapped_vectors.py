"""Scoped mapped candidates keep one budget and retain full float32 scores."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from scope_recall.vector.store import build_vector_store


def vector_row(key, values, scope="TEST-scope"):
    """Make a text-free synthetic companion row."""
    return dict(id=key, scope_id=scope, source=key, target="{}", content="", summary="", updated_at="", vector=values)


def finish_index(store):
    """Publish all bounded pages before measuring indexed candidate work."""
    for _ in range(10):
        receipt = store.maintain_index(max_rows=512)
        if not receipt.get("rows"):
            return
    raise AssertionError("synthetic index did not finish")


def test_global_candidate_budget_and_mapped_reranking(tmp_path, monkeypatch):
    import scope_recall.vector.search as search

    count, dimensions = 2048, 3072
    angles = np.linspace(0.01, 2.4, count, dtype=np.float32)
    vectors = np.zeros((count, dimensions), dtype=np.float32)
    vectors[:, 0] = 0.6 * np.cos(angles)
    vectors[:, 1] = 0.6 * np.sin(angles)
    vectors[:, 768] = 0.8
    query = np.zeros(dimensions, dtype=np.float32)
    query[0] = 1
    scopes = [f"TEST-scope-{i}" for i in range(8)]
    store = build_vector_store("sqlite", storage_dir=tmp_path, table_name="vectors", dimensions=dimensions)
    store.open()
    try:
        store.register_space("TEST-space", {"model": "gemini-embedding-2", "dimensions": dimensions})
        store.upsert_records(vector_row(f"TEST:{i:04d}", vectors[i], scopes[i % 8]) for i in range(count))
        finish_index(store)
        reranked = []
        original = search._distances

        def observed(matrix, values, metric, **kwargs):
            reranked.append(len(matrix))
            return original(matrix, values, metric, **kwargs)

        monkeypatch.setattr(search, "_distances", observed)
        statements = []
        store._require_conn().set_trace_callback(statements.append)
        found = store.search_scopes(query.tolist(), scope_ids=scopes, limit=40)
        assert [r["id"] for r in found] == [f"TEST:{i:04d}" for i in range(40)]
        assert sum(reranked) <= 256
        assert not any("VECTOR FROM VECTOR_RECORDS" in sql.upper() and "SEQ IN" in sql.upper() for sql in statements)
    finally:
        store.close()


@contextmanager
def mapped_store(path, *, model="gemini-embedding-2", dimensions=3072):
    """Open an isolated synthetic embedding space and release all mapped handles."""
    store = build_vector_store("sqlite", storage_dir=path, table_name="vectors", dimensions=dimensions)
    try:
        store.open()
        store.register_space("TEST-space", {"model": model, "dimensions": dimensions})
        yield store
    finally:
        store.close()


def embedding(*components, dimensions=3072):
    """Build a sparse finite synthetic float32 vector."""
    result = np.zeros(dimensions, dtype=np.float32)
    for position, value in components:
        result[position] = value
    return result


def test_full_dimension_scores_override_prefix_order_and_doctor_reports_scheme(tmp_path):
    """A better prefix cannot replace a better full-dimensional cosine score."""
    query = embedding((0, 0.6), (900, 0.8))
    first = embedding((0, 1.0))
    second = embedding((0, 0.6), (1, 0.01), (900, 0.8))
    with mapped_store(tmp_path) as store:
        store.upsert_records([vector_row("TEST-prefix", first), vector_row("TEST-full", second)])
        finish_index(store)
        found = store.search(query.tolist(), scope_id="TEST-scope", limit=2)
        assert [r["id"] for r in found] == ["TEST-full", "TEST-prefix"]
        expected = float(
            np.dot(query.astype("f8"), second.astype("f8"))
            / (np.linalg.norm(query.astype("f8")) * np.linalg.norm(second.astype("f8")))
        )
        assert abs(1 - found[0]["_distance"] - expected) < 1e-12
        details = store.diagnostics()
        assert details["engine"] == "numpy-prefix"
        assert details["coarse_dimensions"] == 768 and details["dimensions"] == 3072
        assert details["compute_threads"] == 1 and details["candidate_target"] == 256


def test_open_reader_sees_tail_replacement_and_worker_appends(tmp_path):
    """Watermarks do not hide new rows or keep an overwritten sequence alive."""
    query = embedding((0, 1.0)).tolist()
    with mapped_store(tmp_path) as writer:
        writer.upsert_records([vector_row("TEST-old", embedding((1, 1.0)))])
        finish_index(writer)
        reader = build_vector_store("sqlite", storage_dir=tmp_path, table_name="vectors", dimensions=3072)
        reader.open_existing()
        try:
            assert reader.search(query, scope_id="TEST-scope", limit=1)[0]["id"] == "TEST-old"
            watermark = reader.diagnostics()["watermark"]
            writer.upsert_records([vector_row("TEST-new", query)])
            assert reader.search(query, scope_id="TEST-scope", limit=1)[0]["id"] == "TEST-new"
            assert reader.diagnostics()["watermark"] == watermark
            finish_index(writer)
            assert reader.diagnostics()["watermark"] > watermark
            writer.upsert_records([vector_row("TEST-old", query)])
            found = reader.search(query, scope_id="TEST-scope", limit=3)
            assert {r["id"] for r in found} == {"TEST-new", "TEST-old"}
            assert all(r["_distance"] == 0 for r in found)
            finish_index(writer)
            assert reader.search(query, scope_id="TEST-scope", limit=3) == found
        finally:
            reader.close()


def test_deleted_cached_rows_obey_fences_and_authorized_scopes(tmp_path):
    """A cached vector cannot bypass a tombstone or an authorized scope filter."""
    query = embedding((0, 1.0)).tolist()
    with mapped_store(tmp_path) as store:
        gone = vector_row("TEST-gone", query)
        store.upsert_records(
            [gone, vector_row("TEST-live", embedding((1, 1.0))), vector_row("TEST-hidden", query, "TEST-other")]
        )
        finish_index(store)
        store.delete_by_ids(["TEST-gone"])
        store.upsert_records([gone])
        expected = store.search(query, scope_id="TEST-scope", limit=3)
        assert [r["id"] for r in expected] == ["TEST-live"]
        finish_index(store)
        assert store.search(query, scope_id="TEST-scope", limit=3) == expected
        assert not store.contains_id("TEST-gone")


def test_missing_derived_file_wakes_worker_and_rebuilds_identical_results(tmp_path):
    """Losing a derived file leaves raw data intact and schedules its reconstruction."""
    from scope_recall.runtime.vector_upkeep import next_vector_wake
    from scope_recall.vector.cache import cache_directory

    vectors = np.random.default_rng(23).standard_normal((24, 3072), dtype=np.float32)
    with mapped_store(tmp_path) as store:
        store.upsert_records(vector_row(f"TEST:{i}", v) for i, v in enumerate(vectors))
        finish_index(store)
        query = vectors[0].tolist()
        expected = store.search(query, scope_id="TEST-scope", limit=10)
        conn = store._require_conn()
        original = [(r[0], bytes(r[1])) for r in conn.execute("SELECT id,vector FROM vector_records ORDER BY id")]
        generation = conn.execute("SELECT value FROM vector_meta WHERE key='cache_generation'").fetchone()[0]
        store.close()
        damaged = next(cache_directory(store.db_path).glob("*.prefix.f32"))
        assert damaged.resolve().is_relative_to(tmp_path.resolve())
        damaged.unlink()
        store.open()
        assert store.diagnostics()["engine"] == "numpy-raw"
        config = SimpleNamespace(vector=SimpleNamespace(storage_dir=tmp_path))
        assert next_vector_wake(config, datetime.now(UTC))[1] == "vector_index"
        assert store.search(query, scope_id="TEST-scope", limit=10) == expected
        finish_index(store)
        conn = store._require_conn()
        assert conn.execute("SELECT value FROM vector_meta WHERE key='cache_generation'").fetchone()[0] != generation
        assert [
            (r[0], bytes(r[1])) for r in conn.execute("SELECT id,vector FROM vector_records ORDER BY id")
        ] == original
        assert store.search(query, scope_id="TEST-scope", limit=10) == expected
        assert store.diagnostics()["engine"] == "numpy-prefix"


def test_failed_file_publication_keeps_old_prefix_and_retries_without_duplicates(tmp_path, monkeypatch):
    """Unpublished appended bytes never advance SQLite counts or hide raw tail rows."""
    import scope_recall.vector.cache as cache

    with mapped_store(tmp_path) as store:
        store.upsert_records([vector_row("TEST-old", embedding((0, 1.0)))])
        finish_index(store)
        store.search(embedding((0, 1.0)).tolist(), scope_id="TEST-scope", limit=1)
        watermark = store.diagnostics()["watermark"]
        store.upsert_records([vector_row("TEST-tail", embedding((1, 1.0)))])
        original = cache._append

        def interrupted(path, start, values):
            original(path, start, values)
            if path.name.endswith("norms.f64"):
                raise OSError("TEST publication interruption")

        monkeypatch.setattr(cache, "_append", interrupted)
        with pytest.raises(OSError, match="TEST publication"):
            store.maintain_index()
        assert store.diagnostics()["watermark"] == watermark
        query = embedding((1, 1.0)).tolist()
        found = store.search(query, scope_id="TEST-scope", limit=3)
        assert len(found) == 2 and found[0]["id"] == "TEST-tail"
        monkeypatch.setattr(cache, "_append", original)
        finish_index(store)
        assert store.search(query, scope_id="TEST-scope", limit=3) == found
        assert store.diagnostics()["cached_rows"] == 2


@pytest.mark.parametrize("model", ["TEST-unknown-model", "gemini-embedding-2"])
def test_undeclared_or_unknown_models_scan_full_dimensions(tmp_path, model):
    """Both the approved data table and a space capability record are required for a prefix."""
    with mapped_store(tmp_path, model=model) as store:
        if model == "gemini-embedding-2":
            store._require_conn().execute("UPDATE embedding_spaces SET capabilities='{}'")
            store._require_conn().commit()
        store.upsert_records(
            [vector_row("TEST-prefix", embedding((0, 1.0))), vector_row("TEST-tail", embedding((900, 1.0)))]
        )
        finish_index(store)
        assert store.diagnostics()["engine"] == "numpy-exact"
        assert store.diagnostics()["coarse_dimensions"] == 3072
        assert store.search(embedding((900, 1.0)).tolist(), scope_id="TEST-scope", limit=1)[0]["id"] == "TEST-tail"


@pytest.mark.parametrize("dimensions", [8, 768, 1536, 3072])
def test_route_dimensions_keep_original_scores(tmp_path, dimensions):
    """Prefix layouts follow the registered dimension while final scoring keeps every component."""
    with mapped_store(tmp_path, dimensions=dimensions) as store:
        query = embedding((dimensions - 1, 1.0), dimensions=dimensions).tolist()
        store.upsert_records(
            [vector_row("TEST-zero", np.zeros(dimensions, dtype=np.float32)), vector_row("TEST-hit", query)]
        )
        finish_index(store)
        found = store.search(query, scope_id="TEST-scope", limit=2)
        assert [r["id"] for r in found] == ["TEST-hit", "TEST-zero"]
        assert [r["_distance"] for r in found] == [0.0, 1.0]
        assert store.diagnostics()["coarse_dimensions"] == (768 if dimensions > 768 else dimensions)


def test_changing_scopes_releases_unneeded_mappings(tmp_path):
    """A reader visiting many scopes does not keep their file handles after the scope changes."""
    query = embedding((0, 1.0)).tolist()
    with mapped_store(tmp_path) as store:
        store.upsert_records([vector_row("TEST-a", query, "TEST-a"), vector_row("TEST-b", query, "TEST-b")])
        finish_index(store)
        assert store.search(query, scope_id="TEST-a", limit=1)[0]["id"] == "TEST-a"
        mappings = [array._mmap for array in store._mapped.scopes["TEST-a"].arrays.values()]
        assert all(not mapping.closed for mapping in mappings)
        assert store.search(query, scope_id="TEST-b", limit=1)[0]["id"] == "TEST-b"
        assert all(mapping.closed for mapping in mappings)
        assert store.search(query, scope_id="TEST-a", limit=1)[0]["id"] == "TEST-a"
