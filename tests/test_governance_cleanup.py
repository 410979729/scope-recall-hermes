from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

from scope_recall.governance_cleanup import active_dirty_counts, apply_cleanup, find_cleanup_candidates, rollback_cleanup_batch
from scope_recall.sql_store import ensure_governance_schema, ensure_schema, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert(conn: sqlite3.Connection, *, memory_id: str, content: str, source: str = "journal-digest", metadata: dict | None = None) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source=source,
        target="memory",
        content=content,
        metadata=metadata,
        allow_duplicate=True,
    )


def _metadata(conn: sqlite3.Connection, memory_id: str) -> dict:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    assert row is not None
    return json.loads(row["metadata"] or "{}")


def test_governance_schema_is_created_with_audit_table():
    conn = _conn()

    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='governance_audit_events'").fetchone()

    assert row is not None


def test_governance_schema_migrates_legacy_audit_table_missing_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE governance_audit_events (id TEXT PRIMARY KEY)")

    ensure_governance_schema(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(governance_audit_events)").fetchall()}
    assert {"batch_id", "before_json", "after_json", "dry_run", "created_at"} <= columns
    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(governance_audit_events)").fetchall()}
    assert "idx_governance_audit_batch" in indexes


def test_cleanup_dry_run_finds_historical_template_and_transcript_noise_without_mutation():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    _insert(conn, memory_id="journal", content="Journal digest memory decision/workflow about release.")
    _insert(conn, memory_id="role", content="user: 请继续\nassistant: 已完成。")
    _insert(conn, memory_id="keep", content="Joy prefers concise Chinese operation reports.")

    before_changes = conn.total_changes
    candidates = find_cleanup_candidates(conn, scope_ids=["shared-scope"], limit=20)
    counts = active_dirty_counts(conn, scope_ids=["shared-scope"])
    result = apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=True, limit=20, batch_id="batch-dry")

    assert conn.total_changes == before_changes
    assert {item["id"] for item in candidates} == {"ops", "journal", "role"}
    assert counts["template.operations-workflow-summary"] == 1
    assert counts["template.journal-digest-memory"] == 1
    assert counts["transcript.role-prefix-user"] == 1
    assert result["dry_run"] is True
    assert result["archived"] == 0
    assert _metadata(conn, "ops").get("lifecycle") != "archived"


def test_cleanup_dry_run_does_not_rebuild_stale_fts_index():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    conn.execute("DELETE FROM memories_fts")
    conn.commit()
    before_changes = conn.total_changes

    counts = active_dirty_counts(conn, scope_ids=["shared-scope"])
    candidates = find_cleanup_candidates(conn, scope_ids=["shared-scope"], limit=20)
    result = apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=True, limit=20, batch_id="batch-dry")

    assert conn.total_changes == before_changes
    assert counts["template.operations-workflow-summary"] == 1
    assert [item["id"] for item in candidates] == ["ops"]
    assert result["candidate_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 0


def test_cleanup_apply_soft_archives_and_writes_rollback_audit_events():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    _insert(conn, memory_id="journal", content="Journal digest memory decision/workflow about release.")
    _insert(conn, memory_id="keep", content="Joy prefers concise Chinese operation reports.")

    result = apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-apply")

    assert result["archived"] == 2
    assert result["batch_id"] == "batch-apply"
    assert active_dirty_counts(conn, scope_ids=["shared-scope"]) == {
        "template.journal-digest-memory": 0,
        "template.operations-workflow-summary": 0,
        "transcript.role-prefix-assistant": 0,
        "transcript.role-prefix-user": 0,
    }
    ops_meta = _metadata(conn, "ops")
    assert ops_meta["lifecycle"] == "archived"
    assert ops_meta["forget_reason"] == "template.operations-workflow-summary"
    assert ops_meta["rollback_batch_id"] == "batch-apply"
    assert _metadata(conn, "keep").get("lifecycle") != "archived"
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = ? AND action = 'soft_archive'",
        ("batch-apply",),
    ).fetchone()[0]
    assert audit_count == 2


def test_cleanup_rollback_restores_soft_archived_metadata():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-rollback")
    assert _metadata(conn, "ops")["lifecycle"] == "archived"

    dry = rollback_cleanup_batch(conn, batch_id="batch-rollback", dry_run=True)
    assert dry["rollback_candidates"] == 1
    assert _metadata(conn, "ops")["lifecycle"] == "archived"

    applied = rollback_cleanup_batch(conn, batch_id="batch-rollback", dry_run=False)

    assert applied["restored"] == 1
    assert _metadata(conn, "ops").get("lifecycle") != "archived"
    rollback_count = conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = ? AND action = 'rollback_soft_archive'",
        ("batch-rollback",),
    ).fetchone()[0]
    assert rollback_count == 1

    second = rollback_cleanup_batch(conn, batch_id="batch-rollback", dry_run=False)
    assert second["restored"] == 0
    assert _metadata(conn, "ops").get("lifecycle") != "archived"


def test_cleanup_rollback_skips_rows_rearchived_by_later_batch():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="old-batch")
    assert rollback_cleanup_batch(conn, batch_id="old-batch", dry_run=False)["restored"] == 1
    metadata = _metadata(conn, "ops")
    metadata.update({"lifecycle": "archived", "rollback_batch_id": "new-batch", "forget_reason": "operator-review"})
    conn.execute("UPDATE memories SET metadata = ? WHERE id = 'ops'", (json.dumps(metadata, ensure_ascii=False),))
    conn.commit()

    second = rollback_cleanup_batch(conn, batch_id="old-batch", dry_run=False)

    assert second["restored"] == 0
    assert _metadata(conn, "ops")["rollback_batch_id"] == "new-batch"


def test_governance_cleanup_cli_allows_apply_rollback_batch(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="cli-batch")
    conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/governance.cleanup.py",
            "--db",
            str(db_path),
            "--rollback-batch",
            "--batch-id",
            "cli-batch",
            "--apply",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["dry_run"] is False
    assert payload["result"]["restored"] == 1
    reopened = sqlite3.connect(db_path)
    reopened.row_factory = sqlite3.Row
    try:
        assert _metadata(reopened, "ops").get("lifecycle") != "archived"
    finally:
        reopened.close()
