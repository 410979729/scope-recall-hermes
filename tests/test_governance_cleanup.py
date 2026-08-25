"""Tests for governance cleanup, rollback batches, and legacy audit backfill.

Cleanup must remain auditable and fail closed when evidence is missing."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading

import pytest

from scope_recall.memory_ops import archive_memories
from scope_recall.governance_cleanup import (
    active_dirty_counts,
    apply_cleanup,
    backfill_legacy_archive_audit,
    find_cleanup_candidates,
    governance_audit_coverage_report,
    rollback_cleanup_batch,
)
from scope_recall.sql_store import ensure_governance_schema, ensure_schema, record_governance_audit_event, store_row


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


def _update_metadata(conn: sqlite3.Connection, memory_id: str, updates: dict) -> None:
    metadata = _metadata(conn, memory_id)
    metadata.update(updates)
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


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


def test_record_governance_audit_event_does_not_commit_outer_business_transaction(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    ensure_schema(setup)
    _insert(setup, memory_id="txn-row", content="Memory that should not stay archived after rollback.")
    setup.commit()
    setup.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    metadata = _metadata(conn, "txn-row")
    metadata["lifecycle"] = "archived"
    metadata["archived_batch_id"] = "txn-probe"
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), "txn-row"))
    record_governance_audit_event(
        conn,
        event_id="gov-txn-probe",
        event_type="scope_recall_forget",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="txn-row",
        batch_id="txn-probe",
        before={"id": "txn-row", "metadata": {}},
        after={"id": "txn-row", "metadata": metadata},
        reason="transaction probe",
        actor="test",
    )
    conn.rollback()
    conn.close()

    reopened = sqlite3.connect(db_path)
    reopened.row_factory = sqlite3.Row
    try:
        assert _metadata(reopened, "txn-row").get("lifecycle") != "archived"
        audit_rows = reopened.execute("SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = 'txn-probe'").fetchone()[0]
        assert audit_rows == 0
    finally:
        reopened.close()


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


def test_cleanup_apply_commit_failure_does_not_persist_archive_without_audit(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    ensure_schema(setup)
    _insert(setup, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    setup.commit()
    setup.close()

    class CommitFailingConnection:
        def __init__(self, path):
            self.raw = sqlite3.connect(path)
            self.raw.row_factory = sqlite3.Row

        def execute(self, *args, **kwargs):
            return self.raw.execute(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            return self.raw.executescript(*args, **kwargs)

        def commit(self) -> None:
            raise RuntimeError("commit failed after governance cleanup audit insert")

        def rollback(self) -> None:
            self.raw.rollback()

        def close(self) -> None:
            self.raw.close()

    conn = CommitFailingConnection(db_path)
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="cleanup-commit-fail")
    conn.rollback()
    conn.close()

    reopened = sqlite3.connect(db_path)
    reopened.row_factory = sqlite3.Row
    try:
        assert _metadata(reopened, "ops").get("lifecycle") != "archived"
        audit_rows = reopened.execute("SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = 'cleanup-commit-fail'").fetchone()[0]
        assert audit_rows == 0
    finally:
        reopened.close()


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


def test_cleanup_rollback_preserves_callers_outer_transaction():
    conn = _conn()
    _insert(
        conn,
        memory_id="ops",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    _insert(conn, memory_id="marker", content="caller-owned content")
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="outer-transaction-rollback",
    )
    assert _metadata(conn, "ops")["lifecycle"] == "archived"

    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("caller uncommitted change", "marker"),
    )
    assert conn.in_transaction is True

    applied = rollback_cleanup_batch(
        conn,
        batch_id="outer-transaction-rollback",
        dry_run=False,
    )

    assert applied["restored"] == 1
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute("SELECT content FROM memories WHERE id='marker'").fetchone()[0] == "caller-owned content"
    assert _metadata(conn, "ops")["lifecycle"] == "archived"


def test_scope_recall_forget_archive_batch_is_default_rollback_candidate():
    conn = _conn()
    _insert(conn, memory_id="forgotten", content="User asked to forget this exact memory id.")

    class Provider:
        _vector_store = None
        _vector_enabled = False
        _vector_status = "disabled"
        _vector_message = ""
        _accessible_scope_ids = ["shared-scope"]
        _writable_scope_ids = ["shared-scope"]

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn
            self._lock = threading.RLock()

        def _require_conn(self) -> sqlite3.Connection:
            return self._conn

    archived = archive_memories(Provider(conn), ["forgotten"], reason="user-request", actor="scope_recall_forget", batch_id="restore-gap")
    assert archived["archived"] == 1
    assert _metadata(conn, "forgotten")["lifecycle"] == "archived"

    dry = rollback_cleanup_batch(conn, batch_id="restore-gap", dry_run=True)
    assert dry["rollback_candidates"] == 1
    assert dry["restore_ids"] == ["forgotten"]

    applied = rollback_cleanup_batch(conn, batch_id="restore-gap", dry_run=False)
    assert applied["restored"] == 1
    assert _metadata(conn, "forgotten").get("lifecycle") != "archived"


def test_cleanup_rollback_dry_run_is_query_only_on_readonly_connection(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    ensure_schema(writer)
    _insert(writer, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(writer, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-readonly-rollback")
    writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        dry = rollback_cleanup_batch(readonly, batch_id="batch-readonly-rollback", dry_run=True)
    finally:
        readonly.close()

    assert dry["dry_run"] is True
    assert dry["rollback_candidates"] == 1
    assert dry["restored"] == 0


def test_cleanup_rollback_replaces_archive_metadata_exactly():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-exact-restore")
    archived = _metadata(conn, "ops")
    assert archived["archived_at"]
    assert archived["archived_by"]

    applied = rollback_cleanup_batch(conn, batch_id="batch-exact-restore", dry_run=False)

    restored = _metadata(conn, "ops")
    assert applied["restored"] == 1
    assert restored.get("lifecycle") != "archived"
    for key in (
        "archive_reason",
        "archived_at",
        "archived_by",
        "candidate_status",
        "candidate_promotion_batch_id",
    ):
        assert key not in restored


def test_cleanup_rollback_refuses_metadata_drift_with_same_batch_marker():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-drift")
    _update_metadata(conn, "ops", {"operator_note": "later decision"})
    before = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'ops'"
    ).fetchone()

    applied = rollback_cleanup_batch(conn, batch_id="batch-drift", dry_run=False)
    after = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'ops'"
    ).fetchone()

    assert applied["restored"] == 0
    assert tuple(after) == tuple(before)
    assert _metadata(conn, "ops")["operator_note"] == "later decision"


def test_cleanup_rollback_malformed_before_json_is_skipped_without_mutation():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="batch-malformed")
    before_row = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'ops'"
    ).fetchone()
    conn.execute(
        "UPDATE governance_audit_events SET before_json = ? WHERE batch_id = ? AND action = 'soft_archive'",
        ("{not-json", "batch-malformed"),
    )
    conn.commit()

    applied = rollback_cleanup_batch(conn, batch_id="batch-malformed", dry_run=False)
    after_row = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'ops'"
    ).fetchone()

    assert applied["restored"] == 0
    assert applied["skipped_invalid"] == 1
    assert _metadata(conn, "ops")["lifecycle"] == "archived"
    assert tuple(after_row) == tuple(before_row)


def test_cleanup_rollback_rejects_archived_before_snapshot_and_never_reports_noop_restored():
    conn = _conn()
    _insert(
        conn,
        memory_id="already-archived-before",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="archived-before-noop",
    )
    receipt = conn.execute(
        "SELECT after_json FROM governance_audit_events "
        "WHERE batch_id='archived-before-noop' AND action='soft_archive'"
    ).fetchone()
    conn.execute(
        "UPDATE governance_audit_events SET before_json = ? "
        "WHERE batch_id='archived-before-noop' AND action='soft_archive'",
        (receipt["after_json"],),
    )
    conn.commit()

    dry = rollback_cleanup_batch(
        conn,
        batch_id="archived-before-noop",
        dry_run=True,
    )
    applied = rollback_cleanup_batch(
        conn,
        batch_id="archived-before-noop",
        dry_run=False,
    )

    assert dry["restore_ids"] == []
    assert dry["invalid_ids"] == ["already-archived-before"]
    assert applied["restored"] == 0
    assert applied["invalid_ids"] == ["already-archived-before"]
    assert _metadata(conn, "already-archived-before")["lifecycle"] == "archived"
    assert conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events "
        "WHERE batch_id='archived-before-noop' AND action='rollback_soft_archive'"
    ).fetchone()[0] == 0


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


def test_governance_audit_coverage_splits_legacy_and_new_archive_debt():
    conn = _conn()
    _insert(conn, memory_id="legacy", content="Historical archived memory kept for rollback evidence.")
    _insert(conn, memory_id="new-missing", content="New archived memory whose mutation should have an audit event.")
    _insert(conn, memory_id="audited", content="Journal digest memory decision/workflow about release.")
    _update_metadata(conn, "legacy", {"lifecycle": "archived"})
    _update_metadata(
        conn,
        "new-missing",
        {"lifecycle": "archived", "archived_by": "forgetting.py", "archived_at": "2026-01-01T00:00:00+00:00", "rollback_batch_id": "new-batch"},
    )
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="audited-batch")

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_repair"
    assert report["archived_total"] == 3
    assert report["archived_with_audit"] == 1
    assert report["archived_without_audit"] == 2
    assert report["new_mutation_coverage"]["archived_total"] == 2
    assert report["new_mutation_coverage"]["missing_audit"] == 1
    assert report["new_mutation_coverage"]["ok"] is False
    assert report["legacy_coverage"]["archived_total"] == 1
    assert report["legacy_coverage"]["backfill_candidates"] == 1
    assert report["samples"]["new_missing_audit"][0]["id"] == "new-missing"
    assert report["samples"]["legacy_missing_audit"][0]["id"] == "legacy"


@pytest.mark.parametrize(
    ("event_type", "action"),
    [
        ("memory_quality_lint", "archive_lint_hit"),
        ("memory_quality_cleanup", "archive_active_lint_hit"),
    ],
)
def test_governance_audit_coverage_counts_memory_quality_archive_events(event_type: str, action: str):
    conn = _conn()
    _insert(conn, memory_id="quality-archived", content="Noisy nightly digest summary archived by quality lint.")
    _update_metadata(
        conn,
        "quality-archived",
        {"lifecycle": "archived", "archived_batch_id": "quality-batch", "archived_reason": "memory_quality_lint_operator_review"},
    )
    row = conn.execute("SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories WHERE id='quality-archived'").fetchone()
    snapshot = dict(row)
    record_governance_audit_event(
        conn,
        event_id="gov_quality_archived",
        event_type=event_type,
        action=action,
        scope_id="shared-scope",
        target_id="quality-archived",
        batch_id="quality-batch",
        before=snapshot,
        after=snapshot,
        reason="fixture quality lint archive",
        actor="test",
        dry_run=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "ready"
    assert report["archived_total"] == 1
    assert report["archived_with_audit"] == 1
    assert report["archived_without_audit"] == 0
    assert report["new_mutation_coverage"] == {"archived_total": 1, "with_audit": 1, "missing_audit": 0, "coverage_percent": 100.0, "ok": True}
    assert report["legacy_coverage"]["backfill_candidates"] == 0


def test_unknown_writer_known_soft_archive_is_not_trusted_for_coverage():
    conn = _conn()
    _insert(conn, memory_id="rogue-archive", content="Archive receipt from an unknown writer using a known action.")
    before = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id='rogue-archive'"
        ).fetchone()
    )
    _update_metadata(
        conn,
        "rogue-archive",
        {"lifecycle": "archived", "rollback_batch_id": "rogue-batch"},
    )
    after = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id='rogue-archive'"
        ).fetchone()
    )
    record_governance_audit_event(
        conn,
        event_id="gov_rogue_soft_archive",
        event_type="third_party_writer",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="rogue-archive",
        batch_id="rogue-batch",
        before=before,
        after=after,
        reason="unknown writer with a known action must fail closed",
        actor="test",
        dry_run=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_repair"
    assert report["archived_with_audit"] == 0
    assert report["new_mutation_coverage"]["missing_audit"] == 1


def test_rollback_rejects_unknown_event_type_without_mapping_to_soft_archive():
    conn = _conn()
    _insert(conn, memory_id="rogue-restore", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="legal-batch")
    conn.execute(
        "UPDATE governance_audit_events SET event_type = 'rogue_writer' "
        "WHERE batch_id = 'legal-batch' AND action = 'soft_archive'"
    )
    conn.commit()
    before_row = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'rogue-restore'"
    ).fetchone()

    applied = rollback_cleanup_batch(
        conn,
        batch_id="legal-batch",
        dry_run=False,
        event_types=["rogue_writer"],
    )
    after_row = conn.execute(
        "SELECT metadata, updated_at FROM memories WHERE id = 'rogue-restore'"
    ).fetchone()

    assert applied["restored"] == 0
    assert applied["rollback_candidates"] == 0
    assert applied["unsupported_event_types"] == ["rogue_writer"]
    assert _metadata(conn, "rogue-restore")["lifecycle"] == "archived"
    assert tuple(after_row) == tuple(before_row)


def test_recognized_archive_receipt_pairs_count_as_coverage():
    conn = _conn()
    _insert(conn, memory_id="legal-archive", content="Memory archived by a recognized writer pair.")
    _update_metadata(
        conn,
        "legal-archive",
        {"lifecycle": "archived", "rollback_batch_id": "legal-pair"},
    )
    row = conn.execute(
        "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
        "FROM memories WHERE id='legal-archive'"
    ).fetchone()
    snapshot = dict(row)
    record_governance_audit_event(
        conn,
        event_id="gov_legal_forget",
        event_type="forgetting",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="legal-archive",
        batch_id="legal-pair",
        before=snapshot,
        after=snapshot,
        reason="recognized forgetting archive pair",
        actor="test",
        dry_run=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["archived_with_audit"] == 1
    assert report["new_mutation_coverage"]["missing_audit"] == 0
    assert report["new_mutation_coverage"]["ok"] is True


def test_generic_archive_action_is_not_trusted_for_coverage_or_rollback():
    conn = _conn()
    _insert(conn, memory_id="untrusted-archive", content="Archive event from an unknown writer.")
    before = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id='untrusted-archive'"
        ).fetchone()
    )
    _update_metadata(
        conn,
        "untrusted-archive",
        {
            "lifecycle": "archived",
            "candidate_promotion_batch_id": "untrusted-batch",
        },
    )
    after = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id='untrusted-archive'"
        ).fetchone()
    )
    record_governance_audit_event(
        conn,
        event_id="gov_untrusted_archive",
        event_type="third_party_writer",
        action="archive",
        scope_id="shared-scope",
        target_id="untrusted-archive",
        batch_id="untrusted-batch",
        before=before,
        after=after,
        reason="fixture unknown archive writer",
        actor="test",
        dry_run=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()

    coverage = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])
    rollback = rollback_cleanup_batch(
        conn,
        batch_id="untrusted-batch",
        dry_run=True,
        event_types=["third_party_writer"],
    )

    assert coverage["new_mutation_coverage"]["missing_audit"] == 1
    assert rollback["rollback_candidates"] == 0


def test_governance_audit_coverage_treats_archived_at_only_rows_as_legacy():
    conn = _conn()
    _insert(conn, memory_id="archived-at-only", content="Historical archive row that predates governance audit coverage.")
    _update_metadata(conn, "archived-at-only", {"lifecycle": "archived", "archived_at": "2026-01-01T00:00:00+00:00"})

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_review"
    assert report["new_mutation_coverage"]["archived_total"] == 0
    assert report["new_mutation_coverage"]["missing_audit"] == 0
    assert report["legacy_coverage"]["archived_total"] == 1
    assert report["legacy_coverage"]["backfill_candidates"] == 1


def test_stale_archive_receipt_does_not_cover_later_unaudited_rearchive():
    conn = _conn()
    _insert(conn, memory_id="ops", content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。")
    apply_cleanup(conn, scope_ids=["shared-scope"], dry_run=False, limit=20, batch_id="stale-receipt")
    covered = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])
    assert covered["status"] == "ready"
    assert covered["new_mutation_coverage"]["missing_audit"] == 0

    metadata = _metadata(conn, "ops")
    metadata["later_operator_review"] = True
    conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = 'ops'",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), "2026-08-01T00:00:00+00:00"),
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_repair"
    assert report["archived_total"] == 1
    assert report["archived_with_audit"] == 0
    assert report["archived_without_audit"] == 1
    assert report["new_mutation_coverage"]["missing_audit"] == 1
    assert report["new_mutation_coverage"]["ok"] is False


def test_only_latest_recognized_archive_receipt_can_cover_current_state():
    conn = _conn()
    _insert(
        conn,
        memory_id="ops",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="first-receipt",
    )
    covered = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])
    assert covered["archived_with_audit"] == 1

    current = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id = 'ops'"
        ).fetchone()
    )
    stale_after = dict(current)
    stale_after["updated_at"] = "2099-01-01T00:00:00+00:00"
    record_governance_audit_event(
        conn,
        event_id="gov_later_stale_receipt",
        event_type="forgetting",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="ops",
        batch_id="later-receipt",
        before=current,
        after=stale_after,
        reason="latest recognized receipt deliberately does not bind current truth",
        actor="test",
        dry_run=False,
        created_at="2099-01-01T00:00:00+00:00",
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_repair"
    assert report["archived_with_audit"] == 0
    assert report["archived_without_audit"] == 1


def test_latest_archive_receipt_uses_write_order_when_timestamps_tie():
    conn = _conn()
    _insert(
        conn,
        memory_id="ops",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="timestamp-tie-base",
    )
    current = dict(
        conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at, metadata "
            "FROM memories WHERE id = 'ops'"
        ).fetchone()
    )
    stale_after = dict(current)
    stale_after["updated_at"] = "2099-01-01T00:00:01+00:00"
    tied_at = "2099-01-01T00:00:00+00:00"
    record_governance_audit_event(
        conn,
        event_id="gov_z_matching_written_first",
        event_type="forgetting",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="ops",
        batch_id="timestamp-tie-matching",
        before=current,
        after=current,
        reason="matching receipt written first",
        actor="test",
        dry_run=False,
        created_at=tied_at,
    )
    record_governance_audit_event(
        conn,
        event_id="gov_a_stale_written_last",
        event_type="forgetting",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="ops",
        batch_id="timestamp-tie-stale",
        before=current,
        after=stale_after,
        reason="stale receipt written last at the same timestamp",
        actor="test",
        dry_run=False,
        created_at=tied_at,
    )
    conn.commit()

    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert report["status"] == "needs_repair"
    assert report["archived_with_audit"] == 0


def test_rollback_deduplicates_same_target_receipts_within_one_batch():
    conn = _conn()
    _insert(
        conn,
        memory_id="ops",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="duplicate-target-batch",
    )
    original = conn.execute(
        "SELECT before_json, after_json FROM governance_audit_events "
        "WHERE batch_id = 'duplicate-target-batch'"
    ).fetchone()
    assert original is not None
    record_governance_audit_event(
        conn,
        event_id="gov_duplicate_target_receipt",
        event_type="memory_cleanup",
        action="soft_archive",
        scope_id="shared-scope",
        target_id="ops",
        batch_id="duplicate-target-batch",
        before=json.loads(original["before_json"]),
        after=json.loads(original["after_json"]),
        reason="duplicate exact receipt for the same target and batch",
        actor="test",
        dry_run=False,
        created_at="2099-01-01T00:00:00+00:00",
    )
    conn.commit()

    dry = rollback_cleanup_batch(
        conn,
        batch_id="duplicate-target-batch",
        dry_run=True,
    )
    applied = rollback_cleanup_batch(
        conn,
        batch_id="duplicate-target-batch",
        dry_run=False,
    )

    assert dry["rollback_candidates"] == 1
    assert dry["restore_ids"] == ["ops"]
    assert applied["restored"] == 1
    assert applied["status"] == "restored"


def test_backfill_legacy_archive_audit_records_existing_archived_state():
    conn = _conn()
    _insert(conn, memory_id="legacy", content="Historical archived memory kept for rollback evidence.")
    _insert(conn, memory_id="new-missing", content="New archived memory whose mutation should have an audit event.")
    _update_metadata(conn, "legacy", {"lifecycle": "archived"})
    _update_metadata(conn, "new-missing", {"lifecycle": "archived", "archived_by": "forgetting.py", "rollback_batch_id": "new-batch"})
    before_changes = conn.total_changes

    dry = backfill_legacy_archive_audit(conn, scope_ids=["shared-scope"], dry_run=True, batch_id="legacy-backfill")

    assert conn.total_changes == before_changes
    assert dry["candidate_count"] == 1
    assert dry["backfilled"] == 0
    assert dry["backfill_ids"] == ["legacy"]

    applied = backfill_legacy_archive_audit(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="legacy-backfill")
    report = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])

    assert applied["backfilled"] == 1
    assert _metadata(conn, "legacy")["lifecycle"] == "archived"
    assert report["legacy_coverage"]["missing_audit"] == 0
    assert report["new_mutation_coverage"]["missing_audit"] == 1
    audit = conn.execute("SELECT action, target_id, before_json, after_json FROM governance_audit_events WHERE batch_id = 'legacy-backfill'").fetchone()
    assert audit["action"] == "legacy_archive_backfill"
    assert audit["target_id"] == "legacy"
    assert json.loads(audit["before_json"]) == json.loads(audit["after_json"])


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


def test_governance_cleanup_cli_fails_when_apply_rollback_is_blocked(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(
        conn,
        memory_id="blocked",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="blocked-cli-batch",
    )
    conn.execute(
        "UPDATE governance_audit_events SET before_json = ? WHERE batch_id = ?",
        (json.dumps({"metadata": {}}), "blocked-cli-batch"),
    )
    conn.commit()
    conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/governance.cleanup.py",
            "--db",
            str(db_path),
            "--rollback-batch",
            "--batch-id",
            "blocked-cli-batch",
            "--apply",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["result"]["status"] == "blocked"


def test_governance_audit_coverage_cli_reports_legacy_backfill_candidates(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="legacy", content="Historical archived memory kept for rollback evidence.")
    _update_metadata(conn, "legacy", {"lifecycle": "archived"})
    conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/governance.audit_coverage.py",
            "--db",
            str(db_path),
            "--dry-run",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["before"]["legacy_coverage"]["backfill_candidates"] == 1
    assert payload["result"]["dry_run"] is True
    assert payload["result"]["candidate_count"] == 1


def test_rollback_rejects_before_metadata_without_explicit_lifecycle():
    conn = _conn()
    _insert(
        conn,
        memory_id="incomplete-before",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="incomplete-before-batch",
    )
    conn.execute(
        "UPDATE governance_audit_events SET before_json = ? WHERE batch_id = ?",
        (json.dumps({"metadata": {}}), "incomplete-before-batch"),
    )
    conn.commit()

    dry = rollback_cleanup_batch(
        conn,
        batch_id="incomplete-before-batch",
        dry_run=True,
    )
    applied = rollback_cleanup_batch(
        conn,
        batch_id="incomplete-before-batch",
        dry_run=False,
    )

    assert dry["restore_ids"] == []
    assert dry["invalid_ids"] == ["incomplete-before"]
    assert applied["restored"] == 0
    assert _metadata(conn, "incomplete-before")["lifecycle"] == "archived"


def test_archive_receipt_and_rollback_bind_current_content():
    conn = _conn()
    _insert(
        conn,
        memory_id="content-drift",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    apply_cleanup(
        conn,
        scope_ids=["shared-scope"],
        dry_run=False,
        limit=20,
        batch_id="content-drift-batch",
    )
    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("tampered archived content", "content-drift"),
    )
    conn.commit()

    coverage = governance_audit_coverage_report(conn, scope_ids=["shared-scope"])
    dry = rollback_cleanup_batch(
        conn,
        batch_id="content-drift-batch",
        dry_run=True,
    )
    applied = rollback_cleanup_batch(
        conn,
        batch_id="content-drift-batch",
        dry_run=False,
    )

    assert coverage["archived_with_audit"] == 0
    assert coverage["archived_without_audit"] == 1
    assert dry["restore_ids"] == []
    assert dry["stale_ids"] == ["content-drift"]
    assert applied["restored"] == 0
    assert applied["status"] == "blocked"
    assert conn.execute(
        "SELECT content FROM memories WHERE id = 'content-drift'"
    ).fetchone()[0] == "tampered archived content"
