"""Read-only doctor telemetry for temporal facts and reflection debt."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scope_recall.doctor_temporal import temporal_evolution_report
from scope_recall.fact_repository import insert_claim
from scope_recall.sql_store import ensure_schema

ROOT = Path(__file__).resolve().parents[1]


def _home(tmp_path: Path, *, with_schema: bool = True) -> tuple[Path, sqlite3.Connection]:
    home = tmp_path / "hermes"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if with_schema:
        ensure_schema(conn)
    else:
        conn.execute("CREATE TABLE memories(id TEXT PRIMARY KEY, metadata TEXT NOT NULL DEFAULT '{}')")
        conn.commit()
    return home, conn


def _memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    metadata: Mapping[str, Any],
) -> None:
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'test', 'memory', ?, ?, ?, ?, ?)
        """,
        (memory_id, memory_id, memory_id, now, now, json.dumps(metadata)),
    )


def _drop_fact_unique_indexes(conn: sqlite3.Connection) -> None:
    for row in conn.execute("PRAGMA index_list(fact_claims)").fetchall():
        if int(row[2]) and str(row[3]) == "c":
            conn.execute(f'DROP INDEX "{row[1]}"')


def _clone_claim(
    conn: sqlite3.Connection,
    source_claim_id: str,
    *,
    claim_id: str,
    memory_id: str,
    source_type: str,
    source_ref: str,
) -> None:
    source = conn.execute(
        "SELECT * FROM fact_claims WHERE claim_id = ?",
        (source_claim_id,),
    ).fetchone()
    assert source is not None
    columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(fact_claims)")]
    values = {column: source[column] for column in columns}
    values.update(
        {
            "claim_id": claim_id,
            "memory_id": memory_id,
            "source_type": source_type,
            "source_ref": source_ref,
        }
    )
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO fact_claims({','.join(columns)}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )


def test_temporal_doctor_clean_report_is_read_only(tmp_path: Path) -> None:
    home, conn = _home(tmp_path)
    _memory(
        conn,
        "memory-one",
        metadata={"lifecycle": "promoted", "memory_type": "factual"},
    )
    insert_claim(
        conn,
        claim_id="claim-one",
        memory_id="memory-one",
        scope_id="scope-a",
        subject="Aurora",
        predicate="database",
        value="PostgreSQL",
        valid_from="2025-01-01T00:00:00+00:00",
        recorded_at="2025-01-02T00:00:00+00:00",
        source_type="user_message",
        source_ref="message-one",
        confidence=0.99,
    )
    conn.commit()
    before = conn.total_changes

    payload, check, recommendations = temporal_evolution_report(
        home,
        {
            "fact_evolution": {"enabled": True},
            "temporal_queries": {"enabled": True},
            "reflection": {"enabled": True, "write_candidates": False},
        },
    )

    assert conn.total_changes == before
    assert check == {"ok": True, "failures": []}
    assert payload["status"] == "ready"
    assert payload["write_delta"] == 0
    assert payload["claim_coverage"] == {
        "eligible_memory_count": 1,
        "claimed_memory_count": 1,
        "coverage_rate": 1.0,
    }
    assert payload["single_current_overlap_groups"] == 0
    assert payload["open_interval_conflict_groups"] == 0
    assert payload["claims_without_source"] == 0
    assert payload["successor_integrity"]["violation_count"] == 0
    assert payload["successor_integrity"]["cycle_component_count"] == 0
    assert payload["fact_fts_integrity"]["current"] is True
    assert payload["fact_fts_integrity"]["membership_sets_checked"] is True
    assert all(
        summary["count"] == 0
        for summary in payload["fact_fts_integrity"]["set_differences"].values()
    )
    assert recommendations == []
    conn.close()


