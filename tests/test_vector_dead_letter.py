"""Tests for audited vector dead-letter inspection and requeue."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scope_recall.sql_store import ensure_schema
from scope_recall.vector_dead_letter import (
    dead_letter_vector_events_report,
    requeue_dead_letter_vector_events,
)
from scope_recall.vector_generation import (
    claim_vector_events,
    enqueue_vector_event,
    fail_vector_event,
)
from writer_lease import TruthWriterLease


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _dead_letter_event(conn: sqlite3.Connection, *, event_key: str = "dead-event") -> int:
    event = enqueue_vector_event(
        conn,
        event_key=event_key,
        generation_id="gen-dead-letter",
        memory_id=f"memory-{event_key}",
        operation="upsert",
        timestamp="2026-07-10T00:00:00+00:00",
    )
    sensitive_error = "api_key=" + "sk-" + "synthetic-dead-letter-value"
    for attempt in range(2):
        worker = f"worker-{attempt}"
        at = f"2026-07-10T00:0{attempt + 1}:00+00:00"
        [claimed] = claim_vector_events(
            conn,
            generation_id="gen-dead-letter",
            worker_id=worker,
            timestamp=at,
        )
        assert claimed["id"] == event["id"]
        fail_vector_event(
            conn,
            event["id"],
            worker_id=worker,
            error=sensitive_error,
            max_attempts=2,
            timestamp=at,
        )
    conn.commit()
    return int(event["id"])


def test_dead_letter_report_is_bounded_and_does_not_echo_errors():
    conn = _connection()
    try:
        event_id = _dead_letter_event(conn)

        report = dead_letter_vector_events_report(conn, limit=10)
    finally:
        conn.close()

    assert report["status"] == "needs_repair"
    assert report["dead_letter"] == 1
    assert report["events"] == [
        {
            "id": event_id,
            "generation_id": "gen-dead-letter",
            "memory_id": "memory-dead-event",
            "operation": "upsert",
            "attempts": 2,
            "updated_at": "2026-07-10T00:02:00+00:00",
        }
    ]
    assert "synthetic-dead-letter-value" not in json.dumps(report)


def test_dead_letter_requeue_dry_run_is_read_only_and_apply_is_audited_idempotent():
    conn = _connection()
    try:
        event_id = _dead_letter_event(conn)
        plan = requeue_dead_letter_vector_events(
            conn,
            event_ids=[event_id],
            apply=False,
        )
        before_apply = conn.execute(
            "SELECT status, attempts FROM vector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert tuple(before_apply) == ("dead_letter", 2)

        applied = requeue_dead_letter_vector_events(
            conn,
            event_ids=[event_id],
            apply=True,
            operation_id="vector-requeue-test-001",
            reason="operator fixed the embedder credentials",
            backup_path="backups/pre-requeue.sqlite3",
            timestamp="2026-07-11T00:00:00+00:00",
        )
        after_apply = conn.execute(
            "SELECT status, attempts, worker_id, last_error, completed_at "
            "FROM vector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
        ledger = conn.execute(
            "SELECT operation_kind, before_json, result_json FROM operator_operations "
            "WHERE operation_id = 'vector-requeue-test-001'"
        ).fetchone()
        replay = requeue_dead_letter_vector_events(
            conn,
            event_ids=[event_id],
            apply=True,
            operation_id="vector-requeue-test-001",
            reason="operator fixed the embedder credentials",
            backup_path="backups/pre-requeue.sqlite3",
            timestamp="2026-07-11T00:00:00+00:00",
        )
        claimed = claim_vector_events(
            conn,
            generation_id="gen-dead-letter",
            worker_id="worker-recovery",
            timestamp="2026-07-11T00:01:00+00:00",
        )
    finally:
        conn.close()

    assert plan["apply"] is False
    assert plan["planned"] == 1
    assert applied["apply"] is True
    assert applied["requeued"] == 1
    assert applied["ids"] == [event_id]
    assert tuple(after_apply) == ("pending", 0, "", "", "")
    assert ledger["operation_kind"] == "vector_outbox.requeue_dead_letter"
    assert "synthetic-dead-letter-value" not in ledger["before_json"]
    assert json.loads(ledger["result_json"])["ids"] == [event_id]
    assert replay["idempotent_replay"] is True
    assert [row["id"] for row in claimed] == [event_id]
    assert claimed[0]["attempts"] == 1


def test_dead_letter_requeue_apply_fails_closed_when_requested_ids_are_not_all_dead():
    conn = _connection()
    try:
        event_id = _dead_letter_event(conn)
        with pytest.raises(ValueError, match="dead-letter"):
            requeue_dead_letter_vector_events(
                conn,
                event_ids=[event_id, 999999],
                apply=True,
                operation_id="vector-requeue-mixed-ids",
                reason="operator selected an invalid mixed event set",
            )
        row = conn.execute(
            "SELECT status, attempts FROM vector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()

    assert tuple(row) == ("dead_letter", 2)


def test_dead_letter_operator_script_runs_dry_run_backup_apply_and_receipt(tmp_path):
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        event_id = _dead_letter_event(conn, event_key="script-dead-event")
    finally:
        conn.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "requeue.vector_dead_letter.py"
    common = [
        sys.executable,
        str(script),
        "--hermes-home",
        str(home),
        "--event-id",
        str(event_id),
    ]
    dry_run = subprocess.run(common, text=True, capture_output=True, check=False)
    blocked = subprocess.run(
        [
            *common,
            "--apply",
            "--operation-id",
            "script-requeue-001",
            "--reason",
            "operator repaired the vector dependency",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    applied = subprocess.run(
        [
            *common,
            "--apply",
            "--maintenance-confirmed",
            "--operation-id",
            "script-requeue-001",
            "--reason",
            "operator repaired the vector dependency",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["planned"] == 1
    assert blocked.returncode == 2
    assert "maintenance-confirmed" in json.loads(blocked.stdout)["error"]
    assert applied.returncode == 0, applied.stdout + applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["status"] == "requeued"
    assert applied_payload["requeued"] == 1
    assert applied_payload["receipt"]["receipt_state"] == "mirrored"

    backups = sorted((storage / "backups").glob("vector-dead-letter-requeue.*.sqlite3"))
    receipts = sorted((storage / "receipts").glob("playbooks.requeue_dead_letter.*.json"))
    assert len(backups) == 1
    assert len(receipts) == 1
    check = sqlite3.connect(db_path)
    try:
        status = check.execute(
            "SELECT status, attempts FROM vector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
        receipt_state = check.execute(
            "SELECT receipt_state FROM operator_operations "
            "WHERE operation_id = 'script-requeue-001'"
        ).fetchone()[0]
    finally:
        check.close()
    assert status == ("pending", 0)
    assert receipt_state == "mirrored"


def test_requeue_apply_fails_closed_while_parent_holds_writer_lease(tmp_path):
    event_marker = "event-marker-not-for-output-42"
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        event_id = _dead_letter_event(conn, event_key=event_marker)
    finally:
        conn.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "requeue.vector_dead_letter.py"
    apply_cmd = [
        sys.executable,
        str(script),
        "--hermes-home",
        str(home),
        "--event-id",
        str(event_id),
        "--apply",
        "--maintenance-confirmed",
        "--operation-id",
        "lease-blocked-requeue-001",
        "--reason",
        "operator repaired the vector dependency",
    ]
    parent = TruthWriterLease(storage, role="provider")
    assert parent.acquire()["status"] == "acquired"
    try:
        blocked = subprocess.run(apply_cmd, text=True, capture_output=True, check=False)
        probe = sqlite3.connect(db_path)
        try:
            row = probe.execute(
                "SELECT status, attempts FROM vector_outbox WHERE id = ?",
                (event_id,),
            ).fetchone()
        finally:
            probe.close()
    finally:
        parent.release()

    combined = (blocked.stdout or "") + (blocked.stderr or "")
    assert blocked.returncode != 0
    assert row == ("dead_letter", 2)
    assert str(home) not in combined
    assert str(db_path) not in combined
    assert "memory.sqlite3" not in combined
    assert event_marker not in combined
    assert "truth_writer_busy" in combined

    applied = subprocess.run(apply_cmd, text=True, capture_output=True, check=False)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "requeued"
    assert payload["requeued"] == 1
    check = sqlite3.connect(db_path)
    try:
        status = check.execute(
            "SELECT status, attempts FROM vector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        check.close()
    assert status == ("pending", 0)
