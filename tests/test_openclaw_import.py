"""Tests for OpenClaw memory import mapping, sanitization, and idempotent ledger behavior.

They keep external memory data from entering Scope Recall truth without review."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scope_recall.migration_openclaw as openclaw_import
from scope_recall.migration_openclaw import run_openclaw_import_rows
from scope_recall.sql_store import ensure_schema


def _openclaw_row(*, row_id: str, text: str, category: str = "memory", scope: str = "joy", timestamp: int = 1_700_000_000_000) -> dict:
    return {
        "id": row_id,
        "text": text,
        "category": category,
        "scope": scope,
        "timestamp": timestamp,
        "metadata": {"source": "unit-test"},
    }


def _memory_count(db_path: Path, prefix: str = "openclaw:") -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memories WHERE id LIKE ?", (f"{prefix}%",)).fetchone()[0])
    finally:
        conn.close()


def test_openclaw_import_dry_run_is_read_only_and_surfaces_safety_findings(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    rows = [
        _openclaw_row(row_id="safe", text="OpenClaw restart workflow: check gateway health before restart", category="ops"),
        _openclaw_row(row_id="secret", text="token: x should never become durable memory", category="memory"),
        _openclaw_row(row_id="blocked-target", text="raw scratch transcript", category="general"),
    ]

    report = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        scope_prefix="imported.openclaw",
        allowed_targets={"memory", "ops"},
        apply=False,
    )

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["safe_to_apply"] is False
    assert report["rows_seen"] == 3
    assert report["rows_mappable"] == 3
    assert report["rows_rejected"] == 1
    assert report["lint"]["high_risk_count"] == 1
    assert {item["reason"] for item in report["rejections"]} == {"target_not_allowed"}
    assert {item["kind"] for item in report["lint"]["findings"]} == {"secret_like"}
    assert not target_db.exists()


def test_openclaw_import_apply_refuses_unsafe_rows_before_writing(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    target_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(target_db)
    try:
        ensure_schema(conn)
        before_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    report = run_openclaw_import_rows(
        [_openclaw_row(row_id="secret", text="password=hunter2", category="memory")],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"memory"},
        apply=True,
    )

    assert report["ok"] is False
    assert report["dry_run"] is False
    assert report["safe_to_apply"] is False
    assert "safety" in report["error"].lower()
    assert report["rows_inserted"] == 0
    assert report["backup"] == ""
    assert _memory_count(target_db) == 0
    conn = sqlite3.connect(target_db)
    try:
        after_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert after_tables == before_tables


def test_openclaw_import_blocks_secret_like_source_metadata_before_writing(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    row = _openclaw_row(row_id="metadata-secret", text="Safe operational note", category="ops")
    row["metadata"] = {
        "api_key": "x",
        "path": "/private/openclaw/config.yaml",
        "safe_label": "legacy",
    }

    dry_run = run_openclaw_import_rows(
        [row],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=False,
    )
    assert dry_run["safe_to_apply"] is False
    assert {finding["kind"] for finding in dry_run["lint"]["findings"]} >= {"secret_like", "path_like"}
    assert not target_db.exists()

    blocked = run_openclaw_import_rows(
        [row],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
    )
    assert blocked["ok"] is False
    assert blocked["rows_inserted"] == 0
    assert not target_db.exists()


def test_openclaw_import_rejects_raw_transcript_even_when_target_allowed(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    transcript = "User: please restart the gateway\nAssistant: I will run systemctl and paste logs\nTool execution trace: stdout stderr"
    for target in ("memory", "ops", "project", "user"):
        report = run_openclaw_import_rows(
            [_openclaw_row(row_id=f"raw-{target}", text=transcript, category=target)],
            source_path=tmp_path / "openclaw-memory",
            target_db=target_db,
            allowed_targets={"memory", "ops", "project", "user"},
            apply=False,
        )
        assert report["safe_to_apply"] is False
        assert report["rows_rejected"] == 1
        assert report["rejections"][0]["reason"] == "raw_transcript"
    assert not target_db.exists()


def test_openclaw_import_backups_are_unique_and_preserve_pre_apply_state(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    target_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(target_db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    first = run_openclaw_import_rows(
        [_openclaw_row(row_id="safe-1", text="First safe OpenClaw import note", category="ops")],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
    )
    second = run_openclaw_import_rows(
        [_openclaw_row(row_id="safe-2", text="Second safe OpenClaw import note", category="ops")],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
    )

    assert first["backup"]
    assert second["backup"]
    assert first["backup"] != second["backup"]
    first_backup = sqlite3.connect(first["backup"])
    second_backup = sqlite3.connect(second["backup"])
    try:
        assert first_backup.execute("SELECT COUNT(*) FROM memories WHERE id LIKE 'openclaw:%'").fetchone()[0] == 0
        assert second_backup.execute("SELECT COUNT(*) FROM memories WHERE id LIKE 'openclaw:%'").fetchone()[0] == 1
    finally:
        first_backup.close()
        second_backup.close()
    assert _memory_count(target_db) == 2


def test_openclaw_import_path_and_template_lint_block_apply_by_default(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    rows = [
        _openclaw_row(row_id="path", text="Config lives at /private/config.yaml", category="ops"),
        _openclaw_row(row_id="template", text="Render {{ customer_secret }} before deploy", category="ops"),
    ]

    report = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
    )

    assert report["ok"] is False
    assert report["lint"]["blocking_count"] == 2
    assert {finding["kind"] for finding in report["lint"]["findings"]} == {"path_like", "template_like"}
    assert not target_db.exists()


def test_openclaw_import_receipt_can_reconcile_inserted_and_skipped_rows(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    target_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(target_db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    receipt_path = tmp_path / "receipts" / "openclaw-import.json"
    rows = [_openclaw_row(row_id="safe", text="Receipt reconciliation safe note", category="ops")]

    first = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
        receipt_path=receipt_path,
    )
    second = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
        receipt_path=receipt_path,
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert first["inserted"][0]["memory_id"].startswith("openclaw:")
    assert first["inserted"][0]["fingerprint"] in first["ledger_fingerprints"]
    assert first["backup_info"]["sha256"]
    assert second["skipped"][0]["memory_id"] == first["inserted"][0]["memory_id"]
    assert receipt["skipped"][0]["fingerprint"] == first["inserted"][0]["fingerprint"]
    conn = sqlite3.connect(target_db)
    try:
        ledger_rows = conn.execute("SELECT import_fingerprint, memory_id FROM import_ledger").fetchall()
    finally:
        conn.close()
    assert ledger_rows == [(first["inserted"][0]["fingerprint"], first["inserted"][0]["memory_id"])]


def test_openclaw_import_reports_graph_sync_failures_without_blocking_truth_import(tmp_path: Path, monkeypatch):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    target_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(target_db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    def fail_sync(*args, **kwargs):
        raise RuntimeError("graph sync unavailable")

    monkeypatch.setattr(openclaw_import, "sync_memory_entities", fail_sync)
    report = run_openclaw_import_rows(
        [_openclaw_row(row_id="safe", text="Graph warning import should preserve SQLite truth", category="ops")],
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        allowed_targets={"ops"},
        apply=True,
    )

    assert report["ok"] is True
    assert report["rows_inserted"] == 1
    assert report["graph_sync_failed_count"] == 1
    assert report["graph_sync_failures"][0]["memory_id"].startswith("openclaw:")
    assert "repair.graph_hygiene" in report["graph_repair"]["command"]
    assert _memory_count(target_db) == 1


def test_openclaw_import_apply_creates_backup_receipt_and_is_idempotent(tmp_path: Path):
    target_db = tmp_path / "hermes" / "scope-recall" / "memory.sqlite3"
    target_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(target_db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    receipt_path = tmp_path / "receipts" / "openclaw-import.json"
    rows = [
        _openclaw_row(row_id="safe", text="Use OpenClaw health checks before gateway restart", category="ops"),
    ]

    first = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        scope_prefix="imported.openclaw",
        allowed_targets={"ops"},
        apply=True,
        receipt_path=receipt_path,
        vector_repair="dry-run",
    )

    assert first["ok"] is True
    assert first["dry_run"] is False
    assert first["safe_to_apply"] is True
    assert first["rows_inserted"] == 1
    assert first["rows_skipped"] == 0
    assert Path(first["backup"]).exists()
    assert first["receipt_path"] == str(receipt_path)
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["rows_inserted"] == 1
    assert receipt["backup"] == first["backup"]
    assert receipt["vector_repair"]["mode"] == "dry-run"
    assert "hermes-scope-recall vector repair" in receipt["vector_repair"]["command"]

    second = run_openclaw_import_rows(
        rows,
        source_path=tmp_path / "openclaw-memory",
        target_db=target_db,
        scope_prefix="imported.openclaw",
        allowed_targets={"ops"},
        apply=True,
    )

    assert second["ok"] is True
    assert second["rows_inserted"] == 0
    assert second["rows_skipped"] == 1
    assert _memory_count(target_db) == 1