def test_temporal_doctor_recursively_detects_decontented_successor_cycle(
    tmp_path: Path,
) -> None:
    home, conn = _home(tmp_path)
    for suffix in ("a", "b", "c"):
        memory_id = f"memory-cycle-{suffix}"
        claim_id = f"claim-cycle-{suffix}"
        _memory(
            conn,
            memory_id,
            metadata={"lifecycle": "promoted", "memory_type": "factual"},
        )
        insert_claim(
            conn,
            claim_id=claim_id,
            memory_id=memory_id,
            scope_id="scope-a",
            subject="Aurora",
            predicate="database",
            value=f"value-{suffix}",
            cardinality="multi",
            recorded_at="2026-01-02T00:00:00+00:00",
            source_type="user_message",
            source_ref=f"message-{suffix}",
            confidence=0.99,
        )
    for predecessor, successor in (("a", "b"), ("b", "c"), ("c", "a")):
        conn.execute(
            "UPDATE fact_claims SET superseded_by_claim_id = ? WHERE claim_id = ?",
            (f"claim-cycle-{successor}", f"claim-cycle-{predecessor}"),
        )
    conn.execute(
        """
        UPDATE fact_claims
        SET status = 'superseded', retired_at = '2026-02-01T00:00:00+00:00'
        WHERE claim_id LIKE 'claim-cycle-%'
        """
    )
    conn.commit()
    before = conn.total_changes

    payload, check, recommendations = temporal_evolution_report(
        home,
        {"fact_evolution": {"enabled": True}},
    )

    assert conn.total_changes == before
    assert check["ok"] is False
    assert any("successor chain invariant" in item for item in check["failures"])
    integrity = payload["successor_integrity"]
    assert integrity["checked_claim_count"] == 3
    assert integrity["checked_edge_count"] == 3
    assert integrity["cycle_component_count"] == 1
    assert integrity["cycle_claim_count"] == 3
    assert integrity["violation_count"] >= 1
    assert "claim-cycle-" not in json.dumps(integrity, sort_keys=True)
    assert payload["write_delta"] == 0
    assert any("repair plan" in item for item in recommendations)
    conn.close()


def test_temporal_doctor_rejects_equal_count_different_fts_membership(
    tmp_path: Path,
) -> None:
    home, conn = _home(tmp_path)
    for memory_id, subject in (
        ("memory-one", "Aurora"),
        ("memory-two", "Astra"),
    ):
        _memory(
            conn,
            memory_id,
            metadata={"lifecycle": "promoted", "memory_type": "factual"},
        )
        insert_claim(
            conn,
            claim_id=f"claim-{memory_id}",
            memory_id=memory_id,
            scope_id="scope-a",
            subject=subject,
            predicate="database",
            value="PostgreSQL",
            valid_from="2025-01-01T00:00:00+00:00",
            recorded_at="2025-01-02T00:00:00+00:00",
            source_type="user_message",
            source_ref=f"message-{memory_id}",
            confidence=0.99,
        )
    conn.execute(
        "DELETE FROM fact_claims_fts WHERE claim_id = 'claim-memory-one'"
    )
    conn.execute(
        """
        INSERT INTO fact_claims_fts(
            claim_id, memory_id, subject_key, predicate_key, value, memory_text
        ) VALUES (
            'orphan-claim', 'orphan-memory', 'orphan', 'database',
            'orphan-value', 'orphan text'
        )
        """
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fact_claims_fts").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(DISTINCT claim_id) FROM fact_claims_fts"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM fact_claims_fts_membership"
    ).fetchone()[0] == 2
    before = conn.total_changes

    payload, check, recommendations = temporal_evolution_report(
        home,
        {"temporal_queries": {"enabled": True}},
    )

    assert conn.total_changes == before
    assert check["ok"] is False
    assert any("fact FTS membership is not current" in item for item in check["failures"])
    integrity = payload["fact_fts_integrity"]
    assert integrity["claim_count"] == 2
    assert integrity["fts_row_count"] == 2
    assert integrity["fts_distinct_claim_count"] == 2
    assert integrity["membership_count"] == 2
    assert integrity["current"] is False
    assert integrity["set_differences"]["claims_missing_from_fts"] == {
        "count": 1,
        "sample_claim_ids": ["claim-memory-one"],
        "sample_truncated": False,
    }
    assert integrity["set_differences"]["fts_orphans"] == {
        "count": 1,
        "sample_claim_ids": ["orphan-claim"],
        "sample_truncated": False,
    }
    assert integrity["set_differences"]["claims_missing_from_membership"][
        "count"
    ] == 0
    assert integrity["set_differences"]["membership_orphans"]["count"] == 0
    assert recommendations
    conn.close()


