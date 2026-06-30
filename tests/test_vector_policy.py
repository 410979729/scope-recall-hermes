"""Tests for vector indexing policy around general rows and hidden lifecycles.

They ensure vector companion cleanup follows SQLite visibility rules."""

from __future__ import annotations

import json
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

    def embed_query(self, text):
        return self.embed(text)

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


class LockSpy:
    def __init__(self):
        self.active = False
        self.entered = 0

    def __enter__(self):
        self.active = True
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.active = False
        return False


class LockAssertingVectorStore(FakeVectorStore):
    def __init__(self, lock):
        super().__init__()
        self.lock = lock

    def delete_by_ids(self, ids):
        assert self.lock.active
        super().delete_by_ids(ids)

    def upsert_records(self, rows):
        assert self.lock.active
        super().upsert_records(rows)

    def repair_records(self, desired_records):
        assert self.lock.active
        return super().repair_records(desired_records)

    def audit_counts(self):
        assert self.lock.active
        return super().audit_counts()


class FakeProvider:
    def __init__(self, conn, *, index_general=False):
        self._conn = conn
        self._lock = __import__("threading").RLock()
        self._vector_config = {"index_general": index_general}
        self._vector_ready = True
        self._vector_store = FakeVectorStore()
        self._vector_lock = self._lock
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


def _set_lifecycle(conn, memory_id: str, lifecycle: str) -> None:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    metadata = json.loads(str(row["metadata"] or "{}"))
    metadata["lifecycle"] = lifecycle
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


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


def test_sync_vector_index_excludes_lifecycle_hidden_and_deletes_stale_vectors():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="active-1", target="memory", scope_id="shared-scope", content="active durable memory should be indexed")
    _insert(conn, memory_id="archived-1", target="memory", scope_id="shared-scope", content="archived durable memory should not be indexed")
    _set_lifecycle(conn, "archived-1", "archived")
    provider = FakeProvider(conn, index_general=False)
    provider._vector_store.records["archived-1"] = {"id": "archived-1", "target": "memory", "updated_at": "old"}

    count = sync_vector_index(provider)

    assert count == 1
    assert set(provider._vector_store.records) == {"active-1"}
    assert "archived-1" in provider._vector_store.deleted
    assert all("archived durable" not in text for text in provider._embedder.embedded_texts)


def test_upsert_vector_record_deletes_existing_lifecycle_hidden_vector():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="archived-1", target="memory", scope_id="shared-scope", content="archived durable memory should not be indexed")
    _set_lifecycle(conn, "archived-1", "archived")
    provider = FakeProvider(conn, index_general=False)
    provider._vector_store.records["archived-1"] = {"id": "archived-1", "target": "memory", "updated_at": "old"}

    upsert_vector_record(
        provider,
        id="archived-1",
        source="tool-store",
        target="memory",
        content="archived durable memory should not be indexed",
        summary="archived durable memory should not be indexed",
        updated_at="2026-05-01T00:00:00+00:00",
        scope_id="shared-scope",
    )

    assert "archived-1" not in provider._vector_store.records
    assert provider._vector_store.deleted == ["archived-1"]
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


def test_upsert_vector_record_mutates_store_under_vector_lock():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    provider = FakeProvider(conn, index_general=False)
    lock = LockSpy()
    provider._vector_lock = lock
    provider._vector_store = LockAssertingVectorStore(lock)

    upsert_vector_record(
        provider,
        id="memory-locked",
        source="tool-store",
        target="memory",
        content="durable memory should be indexed under lock",
        summary="durable memory should be indexed under lock",
        updated_at="2026-05-01T00:00:00+00:00",
        scope_id="shared-scope",
    )

    assert lock.entered >= 1
    assert "memory-locked" in provider._vector_store.records


