from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.experience_bootstrap import CORE_PLAYBOOKS, bootstrap_core_playbooks
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_core_playbook_catalog_has_at_least_five_promotable_playbooks():
    assert len(CORE_PLAYBOOKS) >= 5
    ids = [str(item["id"]) for item in CORE_PLAYBOOKS]
    assert len(ids) == len(set(ids))
    for item in CORE_PLAYBOOKS:
        payload = item["payload"]
        assert payload["title"]
        assert payload["task_class"]
        assert payload["verification"]
        assert payload["reuse_policy"]["requires_live_check"] is True


def test_bootstrap_core_playbooks_dry_run_does_not_mutate():
    conn = _conn()
    before_changes = conn.total_changes

    result = bootstrap_core_playbooks(conn, scope_id="scope-a", shared_scope_id="pool", dry_run=True)

    assert result["dry_run"] is True
    assert result["created"] == 0
    assert result["promoted"] == 0
    assert len(result["items"]) >= 5
    assert all(item["action"] == "would_create_promote" for item in result["items"])
    assert conn.total_changes == before_changes
    assert conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 0


def test_bootstrap_core_playbooks_dry_run_handles_legacy_db_without_experience_schema(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memories(id TEXT PRIMARY KEY, content TEXT)")
    conn.commit()
    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    try:
        result = bootstrap_core_playbooks(readonly, scope_id="scope-a", shared_scope_id="pool", dry_run=True)
    finally:
        readonly.close()
        conn.close()

    assert result["dry_run"] is True
    assert result["schema_missing"] is True
    assert result["created"] == 0
    assert result["promoted"] == 0
    assert len(result["items"]) == len(CORE_PLAYBOOKS)
    assert all(item["action"] == "would_create_promote" for item in result["items"])


def test_bootstrap_core_playbooks_apply_creates_promoted_seed_set_idempotently():
    conn = _conn()

    first = bootstrap_core_playbooks(conn, scope_id="scope-a", shared_scope_id="pool", accessible_scope_ids=["scope-a", "pool"], dry_run=False)
    second = bootstrap_core_playbooks(conn, scope_id="scope-a", shared_scope_id="pool", accessible_scope_ids=["scope-a", "pool"], dry_run=False)

    assert first["created"] >= 5
    assert first["promoted"] == first["created"]
    assert second["created"] == 0
    assert second["skipped_existing"] == len(CORE_PLAYBOOKS)
    rows = conn.execute("SELECT id, status, evidence_anchors, reuse_policy FROM procedural_playbooks").fetchall()
    assert len(rows) == len(CORE_PLAYBOOKS)
    assert {row["status"] for row in rows} == {"promoted"}
    for row in rows:
        assert "curated_bootstrap" in row["evidence_anchors"]
        assert "requires_live_check" in row["reuse_policy"]
    version_counts = conn.execute("SELECT playbook_id, COUNT(*) AS count FROM playbook_versions GROUP BY playbook_id").fetchall()
    assert {row["count"] for row in version_counts} == {2}


def test_playbook_bootstrap_cli_dry_run_does_not_mutate(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/playbook.bootstrap.py",
            "--db",
            str(db_path),
            "--scope-id",
            "scope-a",
            "--dry-run",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["dry_run"] is True
    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0] == 0
    finally:
        verifier.close()
