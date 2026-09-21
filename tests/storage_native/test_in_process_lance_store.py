"""The in-process LanceDB store can be published to and purged through the runtime's two seams.

This is the store every LanceDB install off Windows runs (``build_vector_store``), and until #99 it
could do neither: see ``tests/contract/test_every_store_meets_the_runtime.py``, which holds the
behaviour and runs it against the SQLite companion.  It is driven directly here rather than through
``build_vector_store``, which hands Windows the helper-process store, so that the in-process one is
exercised wherever this tier runs.  LanceDB needs a socket and a child process to load, which is why
these cases live in the native tier and not beside the others.
"""
from __future__ import annotations

import importlib.util

import pytest

from scope_recall.vector.store import LanceVectorStore
from tests.contract import test_every_store_meets_the_runtime as seams

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None or importlib.util.find_spec("pyarrow") is None,
    reason="needs the approved LanceDB install",
)


@pytest.mark.parametrize("check", seams.CHECKS, ids=lambda check: check.__name__.removeprefix("check_"))
def test_the_in_process_lance_store(check, tmp_path):
    store = LanceVectorStore(tmp_path / "lancedb", table_name="TEST_vectors", dimensions=2, metric="cosine")
    store.open()
    try:
        check(store)
    finally:
        store.close()
