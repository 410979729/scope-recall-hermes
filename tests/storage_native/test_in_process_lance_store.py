"""Publication and purge contracts also hold with the native index loaded."""

from __future__ import annotations

import importlib.util

import pytest
from scope_recall.vector.sqlite_store import SQLiteVectorStore

from tests.contract import test_every_store_meets_the_runtime as seams


@pytest.mark.parametrize("check", seams.CHECKS, ids=lambda check: check.__name__.removeprefix("check_"))
def test_the_in_process_lance_store(check, tmp_path):
    store = SQLiteVectorStore(tmp_path / "vectors.sqlite3", table_name="TEST_vectors", dimensions=2, metric="cosine")
    store.open()
    try:
        check(store)
    finally:
        store.close()
