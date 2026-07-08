"""Tests for the optional PGVector companion backend.

These tests use fake psycopg/pgvector modules so the optional dependency remains
absent in default development and CI environments.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from scope_recall.pgvector_store import PGVectorStore
from scope_recall.vector_store import VectorRecord, build_vector_store


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.statements.append((" ".join(sql.split()), params))
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("insert into"):
            memory_id, scope_id, source, target, content, summary, updated_at, vector = params
            self.conn.records[str(memory_id)] = {
                "id": str(memory_id),
                "scope_id": str(scope_id),
                "source": str(source),
                "target": str(target),
                "content": str(content),
                "summary": str(summary),
                "updated_at": str(updated_at),
                "vector": list(vector),
            }
            self.rows = []
            return
        if normalized.startswith("delete from"):
            for memory_id in params[0]:
                self.conn.records.pop(str(memory_id), None)
            self.rows = []
            return
        if normalized.startswith("select count(*)"):
            self.rows = [(len(self.conn.records),)]
            return
        if normalized.startswith("select id from"):
            self.rows = [(memory_id,) for memory_id in sorted(self.conn.records)]
            return
        if "as distance" in normalized:
            query_vector, scope_id, _order_vector, limit = params
            hits = []
            for record in self.conn.records.values():
                if record["scope_id"] != str(scope_id):
                    continue
                distance = sum((float(left) - float(right)) ** 2 for left, right in zip(query_vector, record["vector"]))
                hits.append(
                    (
                        record["id"],
                        record["scope_id"],
                        record["source"],
                        record["target"],
                        record["content"],
                        record["summary"],
                        record["updated_at"],
                        distance,
                    )
                )
            self.rows = sorted(hits, key=lambda row: row[7])[: int(limit)]
            return
        if normalized.startswith("select id, scope_id"):
            self.rows = [
                (
                    record["id"],
                    record["scope_id"],
                    record["source"],
                    record["target"],
                    record["content"],
                    record["summary"],
                    record["updated_at"],
                )
                for record in self.conn.records.values()
            ]
            return
        self.rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def install_fake_pgvector_modules(monkeypatch: pytest.MonkeyPatch, conn: FakeConnection) -> None:
    psycopg_module = types.ModuleType("psycopg")
    psycopg_module.connect = lambda dsn: conn  # type: ignore[attr-defined]
    pgvector_package = types.ModuleType("pgvector")
    pgvector_package.__path__ = []  # type: ignore[attr-defined]
    pgvector_psycopg_module = types.ModuleType("pgvector.psycopg")
    pgvector_psycopg_module.register_vector = lambda connection: setattr(connection, "vector_registered", True)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "pgvector", pgvector_package)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg", pgvector_psycopg_module)


def test_pgvector_store_requires_dsn_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCOPE_RECALL_PGVECTOR_DSN", raising=False)
    store = PGVectorStore(dimensions=3)

    assert store.backend == "pgvector"
    assert store.is_available() is False
    with pytest.raises(RuntimeError, match="SCOPE_RECALL_PGVECTOR_DSN"):
        store.open()


def test_pgvector_store_rejects_unsafe_table_names():
    with pytest.raises(ValueError, match="table_name"):
        PGVectorStore(table_name="vectors; drop table memories", dimensions=3)


def test_vector_store_factory_builds_pgvector_with_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCOPE_RECALL_TEST_PGVECTOR_DSN", raising=False)
    store = build_vector_store(
        "pgvector",
        storage_dir=tmp_path,
        table_name="memories",
        dimensions=3,
        metric="cosine",
        config={"pgvector": {"dsn_env": "SCOPE_RECALL_TEST_PGVECTOR_DSN", "table_name": "scope_vectors"}},
    )

    assert isinstance(store, PGVectorStore)
    assert store.dsn_env == "SCOPE_RECALL_TEST_PGVECTOR_DSN"
    assert store.table_name == "scope_vectors"
    assert store.is_available() is False


def test_pgvector_store_protocol_with_fake_driver(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConnection()
    install_fake_pgvector_modules(monkeypatch, conn)
    monkeypatch.setenv("SCOPE_RECALL_TEST_PGVECTOR_DSN", "postgresql://example/scope_recall")
    store = PGVectorStore(dsn_env="SCOPE_RECALL_TEST_PGVECTOR_DSN", table_name="scope_vectors", dimensions=3)

    assert store.is_available() is True
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
                vector=[1.0, 0.0, 0.0],
            )
        )
        store.upsert_records(
            [
                {
                    "id": "m2",
                    "scope_id": "scope-a",
                    "source": "unit-test",
                    "target": "memory",
                    "content": "beta",
                    "summary": "beta summary",
                    "updated_at": "2026-01-01T00:00:01+00:00",
                    "vector": [0.0, 1.0, 0.0],
                }
            ]
        )

        assert store.count_rows() == 2
        assert store.audit_counts()["unique_ids"] == 2
        assert set(store.list_records()) == {"m1", "m2"}
        hits = store.search([1.0, 0.0, 0.0], scope_id="scope-a", limit=1)
        assert hits[0]["id"] == "m1"
        assert store.delete(["m2"]) == 1
        assert store.list_ids() == ["m1"]
    finally:
        store.close()

    assert conn.closed is True


def test_pgvector_store_validates_vector_dimensions(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConnection()
    install_fake_pgvector_modules(monkeypatch, conn)
    monkeypatch.setenv("SCOPE_RECALL_TEST_PGVECTOR_DSN", "postgresql://example/scope_recall")
    store = PGVectorStore(dsn_env="SCOPE_RECALL_TEST_PGVECTOR_DSN", dimensions=3)
    store.open()
    try:
        with pytest.raises(ValueError, match="expected 3"):
            store.upsert_records(
                [
                    {
                        "id": "bad",
                        "scope_id": "scope-a",
                        "source": "unit-test",
                        "target": "memory",
                        "content": "bad",
                        "summary": "bad",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "vector": [1.0, 2.0],
                    }
                ]
            )
    finally:
        store.close()
