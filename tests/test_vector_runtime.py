"""Tests for vector-store protocol adapters and factory selection."""

from __future__ import annotations

import pytest

from scope_recall.vector_store import VectorRecord, build_vector_store


def test_vector_store_factory_builds_sqlite_bruteforce_protocol_adapter(tmp_path):
    store = build_vector_store(
        "sqlite-bruteforce",
        storage_dir=tmp_path,
        table_name="memories",
        dimensions=2,
        metric="cosine",
    )
    assert store.backend == "sqlite-bruteforce"
    store.open()
    try:
        store.upsert(
            VectorRecord(
                id="m1",
                scope_id="scope-a",
                source="unit-test",
                target="memory",
                content="alpha",
                summary="alpha summary",
                updated_at="2026-01-01T00:00:00+00:00",
                vector=[1.0, 0.0],
            )
        )

        hits = store.search([1.0, 0.0], scope_id="scope-a", limit=5)
        assert hits[0]["id"] == "m1"
        assert store.audit_counts()["unique_ids"] == 1
        assert store.delete(["m1"]) == 1
        assert store.audit_counts()["physical_rows"] == 0
    finally:
        store.close()


def test_vector_store_factory_accepts_sqlite_alias_and_rejects_unknown_backend(tmp_path):
    aliased = build_vector_store("sqlite", storage_dir=tmp_path, table_name="memories", dimensions=2, metric="cosine")
    assert aliased.backend == "sqlite-bruteforce"

    with pytest.raises(ValueError, match="unsupported vector backend"):
        build_vector_store("unknown", storage_dir=tmp_path, table_name="memories", dimensions=2, metric="cosine")


@pytest.mark.parametrize("reserved_backend", ["qdrant", "chroma"])
def test_reserved_future_vector_backends_fail_fast_without_runtime_dependency(tmp_path, reserved_backend: str):
    with pytest.raises(ValueError, match="unsupported vector backend"):
        build_vector_store(reserved_backend, storage_dir=tmp_path, table_name="memories", dimensions=2, metric="cosine")
