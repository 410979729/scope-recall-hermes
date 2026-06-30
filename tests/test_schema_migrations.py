"""Tests for schema migration order, ledger state, and backwards compatibility.

They protect live upgrades from partial or out-of-order migration drift."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.sql_store import ensure_schema, schema_migration_status

ROOT = Path(__file__).resolve().parents[1]


def test_schema_migration_status_reports_baseline_after_ensure_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    before = schema_migration_status(conn)
    ensure_schema(conn)
    after = schema_migration_status(conn)

    assert before["current"] is False
    assert before["missing_migrations"] == ["0001_baseline_v1_6_0"]
    assert after["current"] is True
    assert after["user_version"] == after["schema_version"]
    assert [row["id"] for row in after["applied_migrations"]] == ["0001_baseline_v1_6_0"]


def test_migrate_status_script_reports_schema_ledger_read_only(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        before = writer.total_changes
    finally:
        writer.close()

    proc = subprocess.run(
        [sys.executable, "scripts/migrate.status.py", "--hermes-home", str(hermes_home), "--json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == "migration_status_report.v1"
    assert payload["db"] == str(db_path.resolve())
    assert payload["schema_migrations"]["current"] is True
    assert payload["schema_migrations"]["applied_migrations"][0]["id"] == "0001_baseline_v1_6_0"

    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.total_changes == 0
        assert verifier.execute("PRAGMA user_version").fetchone()[0] == payload["schema_migrations"]["user_version"]
        assert before >= 1
    finally:
        verifier.close()


def test_operator_cli_migrate_status_routes_to_schema_status_script():
    import scope_recall.cli as cli

    assert cli._SCRIPT_COMMANDS[("migrate", "status")][0] == "migrate.status.py"
