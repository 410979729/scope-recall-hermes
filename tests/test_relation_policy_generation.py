from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import scope_recall.relation_frequency_maintenance as maintenance
import scope_recall.relation_policy_generation as relation_policy_generation
from scope_recall.durable_work import canonical_snapshot_hash
from scope_recall.relation_containment import (
    establish_relation_scope_baseline,
    record_relation_scope_target,
)
from scope_recall.relation_policy_generation import (
    RELATION_POLICY_VERSION,
    drain_relation_policy_generation,
    materialize_relation_policy_generation,
    relation_generation_descriptor,
    relation_pair_key,
    relation_policy_generation_report,
    relation_policy_generation_schema_status,
)
from scope_recall.relation_scope_state import blocked_entities_receipt_hash
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    entity: str = "database x",
) -> None:
    now = "2026-08-27T05:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, 'scope-a', 'fixture', 'project', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            content,
            now,
            now,
            json.dumps({"entities": [entity]}),
        ),
    )
    conn.execute(
        """
        INSERT INTO relation_entity_postings(scope_id, entity, memory_id)
        VALUES('scope-a', ?, ?)
        """,
        (entity, memory_id),
    )


def _stage_target(
    conn: sqlite3.Connection,
    *,
    old_blocked: set[str],
    new_blocked: set[str],
) -> tuple[int, int]:
    row = conn.execute(
        "SELECT corpus_revision FROM relation_scope_statistics WHERE scope_id='scope-a'"
    ).fetchone()
    baseline = int(row[0] or 0)
    old_json = json.dumps(sorted(old_blocked), separators=(",", ":"))
    old_hash = blocked_entities_receipt_hash("scope-a", baseline, old_blocked)
    conn.execute(
        """
        UPDATE relation_scope_statistics
        SET statistics_revision=?, blocked_entities_json=?,
            blocked_entities_sha256=?
        WHERE scope_id='scope-a'
        """,
        (baseline, old_json, old_hash),
    )
    establish_relation_scope_baseline(
        conn,
        scope_id="scope-a",
        revision=baseline,
        blocked_entities=old_blocked,
    )
    target = baseline + 1
    new_json = json.dumps(sorted(new_blocked), separators=(",", ":"))
    new_hash = blocked_entities_receipt_hash("scope-a", target, new_blocked)
    conn.execute(
        """
        UPDATE relation_scope_statistics
        SET corpus_revision=?, statistics_revision=?, blocked_entities_json=?,
            blocked_entities_sha256=?
        WHERE scope_id='scope-a'
        """,
        (target, target, new_json, new_hash),
    )
    record_relation_scope_target(
        conn,
        scope_id="scope-a",
        prior_statistics_revision=baseline,
        target_revision=target,
        old_blocked_entities=old_blocked,
        new_blocked_entities=new_blocked,
    )
    conn.commit()
    return baseline, target


def test_schema_exposes_exact_domain_objects_without_universal_table() -> None:
    conn = _conn()
    status = relation_policy_generation_schema_status(conn)
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert status["current"] is True
    assert {
        "relation_policy_generations",
        "relation_generation_items",
        "relation_edge_provenance",
    } <= tables
    assert "durable_jobs" not in tables
    conn.close()


