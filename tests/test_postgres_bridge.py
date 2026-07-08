"""Tests for optional PostgreSQL shared-memory bridge adapter."""

from __future__ import annotations

import json
import os
import sys
import types
from typing import Any

import pytest

from scope_recall.external_bridge import EXPORT_SCHEMA_VERSION
from scope_recall.postgres_bridge import DEFAULT_POSTGRES_BRIDGE_DSN_ENV, PostgresSharedMemoryBridge, build_postgres_schema_sql


class FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...] | None]]) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.commit_count = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls)

    def commit(self) -> None:
        self.commit_count += 1

    def close(self) -> None:
        self.closed = True


def _payload() -> dict[str, Any]:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "conflict_policy": "manual_review",
        "records": [
            {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "id": "memory-1",
                "target": "memory",
                "content": "A shared durable fact.",
                "summary": "A shared durable fact.",
                "metadata": {"memory_type": "factual", "trust": 0.8},
                "provenance": {
                    "scope_id": "scope-a",
                    "source": "tool-store",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "source_trust": 0.7,
                    "origin": "scope-recall-sqlite",
                },
                "conflict_policy": "manual_review",
            },
            {"id": ""},
            "not-a-record",
        ],
    }


def _fake_bridge(monkeypatch) -> tuple[PostgresSharedMemoryBridge, FakeConn]:
    fake_conn = FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda dsn: fake_conn))
    monkeypatch.setenv(DEFAULT_POSTGRES_BRIDGE_DSN_ENV, "postgresql://example/scope_recall")
    bridge = PostgresSharedMemoryBridge(table_name="scope_recall_shared_memories")
    return bridge, fake_conn


def test_postgres_schema_sql_uses_expected_table_and_indexes():
    schema = build_postgres_schema_sql(table_name="shared_memories")

    assert 'CREATE TABLE IF NOT EXISTS "shared_memories"' in schema
    assert "metadata JSONB" in schema
    assert "provenance JSONB" in schema
    assert "shared_memories_target_idx" in schema
    with pytest.raises(ValueError, match="simple SQL identifier"):
        build_postgres_schema_sql(table_name="shared;drop")


def test_postgres_bridge_is_optional_without_env_or_psycopg(monkeypatch):
    monkeypatch.delenv(DEFAULT_POSTGRES_BRIDGE_DSN_ENV, raising=False)
    bridge = PostgresSharedMemoryBridge()

    assert bridge.is_available() is False
    with pytest.raises(RuntimeError, match="DSN environment variable is not set"):
        bridge.open()


def test_postgres_bridge_open_ensures_schema_and_publish_payload(monkeypatch):
    bridge, fake_conn = _fake_bridge(monkeypatch)

    assert bridge.is_available() is True
    bridge.open()
    result = bridge.publish_export(_payload())
    bridge.close()

    assert result["ok"] is True
    assert result["schema_version"] == EXPORT_SCHEMA_VERSION
    assert result["table_name"] == "scope_recall_shared_memories"
    assert result["published"] == 1
    assert fake_conn.commit_count == 2
    assert fake_conn.closed is True
    schema_sql = fake_conn.calls[0][0]
    assert "CREATE TABLE IF NOT EXISTS" in schema_sql
    insert_sql, insert_params = fake_conn.calls[1]
    assert 'INSERT INTO "scope_recall_shared_memories"' in insert_sql
    assert "ON CONFLICT (id) DO UPDATE SET" in insert_sql
    assert insert_params is not None
    assert insert_params[0] == "memory-1"
    assert insert_params[2] == "memory"
    assert json.loads(insert_params[5]) == {"memory_type": "factual", "trust": 0.8}
    assert json.loads(insert_params[6])["scope_id"] == "scope-a"
    assert insert_params[7] == "manual_review"
    assert insert_params[8] == "scope-a"
    assert insert_params[9] == "2026-01-01T00:00:00+00:00"
    assert insert_params[10] == 0.7


def test_postgres_bridge_rejects_wrong_payload_schema(monkeypatch):
    bridge, _fake_conn = _fake_bridge(monkeypatch)
    bridge.open()
    try:
        with pytest.raises(ValueError, match="unsupported external export schema_version"):
            bridge.publish_export({"schema_version": "old", "records": []})
        with pytest.raises(ValueError, match="payload.records must be a list"):
            bridge.publish_export({"schema_version": EXPORT_SCHEMA_VERSION, "records": {}})
    finally:
        bridge.close()


@pytest.mark.skipif(not os.environ.get(DEFAULT_POSTGRES_BRIDGE_DSN_ENV), reason="requires a real PostgreSQL bridge DSN")
def test_postgres_bridge_real_connection_smoke():
    psycopg = pytest.importorskip("psycopg")
    del psycopg
    bridge = PostgresSharedMemoryBridge(table_name="scope_recall_shared_memories_smoke")
    bridge.open()
    try:
        result = bridge.publish_export(_payload())
    finally:
        bridge.close()
    assert result["published"] == 1