def test_temporal_doctor_detects_conflicts_provenance_and_review_debt(
    tmp_path: Path,
) -> None:
    home, conn = _home(tmp_path)
    for memory_id, metadata in (
        ("memory-one", {"lifecycle": "promoted", "memory_type": "factual"}),
        ("memory-two", {"lifecycle": "promoted", "memory_type": "factual"}),
        (
            "candidate-review",
            {
                "lifecycle": "candidate",
                "memory_type": "factual",
                "candidate_status": "needs_review",
                "fact_evolution": {"action": "review"},
            },
        ),
        (
            "mental-model",
            {
                "lifecycle": "candidate",
                "memory_type": "mental_model",
                "candidate_status": "needs_review",
            },
        ),
    ):
        _memory(conn, memory_id, metadata=metadata)
    insert_claim(
        conn,
        claim_id="claim-one",
        memory_id="memory-one",
        scope_id="scope-a",
        subject="Aurora",
        predicate="database",
        value="PostgreSQL",
        valid_from="2025-01-01T00:00:00+00:00",
        recorded_at="2025-01-02T00:00:00+00:00",
        source_type="user_message",
        source_ref="message-one",
        confidence=0.99,
    )
    _drop_fact_unique_indexes(conn)
    _clone_claim(
        conn,
        "claim-one",
        claim_id="claim-two",
        memory_id="memory-two",
        source_type="",
        source_ref="",
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO fact_action_receipts(
            action_id, idempotency_key, request_hash, scope_id,
            requested_action, effective_action, status, applied,
            policy_json, receipt_json, error, created_at, updated_at
        ) VALUES (
            'review-one', 'review-idem', 'review-hash', 'scope-a',
            'supersede', 'review', 'review', 0,
            '{}', '{}', '', ?, ?
        )
        """,
        (now, now),
    )
    conn.commit()
    before = conn.total_changes

    payload, check, recommendations = temporal_evolution_report(
        home,
        {"fact_evolution": {"enabled": True}},
    )

    assert conn.total_changes == before
    assert check["ok"] is False
    assert payload["status"] == "needs_repair"
    assert payload["single_current_overlap_groups"] == 1
    assert payload["open_interval_conflict_groups"] == 1
    assert payload["claims_without_source"] == 1
    assert payload["evolution_review_debt"] == {
        "receipt_review_count": 1,
        "candidate_review_count": 2,
        "total": 3,
    }
    assert payload["mental_model_candidate_debt"] == 1
    assert payload["recent_evolution"]["review"] == 1
    assert payload["write_delta"] == 0
    assert recommendations
    conn.close()


def test_temporal_doctor_schema_missing_respects_feature_gate(tmp_path: Path) -> None:
    home, conn = _home(tmp_path, with_schema=False)
    conn.close()

    disabled_payload, disabled_check, _ = temporal_evolution_report(home, {})
    enabled_payload, enabled_check, _ = temporal_evolution_report(
        home,
        {"temporal_queries": {"enabled": True}},
    )

    assert disabled_payload["status"] == "schema_missing"
    assert disabled_check["ok"] is True
    assert enabled_payload["status"] == "schema_missing"
    assert enabled_check["ok"] is False


def test_doctor_cli_includes_temporal_evolution_runtime_section(tmp_path: Path) -> None:
    home, conn = _home(tmp_path)
    conn.close()
    config_path = home / "scope-recall" / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {"enabled": False},
                "fact_evolution": {"enabled": False},
                "temporal_queries": {"enabled": False},
                "reflection": {"enabled": False, "write_candidates": False},
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "doctor.py"),
            "--json",
            "--source-root",
            str(ROOT),
            "--hermes-home",
            str(home),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert completed.returncode in {0, 1}
    assert "temporal_evolution" in report["runtime"]
    assert "temporal_evolution" in report["checks"]
    assert report["runtime"]["temporal_evolution"]["write_delta"] == 0