def test_materialization_freezes_finite_pair_identity_and_item_set() -> None:
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="memory-a",
        content="Application A depends on database x for durable storage.",
    )
    _insert_memory(
        conn,
        memory_id="memory-b",
        content="Database x is the operational database service for Application A.",
    )
    _, target = _stage_target(
        conn, old_blocked={"database x"}, new_blocked=set()
    )

    result = materialize_relation_policy_generation(conn, candidate_cap=10)
    generation_id = str(result["generation_id"])
    generation = conn.execute(
        """
        SELECT policy_version, relation_revision, item_total, state,
               length(item_set_hash), old_blocked_entities_json,
               new_blocked_entities_json, delta_json
        FROM relation_policy_generations WHERE generation_id=?
        """,
        (generation_id,),
    ).fetchone()
    item = conn.execute(
        """
        SELECT item_ordinal, left_memory_id, right_memory_id, pair_key, state
        FROM relation_generation_items WHERE generation_id=?
        """,
        (generation_id,),
    ).fetchone()

    assert result["created"] is True
    assert generation["policy_version"] == RELATION_POLICY_VERSION
    assert generation["relation_revision"] == target
    assert generation["item_total"] == 1
    assert generation["state"] == "pending"
    assert generation["length(item_set_hash)"] == 64
    assert json.loads(generation["delta_json"]) == ["database x"]
    assert item["item_ordinal"] == 1
    assert item["pair_key"] == relation_pair_key(
        "scope-a", target, "memory-a", "memory-b"
    )
    assert item["state"] == "pending"
    descriptor = relation_generation_descriptor(conn, generation_id)
    assert descriptor is not None
    assert descriptor.frozen_upper_bound == 10

    with pytest.raises(sqlite3.IntegrityError, match="item set is immutable"):
        conn.execute(
            """
            INSERT INTO relation_generation_items(
                generation_id, item_id, item_ordinal, left_memory_id,
                right_memory_id, pair_key, state, max_attempts,
                created_at, updated_at
            ) VALUES(?, 'late', 2, 'memory-a', 'memory-z', 'late', 'pending',
                     5, ?, ?)
            """,
            (generation_id, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    with pytest.raises(sqlite3.IntegrityError, match="invalid.*transition"):
        conn.execute(
            "UPDATE relation_generation_items SET state='completed' WHERE generation_id=?",
            (generation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="invalid.*transition"):
        conn.execute(
            "UPDATE relation_policy_generations SET state='completed' WHERE generation_id=?",
            (generation_id,),
        )
    conn.close()


def test_generation_identity_items_and_provenance_are_sql_immutable() -> None:
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="memory-a",
        content="Application A depends on database x for durable storage.",
    )
    _insert_memory(
        conn,
        memory_id="memory-b",
        content="Database x is the operational database service for Application A.",
    )
    _stage_target(conn, old_blocked={"database x"}, new_blocked=set())
    created = materialize_relation_policy_generation(conn, candidate_cap=10)
    generation_id = str(created["generation_id"])

    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        conn.execute(
            "UPDATE relation_policy_generations "
            "SET authority_snapshot_json='{}' WHERE generation_id=?",
            (generation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="item identity is immutable"):
        conn.execute(
            "UPDATE relation_generation_items SET max_attempts=max_attempts+1 "
            "WHERE generation_id=?",
            (generation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="item history is immutable"):
        conn.execute(
            "DELETE FROM relation_generation_items WHERE generation_id=?",
            (generation_id,),
        )
    conn.execute(
        """
        INSERT INTO relation_edge_provenance(
            relation_identity, generation_id, policy_version, support_kind,
            support_entities_json, evidence_hash, reviewed, manual, created_at
        ) VALUES ('fixture-relation', ?, ?, 'fixture', '[]', ?, 0, 0,
                  '2026-08-27T05:00:00+00:00')
        """,
        (generation_id, RELATION_POLICY_VERSION, "a" * 64),
    )
    with pytest.raises(sqlite3.IntegrityError, match="provenance is immutable"):
        conn.execute(
            "DELETE FROM relation_edge_provenance WHERE generation_id=?",
            (generation_id,),
        )
    conn.commit()

    completed = drain_relation_policy_generation(
        conn,
        candidate_cap=10,
        wall_clock_seconds=5.0,
    )
    assert completed["status"] in {"ready", "degraded"}
    with pytest.raises(sqlite3.IntegrityError, match="receipt is immutable"):
        conn.execute(
            "UPDATE relation_generation_items SET receipt_json='{}' "
            "WHERE generation_id=?",
            (generation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="history is immutable"):
        conn.execute(
            "DELETE FROM relation_policy_generations WHERE generation_id=?",
            (generation_id,),
        )
    conn.close()


def test_cap_plus_one_blocks_whole_generation_without_partial_items() -> None:
    conn = _conn()
    for suffix in ("a", "b", "c"):
        _insert_memory(
            conn,
            memory_id=f"memory-{suffix}",
            content=f"Memory {suffix} refers to database x.",
        )
    _stage_target(conn, old_blocked={"database x"}, new_blocked=set())

    result = materialize_relation_policy_generation(conn, candidate_cap=1)
    item_count = conn.execute(
        "SELECT COUNT(*) FROM relation_generation_items"
    ).fetchone()[0]
    containment = conn.execute(
        "SELECT state, reason_code FROM relation_scope_containment WHERE scope_id='scope-a'"
    ).fetchone()

    assert result["status"] == "blocked"
    assert result["reason_code"] == "affected_candidate_cap_exceeded"
    assert result["item_total"] == 0
    assert item_count == 0
    assert containment["state"] == "blocked"
    conn.close()


def test_leased_generation_reaches_terminal_and_records_only_generated_provenance() -> None:
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="memory-a",
        content="Project Atlas deploy depends on Redis service availability.",
        entity="Redis service",
    )
    _insert_memory(
        conn,
        memory_id="memory-b",
        content="Redis service runbook: check redis-cli ping before Atlas deploy.",
        entity="Redis service",
    )
    conn.execute(
        """
        INSERT INTO memory_relations(
            source_memory_id, target_memory_id, relation_type,
            confidence, note, created_at
        ) VALUES('memory-a', 'memory-b', 'same_topic', 1.0,
                 'manual-reviewed-edge', '2026-08-27T05:00:00+00:00')
        """
    )
    _stage_target(conn, old_blocked={"Redis service"}, new_blocked=set())

    result = drain_relation_policy_generation(
        conn,
        candidate_cap=10,
        item_limit=1,
        wall_clock_seconds=5.0,
        lease_seconds=10,
    )
    generation = conn.execute(
        "SELECT state, cursor, item_total FROM relation_policy_generations"
    ).fetchone()
    item = conn.execute(
        "SELECT state, attempt, receipt_json FROM relation_generation_items"
    ).fetchone()
    provenance = conn.execute(
        "SELECT reviewed, manual FROM relation_edge_provenance"
    ).fetchall()
    manual = conn.execute(
        "SELECT note FROM memory_relations WHERE note='manual-reviewed-edge'"
    ).fetchone()

    assert result["status"] == "ready"
    assert generation["state"] == "completed"
    assert generation["cursor"] == generation["item_total"] == 1
    assert item["state"] == "completed"
    assert item["attempt"] == 1
    assert json.loads(item["receipt_json"])["generation_id"] == result["generation_id"]
    assert provenance
    assert all(row["reviewed"] == 0 and row["manual"] == 0 for row in provenance)
    assert manual is not None
    health = relation_policy_generation_report(conn)
    assert health["state"] == "ready"
    assert health["runnable_count"] == 0
    conn.close()


def test_item_set_drift_is_poisoned_before_any_relation_execution() -> None:
    conn = _conn()
    _insert_memory(conn, memory_id="memory-a", content="A uses database x.")
    _insert_memory(conn, memory_id="memory-b", content="Database x serves A.")
    _stage_target(conn, old_blocked={"database x"}, new_blocked=set())
    created = materialize_relation_policy_generation(conn, candidate_cap=10)
    conn.execute("DROP TRIGGER trg_relation_generation_item_identity_immutable")
    conn.execute(
        "UPDATE relation_generation_items SET pair_key='tampered'"
    )
    conn.commit()

    result = drain_relation_policy_generation(
        conn, candidate_cap=10, wall_clock_seconds=5.0
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "relation_generation_item_set_mismatch"
    assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0
    assert conn.execute(
        "SELECT state FROM relation_policy_generations WHERE generation_id=?",
        (created["generation_id"],),
    ).fetchone()[0] == "poisoned"
    assert conn.execute(
        "SELECT state FROM relation_generation_items WHERE generation_id=?",
        (created["generation_id"],),
    ).fetchone()[0] == "cancelled"
    conn.close()


def test_epoch_drift_after_preview_refuses_apply_and_supersedes(monkeypatch) -> None:
    conn = _conn()
    _insert_memory(conn, memory_id="memory-a", content="A uses database x.")
    _insert_memory(conn, memory_id="memory-b", content="Database x serves A.")
    _stage_target(conn, old_blocked={"database x"}, new_blocked=set())

    from scope_recall import relation_extraction

    real_rebuild = relation_extraction.rebuild_extracted_relations

    def drift_after_preview(*args, **kwargs):
        result = real_rebuild(*args, **kwargs)
        if kwargs.get("dry_run"):
            conn.execute(
                """
                UPDATE relation_scope_containment
                SET target_blocked_entities_sha256=?
                WHERE scope_id='scope-a'
                """,
                ("0" * 64,),
            )
        return result

    monkeypatch.setattr(
        relation_extraction, "rebuild_extracted_relations", drift_after_preview
    )
    result = drain_relation_policy_generation(
        conn, candidate_cap=10, wall_clock_seconds=5.0
    )

    assert result["status"] == "superseded"
    assert result["reason_code"] == "relation_generation_epoch_mismatch"
    assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0
    assert conn.execute(
        "SELECT state FROM relation_policy_generations"
    ).fetchone()[0] == "superseded"
    conn.close()


def test_retry_generation_respects_item_backoff_without_empty_reclaims(
    monkeypatch,
) -> None:
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="memory-a",
        content="Project Atlas deploy depends on Redis service availability.",
        entity="Redis service",
    )
    _insert_memory(
        conn,
        memory_id="memory-b",
        content="Redis service runbook: check redis-cli ping before Atlas deploy.",
        entity="Redis service",
    )
    _stage_target(conn, old_blocked={"Redis service"}, new_blocked=set())

    from scope_recall import relation_extraction

    def fail_rebuild(*args, **kwargs):
        raise RuntimeError("fixture dependency unavailable")

    monkeypatch.setattr(
        relation_extraction, "rebuild_extracted_relations", fail_rebuild
    )
    first = drain_relation_policy_generation(
        conn,
        candidate_cap=10,
        wall_clock_seconds=5.0,
        backoff_base_seconds=60.0,
        backoff_max_seconds=60.0,
    )
    generation_before = conn.execute(
        "SELECT attempts, not_before FROM relation_policy_generations"
    ).fetchone()
    second = drain_relation_policy_generation(
        conn,
        candidate_cap=10,
        wall_clock_seconds=5.0,
        backoff_base_seconds=60.0,
        backoff_max_seconds=60.0,
    )
    generation_after = conn.execute(
        "SELECT attempts, not_before FROM relation_policy_generations"
    ).fetchone()

    assert first["status"] == "retry"
    assert first["attempted"] == 1
    assert generation_before["not_before"]
    assert second["status"] == "retry"
    assert second["attempted"] == 0
    assert generation_after["attempts"] == generation_before["attempts"] == 1
    assert generation_after["not_before"] == generation_before["not_before"]
    conn.close()


def test_generation_rollback_clears_active_health_without_history_loss() -> None:
    conn = _conn()
    for suffix in ("a", "b", "c"):
        _insert_memory(
            conn,
            memory_id=f"memory-{suffix}",
            content=f"Memory {suffix} refers to database x.",
        )
    _stage_target(conn, old_blocked={"database x"}, new_blocked=set())
    blocked = materialize_relation_policy_generation(conn, candidate_cap=1)
    assert blocked["status"] == "blocked"
    assert relation_policy_generation_report(conn)["state"] == "blocked"

    unchanged = maintenance.drain_relation_frequency_work(
        conn,
        relation_candidate_cap=1,
        relation_policy_generation_enabled=False,
        wall_clock_seconds=5.0,
    )
    assert unchanged["containment"]["status"] == "idle"
    assert relation_policy_generation_report(conn)["state"] == "blocked"

    rollback = maintenance.drain_relation_frequency_work(
        conn,
        relation_candidate_cap=10,
        relation_policy_generation_enabled=False,
        wall_clock_seconds=5.0,
    )
    health = relation_policy_generation_report(conn)

    assert rollback["containment"]["status"] == "degraded"
    assert rollback["containment"]["reason_code"] == "focus_relation_sync_pending"
    assert health["state"] == "ready"
    assert health["active_generation_counts"] == {}
    assert health["generation_counts"]["blocked"] == 1
    conn.close()


def test_generation_rollback_supersedes_inflight_work_before_handoff() -> None:
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="memory-a",
        content="Application A depends on database x for durable storage.",
    )
    _insert_memory(
        conn,
        memory_id="memory-b",
        content="Database x is the operational database service for Application A.",
    )
    _, target = _stage_target(
        conn, old_blocked={"database x"}, new_blocked=set()
    )
    created = materialize_relation_policy_generation(conn, candidate_cap=10)
    assert created["status"] == "pending"

    rollback = maintenance.drain_relation_frequency_work(
        conn,
        relation_candidate_cap=10,
        relation_policy_generation_enabled=False,
        wall_clock_seconds=5.0,
    )
    generation = conn.execute(
        "SELECT state, reason_code FROM relation_policy_generations"
    ).fetchone()
    containment = conn.execute(
        "SELECT active_revision, target_revision FROM relation_scope_containment "
        "WHERE scope_id='scope-a'"
    ).fetchone()
    health = relation_policy_generation_report(conn)

    assert rollback["containment"]["attempted"] == 1
    assert rollback["containment"]["completed"] == 1
    assert generation["state"] == "superseded"
    assert generation["reason_code"] == "program0_rollback"
    assert containment["active_revision"] == containment["target_revision"] == target
    assert health["state"] == "ready"
    assert health["active_generation_counts"] == {}
    conn.close()


def test_supersede_helper_closes_building_generation() -> None:
    conn = _conn()
    now = "2026-08-27T05:00:00+00:00"
    conn.execute(
        """
        INSERT INTO relation_policy_generations(
            generation_id, scope_id, idempotency_key,
            scope_snapshot_json, authority_snapshot_json, policy_version,
            relation_revision, source_corpus_revision, frozen_upper_bound,
            old_blocked_entities_json, old_blocked_entities_sha256,
            new_blocked_entities_json, new_blocked_entities_sha256,
            delta_json, delta_sha256, item_set_hash, item_total,
            state, max_attempts, created_at, updated_at
        ) VALUES (
            'generation-building', 'scope-a', 'building-key', '{}', '{}', ?,
            1, 0, 10, '[]', ?, '[]', ?, '[]', ?, '', 0,
            'building', 3, ?, ?
        )
        """,
        (RELATION_POLICY_VERSION, "a" * 64, "a" * 64, "a" * 64, now, now),
    )

    relation_policy_generation._supersede_generation(
        conn, "generation-building", reason_code="program0_rollback"
    )

    row = conn.execute(
        "SELECT state, reason_code, item_set_hash, item_total "
        "FROM relation_policy_generations "
        "WHERE generation_id='generation-building'"
    ).fetchone()
    assert tuple(row) == (
        "superseded",
        "program0_rollback",
        canonical_snapshot_hash({"pairs": []}),
        0,
    )
    conn.close()


def test_flag_off_routes_exactly_to_program0_containment(monkeypatch) -> None:
    conn = _conn()
    calls: list[str] = []

    def legacy(*args, **kwargs):
        calls.append("program0")
        return {"status": "idle", "attempted": 0, "completed": 0, "failed": 0}

    def program2(*args, **kwargs):
        calls.append("program2")
        return {"status": "idle", "attempted": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(maintenance, "drain_relation_containment_scope", legacy)
    monkeypatch.setattr(maintenance, "drain_relation_policy_generation", program2)

    maintenance.drain_relation_frequency_work(
        conn, wall_clock_seconds=5.0, relation_policy_generation_enabled=False
    )
    assert calls == ["program0"]
    calls.clear()
    maintenance.drain_relation_frequency_work(
        conn, wall_clock_seconds=5.0, relation_policy_generation_enabled=True
    )
    assert calls == ["program2"]
    conn.close()
