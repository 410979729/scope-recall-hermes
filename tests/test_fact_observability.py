"""Focused, read-only observability tests for structured Fact adoption."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.fact_observability import fact_observability_report
from scope_recall.fact_repository import insert_claim, link_claim_evidence
from scope_recall.sql_store import ensure_schema


def _memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    metadata: str | dict[str, str],
) -> None:
    now = "2026-08-30T00:00:00+00:00"
    encoded = metadata if isinstance(metadata, str) else json.dumps(metadata)
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'test', 'memory', ?, ?, ?, ?, ?)
        """,
        (memory_id, memory_id, memory_id, now, now, encoded),
    )


def test_fact_observability_separates_feature_gate_from_adoption() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    before = conn.total_changes

    report = fact_observability_report(
        conn,
        {
            "fact_evolution": {"enabled": True},
            "fact_backfill": {"shadow_enabled": False},
        },
    )

    assert conn.total_changes == before
    assert report["feature_enabled"] is True
    assert report["effective_modes"] == {
        "default": "preview",
        "nightly": "preview",
        "journal": "preview",
        "tool": "preview",
        "maintenance": "preview",
    }
    assert report["state"] == "preview_no_claims"
    assert report["claim_count"] == 0
    assert report["coverage_ratio"] == 1.0
    conn.close()


def test_fact_observability_reports_real_coverage_without_writes() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    _memory(
        conn,
        "memory-one",
        metadata={"lifecycle": "promoted", "memory_type": "factual"},
    )
    _memory(
        conn,
        "memory-two",
        metadata={"lifecycle": "promoted", "memory_type": "preference"},
    )
    for lifecycle in ("candidate", "scratch", "in_progress"):
        _memory(
            conn,
            f"hidden-{lifecycle}",
            metadata={"lifecycle": lifecycle, "memory_type": "factual"},
        )
    # Malformed historical metadata must not make a read-only status endpoint fail.
    _memory(conn, "malformed-history", metadata="{not-json")
    insert_claim(
        conn,
        claim_id="claim-one",
        memory_id="memory-one",
        scope_id="scope-a",
        subject="Aurora",
        predicate="database",
        value="PostgreSQL",
        recorded_at="2026-08-29T00:00:00+00:00",
        source_type="user_message",
        source_ref="message-one",
        confidence=0.99,
    )
    link_claim_evidence(
        conn,
        claim_id="claim-one",
        source_type="user_message",
        source_ref="message-one",
        recorded_at="2026-08-29T00:00:00+00:00",
    )
    applied_at = "2026-08-30T01:02:03+00:00"
    conn.execute(
        """
        INSERT INTO fact_action_receipts(
            action_id, idempotency_key, request_hash, scope_id,
            requested_action, effective_action, status, applied,
            policy_json, receipt_json, error, created_at, updated_at
        ) VALUES (
            'apply-one', 'apply-idem', 'apply-hash', 'scope-a',
            'add', 'add', 'applied', 1,
            '{}', '{}', '', ?, ?
        )
        """,
        (applied_at, applied_at),
    )
    conn.commit()
    before = conn.total_changes

    report = fact_observability_report(
        conn,
        {
            "fact_evolution": {
                "enabled": True,
                "mode": "auto_apply",
                "nightly_mode": "auto_apply",
                "journal_mode": "auto_apply",
                "tool_mode": "auto_apply",
                "maintenance_mode": "preview",
            },
            "fact_backfill": {"shadow_enabled": True},
        },
    )

    assert conn.total_changes == before
    assert report["state"] == "active_partial_coverage"
    assert report["claim_count"] == 1
    assert report["current_claim_count"] == 1
    assert report["projection_count"] == 1
    assert report["evidence_count"] == 1
    assert report["fact_owned_memory_count"] == 1
    assert report["eligible_memory_count"] == 2
    assert report["claimed_memory_count"] == 1
    assert report["coverage_ratio"] == 0.5
    assert report["backfill_shadow_enabled"] is True
    assert report["last_apply_at"] == applied_at
    conn.close()


def test_fact_observability_retired_history_is_not_current_adoption() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    _memory(
        conn,
        "memory-retired",
        metadata={"lifecycle": "promoted", "memory_type": "factual"},
    )
    insert_claim(
        conn,
        claim_id="claim-retired",
        memory_id="memory-retired",
        scope_id="scope-a",
        subject="Aurora",
        predicate="database",
        value="PostgreSQL",
        recorded_at="2026-08-29T00:00:00+00:00",
        source_type="user_message",
        source_ref="message-retired",
        confidence=0.99,
    )
    conn.execute(
        """
        UPDATE fact_claims
        SET status = 'retracted', retired_at = '2026-08-30T00:00:00+00:00'
        WHERE claim_id = 'claim-retired'
        """
    )
    conn.commit()
    before = conn.total_changes

    report = fact_observability_report(
        conn,
        {
            "fact_evolution": {"enabled": True, "mode": "auto_apply"},
            "fact_backfill": {"shadow_enabled": False},
        },
    )

    assert conn.total_changes == before
    assert report["claim_count"] == 1
    assert report["current_claim_count"] == 0
    assert report["claimed_memory_count"] == 0
    assert report["eligible_memory_count"] == 1
    assert report["coverage_ratio"] == 0.0
    assert report["state"] == "active_no_claims"
    conn.close()
