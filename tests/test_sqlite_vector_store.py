from __future__ import annotations

import sqlite3
import threading

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore
from scope_recall.vector_runtime import setup_vector_layer


class RuntimeProvider:
    def __init__(self, tmp_path):
        self._storage_dir = tmp_path / "scope-recall"
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)
        self._lock = threading.RLock()
        self._vector_config = {
            "enabled": True,
            "backend": "sqlite-bruteforce",
            "table_name": "memories",
            "index_general": False,
            "top_k": 4,
            "embedder": {"provider": "local-debug", "dimensions": 16, "model": "debug-hash-v1"},
        }
        self._retrieval_config = {"metric": "cosine", "vector_min_score": 0.0}
        self._vector_backend = ""
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._embedder = None
        self._vector_store = None
        self._scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]

    def _require_conn(self):
        return self._conn

    def _vector_text(self, summary, content):
        return f"{summary}\n{content}".strip()


def test_sqlite_bruteforce_store_upsert_search_repair(tmp_path):
    store = SQLiteBruteForceVectorStore(tmp_path / "vector.sqlite3", table_name="memories", dimensions=2, metric="cosine")
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "a",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "alpha memory",
                    "summary": "alpha",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                },
                {
                    "id": "b",
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "target": "memory",
                    "content": "beta memory",
                    "summary": "beta",
                    "updated_at": "2026-01-02T00:00:00+00:00",
                    "vector": [0.0, 1.0],
                },
            ]
        )

        assert store.count_rows() == 2
        assert store.audit_counts() == {"physical_rows": 2, "unique_ids": 2, "duplicate_rows": 0, "duplicate_ids": 0}
        assert store.search([1.0, 0.0], scope_id="scope-a", limit=1)[0]["id"] == "a"

        repaired = store.repair_records({"a": {"updated_at": "2026-01-01T00:00:00+00:00"}})
        assert repaired == 1
        assert store.list_ids() == ["a"]
    finally:
        store.close()


def test_setup_vector_layer_can_use_sqlite_bruteforce_without_lancedb(tmp_path):
    provider = RuntimeProvider(tmp_path)
    store_row(
        provider._conn,
        memory_id="memory-1",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store",
        target="memory",
        content="SQLite brute force vector backend supports non AVX CPUs.",
    )

    setup_vector_layer(provider)

    try:
        assert provider._vector_ready is True
        assert provider._vector_status == "ready"
        assert provider._vector_store is not None
        assert provider._embedder is not None
        store = provider._vector_store
        embedder = provider._embedder
        assert store.backend == "sqlite-bruteforce"
        assert provider._vector_row_count == 1
        rows = store.search(embedder.embed("non AVX vector backend"), scope_id="scope-a", limit=3)
        assert rows and rows[0]["id"] == "memory-1"
    finally:
        if provider._vector_store is not None:
            provider._vector_store.close()
        provider._conn.close()
