from __future__ import annotations

import sqlite3

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_runtime import sync_vector_index, upsert_vector_record


class FakeEmbedder:
    dimensions = 2
    provider = "fake"

    def __init__(self):
        self.embedded_texts = []

    def embed(self, text):
        self.embedded_texts.append(text)
        return [1.0, 0.0]

    def embed_texts(self, texts):
        texts = list(texts)
        self.embedded_texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self.records = {}
        self.deleted = []

    def list_ids(self):
        return list(self.records)

    def list_records(self):
        return dict(self.records)

    def delete_by_ids(self, ids):
        self.deleted.extend(ids)
        for memory_id in ids:
            self.records.pop(memory_id, None)

    def upsert_records(self, rows):
        for row in rows:
            self.records[str(row["id"])] = dict(row)

    def repair_records(self, desired_records):
        self.records = {memory_id: dict(row) for memory_id, row in desired_records.items() if memory_id in self.records}
        return len(self.records)

    def audit_counts(self):
        return {
            "physical_rows": len(self.records),
            "unique_ids": len(self.records),
            "duplicate_rows": 0,
            "duplicate_ids": 0,
        }


class FakeProvider:
    def __init__(self, conn, *, index_general=False):
        self._conn = conn
        self._lock = __import__("threading").RLock()
        self._vector_config = {"index_general": index_general}
        self._vector_ready = True
        self._vector_store = FakeVectorStore()
        self._embedder = FakeEmbedder()
        self._scope_id = "local-scope"
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._vector_status = "ready"
        self._vector_message = ""

    def _require_conn(self):
        return self._conn

    def _vector_text(self, summary, content):
        return f"{summary}\n{content}".strip()


def _insert(conn, *, memory_id, target, scope_id="local-scope", content=None):
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="tool-store" if target != "general" else "turn-user",
        target=target,
        content=content or f"{target} vector policy row",
    )


def test_sync_vector_index_excludes_general_and_deletes_stale_general_vectors():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="general-1", target="general", content="general scratch should not be indexed")
    _insert(conn, memory_id="memory-1", target="memory", scope_id="shared-scope", content="durable memory should be indexed")
    provider = FakeProvider(conn, index_general=False)
    provider._vector_store.records["general-1"] = {"id": "general-1", "target": "general", "updated_at": "old"}

    count = sync_vector_index(provider)

    assert count == 1
    assert set(provider._vector_store.records) == {"memory-1"}
    assert "general-1" in provider._vector_store.deleted
    assert all("general scratch" not in text for text in provider._embedder.embedded_texts)


def test_upsert_vector_record_deletes_existing_general_when_policy_excludes_it():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    provider = FakeProvider(conn, index_general=False)
    provider._vector_store.records["general-1"] = {"id": "general-1", "target": "general", "updated_at": "old"}

    upsert_vector_record(
        provider,
        id="general-1",
        source="turn-user",
        target="general",
        content="general scratch should not be indexed",
        summary="general scratch should not be indexed",
        updated_at="2026-05-01T00:00:00+00:00",
        scope_id="local-scope",
    )

    assert "general-1" not in provider._vector_store.records
    assert provider._vector_store.deleted == ["general-1"]
    assert provider._embedder.embedded_texts == []


def test_general_can_be_indexed_only_when_explicitly_enabled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="general-1", target="general", content="general scratch explicitly indexed")
    provider = FakeProvider(conn, index_general=True)

    count = sync_vector_index(provider)

    assert count == 1
    assert set(provider._vector_store.records) == {"general-1"}
    assert provider._embedder.embedded_texts
