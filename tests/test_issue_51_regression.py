"""Accident-scale closure rehearsal for the retired relation rebuild queue."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile

import pytest

import scope_recall.relation_cleanup as cleanup
import scope_recall.relation_frequency_maintenance as maintenance
import scope_recall.relation_rebuild_queue as legacy_queue
from scope_recall.relation_containment import (
    plan_focus_relation_pairs,
    relation_containment_report,
)
from scope_recall.relation_rebuild_queue import (
    relation_rebuild_debt_exists,
    relation_rebuild_queue_report,
)
from scope_recall.sql_store import ensure_schema


_DETAILS_SCHEMA = "scope-recall.issue-51-regression-details.v1"
_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_ISSUE_51_DETAILS_OUTPUT"
_VISIBLE_MEMORIES = 2_046
_LEGACY_PENDING = 1_136
_LEGACY_ATTEMPTS = 658_038
_INCIDENT_SCOPE = "issue-51-incident-scope"
_OLD_REVISION = "issue-51-old-revision"
_NOW = "2026-08-28T00:00:00+00:00"
_FOCUS_MEMORY = "memory-02045"
_CLEANUP_OPERATION_ID = "issue51-accident-scale-closure"
_CLEANUP_REASON = "close retired Issue 51 legacy work after verified backup"


def _legacy_snapshot(conn: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in conn.execute(
            """
            SELECT id, scope_id, focus_memory_id, requested_updated_at,
                   next_requested_updated_at, reason, status, cursor_memory_id,
                   processed_pairs, pass_processed_pairs, pass_number,
                   supersession_count, last_progress_at, attempts,
                   lease_expirations, pass_lease_expirations, failures,
                   pass_failures, available_at, updated_at, lease_owner,
                   lease_token, COALESCE(lease_expires_at, ''), corpus_revision,
                   blocked_entities_json, blocked_entities_sha256, last_error,
                   created_at, COALESCE(completed_at, '')
            FROM relation_rebuild_queue
            ORDER BY id
            """
        ).fetchall()
    )


def _seed_incident_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        ensure_schema(conn)
        conn.executemany(
            """
            INSERT INTO memories(
                id, scope_id, source, target, content, summary,
                created_at, updated_at, metadata
            ) VALUES(?, ?, 'issue-51-rehearsal', 'project', ?, ?, ?, ?, '{}')
            """,
            (
                (
                    f"memory-{index:05d}",
                    _INCIDENT_SCOPE,
                    f"Issue 51 visible memory {index}",
                    f"memory {index}",
                    _NOW,
                    _NOW,
                )
                for index in range(_VISIBLE_MEMORIES)
            ),
        )
        attempts_per_row, remainder = divmod(_LEGACY_ATTEMPTS, _LEGACY_PENDING)
        conn.executemany(
            """
            INSERT INTO relation_rebuild_queue(
                scope_id, focus_memory_id, requested_updated_at, reason,
                status, attempts, available_at, lease_owner, lease_token,
                lease_expires_at, corpus_revision, created_at, updated_at
            ) VALUES(?, ?, ?, 'legacy full-scope rebuild from Issue 51',
                     'pending', ?, ?, 'legacy-worker', 'legacy-lease-token',
                     ?, 17, ?, ?)
            """,
            (
                (
                    _INCIDENT_SCOPE,
                    f"memory-{index:05d}",
                    _OLD_REVISION,
                    attempts_per_row + int(index < remainder),
                    _NOW,
                    _NOW,
                    _NOW,
                    _NOW,
                )
                for index in range(_LEGACY_PENDING)
            ),
        )
        # The incident rehearsal starts after current bounded index work is idle.
        # Only the historical queue remains as explicit operator debt.
        for table in (
            "relation_focus_work_scopes",
            "relation_focus_work",
            "relation_frequency_failures",
            "relation_frequency_changes",
            "relation_frequency_backfill",
            "relation_entity_postings",
            "relation_indexed_memories",
            "relation_scope_entity_frequency",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


def _legacy_sql_mutations(statements: list[str]) -> list[str]:
    mutations: list[str] = []
    for statement in statements:
        normalized = " ".join(statement.casefold().split())
        if "relation_rebuild_queue" not in normalized:
            continue
        if normalized.startswith(("insert ", "update ", "delete ", "replace ")):
            mutations.append(normalized)
    return mutations


def _publish_details(payload: dict[str, object]) -> None:
    output = str(os.environ.get(_DETAILS_OUTPUT_ENV) or "").strip()
    if not output:
        return
    Path(output).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_issue_51_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    # Keep the Windows rehearsal path deliberately short: the verified-backup
    # staging suffix is long by design and must stay below the legacy Win32
    # path boundary used by the supported Python/SQLite runtime.
    del tmp_path
    boundary_parent = Path(os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"])
    short_temp = tempfile.TemporaryDirectory(prefix="sr51.", dir=boundary_parent)
    request.addfinalizer(short_temp.cleanup)
    tmp_path = Path(short_temp.name)
    database = tmp_path / "m.sqlite3"
    _seed_incident_database(database)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == (
            _VISIBLE_MEMORIES
        )
        initial = _legacy_snapshot(conn)
        assert len(initial) == _LEGACY_PENDING
        assert sum(int(row[13]) for row in initial) == _LEGACY_ATTEMPTS
        assert {str(row[3]) for row in initial} == {_OLD_REVISION}

        # Re-initialization is additive but must never recover, claim, or relabel
        # the retired queue.
        ensure_schema(conn)
        conn.commit()
        assert _legacy_snapshot(conn) == initial

        forbidden_calls: list[str] = []

        def forbidden_claim_or_drain(*args: object, **kwargs: object) -> object:
            del args, kwargs
            forbidden_calls.append("legacy-execution-surface")
            raise AssertionError("current maintenance invoked retired legacy work")

        monkeypatch.setattr(
            legacy_queue,
            "claim_relation_rebuild_events",
            forbidden_claim_or_drain,
        )
        monkeypatch.setattr(
            legacy_queue,
            "drain_relation_rebuild_queue",
            forbidden_claim_or_drain,
        )
        idle_tick_statements: list[str] = []
        conn.set_trace_callback(idle_tick_statements.append)
        for _monotonic_second in range(61):
            assert relation_rebuild_debt_exists(conn) is True
            assert conn.in_transaction is False
        conn.set_trace_callback(None)
        legacy_transactions = [
            statement
            for statement in idle_tick_statements
            if " ".join(statement.casefold().split()).startswith(
                ("begin", "commit", "rollback")
            )
        ]
        assert legacy_transactions == []
        clock_values = iter((0.0, 60.0, 60.0, 60.0))
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        idle = maintenance.drain_relation_frequency_work(
            conn,
            change_limit=0,
            focus_limit=0,
            backfill_limit=1,
            relation_candidate_cap=1,
            wall_clock_seconds=60.0,
            deadline_monotonic=60.0,
            clock=lambda: next(clock_values, 60.0),
            commit=True,
        )
        conn.set_trace_callback(None)
        legacy_mutations = _legacy_sql_mutations(statements)
        assert idle["changed_memories"] == 0
        assert idle["focused_memories"] == 0
        assert forbidden_calls == []
        assert legacy_mutations == []
        assert _legacy_snapshot(conn) == initial

        # One relation-relevant update creates one exact focus item. It does
        # not fan out into one legacy item per memory or per scope.
        conn.execute(
            """
            UPDATE memories
            SET content='Issue 51 Focus Service owns this durable fact',
                summary='Issue 51 Focus Service fact',
                metadata='{"entities":["Issue 51 shared entity"]}',
                updated_at='2026-08-28T00:00:01+00:00'
            WHERE id=?
            """,
            (_FOCUS_MEMORY,),
        )
        conn.commit()
        assert maintenance._drain_change_rows(conn, 1) == 1
        conn.commit()
        focus_rows = conn.execute(
            """
            SELECT memory_id, work_generation, status
            FROM relation_focus_work ORDER BY memory_id
            """
        ).fetchall()
        focus_scopes = conn.execute(
            """
            SELECT memory_id, work_generation, scope_id
            FROM relation_focus_work_scopes ORDER BY memory_id, scope_id
            """
        ).fetchall()
        assert [tuple(row) for row in focus_rows] == [
            (_FOCUS_MEMORY, int(focus_rows[0][1]), "pending")
        ]
        assert [str(row[0]) for row in focus_scopes] == [_FOCUS_MEMORY]
        assert _legacy_snapshot(conn) == initial

        # Give the focus two exact posting peers, then exercise the cap+1
        # planner. The refusal is read-only: no relation or generation item is
        # partially materialized.
        focus_entity = conn.execute(
            """
            SELECT entity FROM relation_entity_postings
            WHERE scope_id=? AND memory_id=?
            """,
            (_INCIDENT_SCOPE, _FOCUS_MEMORY),
        ).fetchone()
        assert focus_entity is not None
        entity = str(focus_entity[0])
        conn.executemany(
            """
            INSERT INTO relation_entity_postings(scope_id, entity, memory_id)
            VALUES(?, ?, ?)
            """,
            (
                (_INCIDENT_SCOPE, entity, "memory-00000"),
                (_INCIDENT_SCOPE, entity, "memory-00001"),
            ),
        )
        conn.commit()
        before_relations = conn.execute(
            "SELECT COUNT(*) FROM memory_relations"
        ).fetchone()[0]
        before_generation_items = conn.execute(
            "SELECT COUNT(*) FROM relation_generation_items"
        ).fetchone()[0]
        cap_plan = plan_focus_relation_pairs(
            conn,
            scope_id=_INCIDENT_SCOPE,
            memory_id=_FOCUS_MEMORY,
            blocked_entities=(),
            candidate_cap=1,
            target_revision=17,
        )
        assert cap_plan.blocked is True
        assert cap_plan.reason_code == "affected_candidate_cap_exceeded"
        assert cap_plan.affected_count == 2
        assert cap_plan.pairs == ()
        assert conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == (
            before_relations
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM relation_generation_items"
        ).fetchone()[0] == before_generation_items

        queue_report = relation_rebuild_queue_report(conn)
        containment = relation_containment_report(conn)
        incident_scope = next(
            item
            for item in containment["scopes"]
            if item["scope_id"] == _INCIDENT_SCOPE
        )
        assert queue_report["pending"] == _LEGACY_PENDING
        assert queue_report["lifetime_attempts"] == _LEGACY_ATTEMPTS
        assert containment["status"] == "blocked"
        assert incident_scope["reason_code"] == "legacy_unbounded_work_present"
        assert incident_scope["operator_action_required"] is True
    finally:
        conn.close()

    plan = cleanup.run_legacy_relation_cleanup(
        database,
        scope_ids=[_INCIDENT_SCOPE],
    )
    repeated_plan = cleanup.run_legacy_relation_cleanup(
        database,
        scope_ids=[_INCIDENT_SCOPE],
    )
    assert plan["status"] == "planned"
    assert plan["rebuild_queue_count"] == _LEGACY_PENDING
    assert repeated_plan["plan_sha256"] == plan["plan_sha256"]

    # A syntactically valid but stale expected plan hash must fail before any
    # backup or legacy mutation. This is the operator cleanup CAS fence.
    with pytest.raises(RuntimeError, match="failed before commit"):
        cleanup.run_legacy_relation_cleanup(
            database,
            apply=True,
            maintenance_confirmed=True,
            scope_ids=[_INCIDENT_SCOPE],
            expected_plan_sha256="0" * 64,
            operation_id="issue51-stale-plan-refusal",
            reason=_CLEANUP_REASON,
        )
    verifier = sqlite3.connect(database)
    try:
        assert verifier.execute(
            "SELECT COUNT(*) FROM relation_rebuild_queue"
        ).fetchone()[0] == _LEGACY_PENDING
    finally:
        verifier.close()

    applied = cleanup.run_legacy_relation_cleanup(
        database,
        apply=True,
        maintenance_confirmed=True,
        scope_ids=[_INCIDENT_SCOPE],
        expected_plan_sha256=str(plan["plan_sha256"]),
        operation_id=_CLEANUP_OPERATION_ID,
        reason=_CLEANUP_REASON,
    )
    assert applied["status"] == "committed"
    assert applied["deleted_rebuild_queue_count"] == _LEGACY_PENDING
    assert applied["receipt_state"] == "mirrored"
    assert applied["replayed"] is False
    backup = database.parent / Path(str(applied["backup_path"]))
    assert backup.is_file()
    backup_db = sqlite3.connect(backup)
    try:
        assert backup_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup_db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == (
            _VISIBLE_MEMORIES
        )
        assert backup_db.execute(
            "SELECT COUNT(*) FROM relation_rebuild_queue WHERE status='pending'"
        ).fetchone()[0] == _LEGACY_PENDING
    finally:
        backup_db.close()
    receipt = Path(str(applied["receipt_path"]))
    assert receipt.is_file()

    replayed = cleanup.run_legacy_relation_cleanup(
        database,
        apply=True,
        maintenance_confirmed=True,
        scope_ids=[_INCIDENT_SCOPE],
        expected_plan_sha256=str(plan["plan_sha256"]),
        operation_id=_CLEANUP_OPERATION_ID,
        reason=_CLEANUP_REASON,
    )
    assert replayed["status"] == "committed"
    assert replayed["receipt_state"] == "mirrored"
    assert replayed["replayed"] is True
    assert replayed["backup_path"] == applied["backup_path"]
    assert list(backup.parent.glob("relation-cleanup.*.sqlite3")) == [backup]

    final = sqlite3.connect(database)
    try:
        remaining = final.execute(
            "SELECT COUNT(*) FROM relation_rebuild_queue"
        ).fetchone()[0]
        disposition_count = final.execute(
            """
            SELECT COUNT(*) FROM relation_work_dispositions
            WHERE operation_id=? AND work_kind='rebuild_queue'
            """,
            (_CLEANUP_OPERATION_ID,),
        ).fetchone()[0]
        operation_count = final.execute(
            "SELECT COUNT(*) FROM operator_operations WHERE operation_id=?",
            (_CLEANUP_OPERATION_ID,),
        ).fetchone()[0]
    finally:
        final.close()
    assert remaining == 0
    assert disposition_count == _LEGACY_PENDING
    assert operation_count == 1

    _publish_details(
        {
            "schema_version": _DETAILS_SCHEMA,
            "visible_memory_count": _VISIBLE_MEMORIES,
            "legacy_pending_count": _LEGACY_PENDING,
            "legacy_attempts_total": _LEGACY_ATTEMPTS,
            "old_revision_distinct_count": 1,
            "initialization_legacy_mutations": 0,
            "idle_legacy_mutations": 0,
            "legacy_attempts_unchanged": True,
            "legacy_status_unchanged": True,
            "legacy_available_at_unchanged": True,
            "legacy_lease_fields_unchanged": True,
            "simulated_monotonic_seconds": 60.0,
            "simulated_idle_tick_count": 61,
            "legacy_claim_calls": 0,
            "legacy_drain_calls": 0,
            "legacy_sql_transaction_count": len(legacy_transactions),
            "legacy_sql_mutation_count": len(legacy_mutations),
            "exact_focus_work_count": len(focus_rows),
            "exact_focus_scope_count": len(focus_scopes),
            "scope_wide_fanout_count": 0,
            "candidate_cap": 1,
            "candidate_affected_count": cap_plan.affected_count,
            "candidate_cap_refused": cap_plan.blocked,
            "partial_relation_mutation_count": 0,
            "operator_action_required": True,
            "cleanup_plan_sha256": str(plan["plan_sha256"]),
            "cleanup_repeated_plan_sha256": str(repeated_plan["plan_sha256"]),
            "cleanup_cas_refused": True,
            "backup_verified": True,
            "backup_visible_memory_count": _VISIBLE_MEMORIES,
            "backup_legacy_pending_count": _LEGACY_PENDING,
            "cleanup_deleted_legacy_count": int(
                applied["deleted_rebuild_queue_count"]
            ),
            "cleanup_disposition_count": int(disposition_count),
            "cleanup_receipt_state": str(applied["receipt_state"]),
            "cleanup_receipt_present": receipt.is_file(),
            "cleanup_idempotent_replay": bool(replayed["replayed"]),
            "cleanup_replay_backup_stable": (
                replayed["backup_path"] == applied["backup_path"]
            ),
            "cleanup_remaining_legacy_count": int(remaining),
        }
    )
