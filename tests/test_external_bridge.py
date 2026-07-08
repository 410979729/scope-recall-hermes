"""Tests for the external shared-memory bridge contract."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scope_recall.external_bridge import (
    DURABLE_EXPORT_TARGETS,
    EXPORT_SCHEMA_VERSION,
    build_external_memory_export,
    validate_conflict_policy,
)
from scope_recall.sql_store import ensure_schema, store_row


def _metadata(**values) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    target: str,
    content: str,
    scope_id: str = "scope-a",
    source: str = "tool-store",
    metadata: dict | None = None,
) -> None:
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
        source=source,
        target=target,
        content=content,
        metadata=_metadata(**(metadata or {})),
        allow_duplicate=True,
    )
    if metadata is not None:
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (_metadata(**metadata), memory_id))
        conn.commit()


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_external_export_requires_explicit_conflict_policy():
    assert validate_conflict_policy("manual_review") == "manual_review"
    assert validate_conflict_policy("prefer_local") == "prefer_local"
    assert validate_conflict_policy("prefer_newer") == "prefer_newer"
    with pytest.raises(ValueError, match="conflict_policy is required"):
        validate_conflict_policy("")
    with pytest.raises(ValueError, match="conflict_policy is required"):
        validate_conflict_policy("overwrite_remote")


def test_external_export_includes_only_durable_targets_and_preserves_provenance(tmp_path: Path):
    conn = _make_conn(tmp_path)
    try:
        _store(
            conn,
            memory_id="user-1",
            target="user",
            content="User prefers concise Chinese progress updates.",
            metadata={
                "lifecycle": "promoted",
                "memory_type": "preference",
                "trust": 0.8,
                "importance": 0.9,
                "source_trust": 0.7,
                "entities": ["joy"],
                "tags": ["preference"],
            },
        )
        _store(conn, memory_id="general-1", target="general", content="Temporary scratch must stay local.")

        payload = build_external_memory_export(conn, accessible_scope_ids=["scope-a"], conflict_policy="manual_review")
    finally:
        conn.close()

    assert payload["schema_version"] == EXPORT_SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["allowed_targets"] == list(DURABLE_EXPORT_TARGETS)
    assert payload["count"] == 1
    record = payload["records"][0]
    assert record["id"] == "user-1"
    assert record["target"] == "user"
    assert record["conflict_policy"] == "manual_review"
    assert record["metadata"]["memory_type"] == "preference"
    assert record["metadata"]["entities"] == ["joy"]
    assert record["provenance"]["scope_id"] == "scope-a"
    assert record["provenance"]["source"] == "tool-store"
    assert record["provenance"]["source_trust"] == 0.7
    assert "general-1" not in {item["id"] for item in payload["records"]}


def test_external_export_skips_hidden_lifecycle_and_sensitive_rows(tmp_path: Path):
    conn = _make_conn(tmp_path)
    try:
        _store(conn, memory_id="active-1", target="memory", content="A reviewed durable fact.", metadata={"lifecycle": "promoted"})
        _store(conn, memory_id="candidate-1", target="memory", content="Candidate should not export.", metadata={"lifecycle": "candidate"})
        _store(conn, memory_id="archived-1", target="ops", content="Archived should not export.", metadata={"lifecycle": "archived"})
        _store(conn, memory_id="secret-1", target="project", content="Secret reference should not export.", metadata={"sensitivity": "secret_reference", "vault_ref": "vault://scope/service"})
        _store(conn, memory_id="restricted-1", target="user", content="Restricted should not export.", metadata={"sensitivity": "restricted"})

        payload = build_external_memory_export(conn, accessible_scope_ids=["scope-a"], conflict_policy="prefer_local")
    finally:
        conn.close()

    assert [record["id"] for record in payload["records"]] == ["active-1"]
    skipped = {item["id"]: item["reason"] for item in payload["skipped"]["items"]}
    assert "candidate-1" not in {record["id"] for record in payload["records"]}
    assert "archived-1" not in {record["id"] for record in payload["records"]}
    assert skipped["secret-1"] == "sensitivity:secret_reference"
    assert skipped["restricted-1"] == "sensitivity:restricted"


def test_external_export_applies_hidden_lifecycle_filter_before_limit(tmp_path: Path):
    conn = _make_conn(tmp_path)
    try:
        _store(conn, memory_id="visible-old", target="memory", content="Visible durable fact.", metadata={"lifecycle": "promoted"})
        _store(conn, memory_id="candidate-new", target="memory", content="New hidden candidate.", metadata={"lifecycle": "candidate"})
        conn.execute("UPDATE memories SET updated_at = '2026-07-07T10:00:00Z' WHERE id = 'visible-old'")
        conn.execute("UPDATE memories SET updated_at = '2026-07-07T11:00:00Z' WHERE id = 'candidate-new'")
        conn.commit()

        payload = build_external_memory_export(conn, accessible_scope_ids=["scope-a"], conflict_policy="manual_review", limit=1)
    finally:
        conn.close()

    assert payload["count"] == 1
    assert [record["id"] for record in payload["records"]] == ["visible-old"]


def test_external_export_respects_scope_and_target_filters(tmp_path: Path):
    conn = _make_conn(tmp_path)
    try:
        _store(conn, memory_id="project-a", target="project", content="Project A fact.", scope_id="scope-a")
        _store(conn, memory_id="project-b", target="project", content="Project B fact.", scope_id="scope-b")
        _store(conn, memory_id="ops-a", target="ops", content="Ops A fact.", scope_id="scope-a")

        payload = build_external_memory_export(
            conn,
            accessible_scope_ids=["scope-a"],
            conflict_policy="prefer_newer",
            targets=["project"],
        )
    finally:
        conn.close()

    assert payload["conflict_policy"] == "prefer_newer"
    assert [record["id"] for record in payload["records"]] == ["project-a"]


def test_external_export_runs_on_query_only_connection_without_mutation(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _store(conn, memory_id="memory-1", target="memory", content="Read-only export fact.")
        before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()

    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro_conn.row_factory = sqlite3.Row
    try:
        ro_conn.execute("PRAGMA query_only = ON")
        payload = build_external_memory_export(ro_conn, accessible_scope_ids=["scope-a"], conflict_policy="manual_review")
        after = ro_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        ro_conn.close()

    assert payload["count"] == 1
    assert before == after == 1


def test_external_export_can_record_governance_audit_event(tmp_path: Path):
    conn = _make_conn(tmp_path)
    try:
        _store(conn, memory_id="memory-1", target="memory", content="Audited export fact.")
        payload = build_external_memory_export(
            conn,
            accessible_scope_ids=["scope-a"],
            conflict_policy="manual_review",
            record_audit=True,
            actor="pytest",
            batch_id="batch-1",
        )
        audit_row = conn.execute(
            "SELECT event_type, action, batch_id, actor FROM governance_audit_events WHERE id = ?",
            (payload["audit_event_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert payload["read_only"] is False
    assert audit_row["event_type"] == "external_memory_export"
    assert audit_row["action"] == "export"
    assert audit_row["batch_id"] == "batch-1"
    assert audit_row["actor"] == "pytest"
