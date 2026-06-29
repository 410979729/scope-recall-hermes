from __future__ import annotations

import json
import sqlite3

from scope_recall.memory_quality import memory_quality_report
from scope_recall.sql_store import ensure_schema, now_iso
from scope_recall.doctor_sqlite import memory_quality_lint_report


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
    target: str = "ops",
    source: str = "journal-digest",
    metadata: dict | None = None,
) -> None:
    now = now_iso()
    summary = content[:220]
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content, summary,
            created_at, updated_at, last_recalled_turn, dedup_key, metadata
        ) VALUES (?, 'shared-scope', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'session', ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            memory_id,
            source,
            target,
            content,
            summary,
            now,
            now,
            memory_id,
            json.dumps(metadata if metadata is not None else {"memory_type": "workflow"}, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", (memory_id, content, summary))
    conn.commit()


def test_memory_quality_report_flags_active_lint_rules_and_ignores_archived_rows():
    conn = _conn()
    _insert_memory(conn, memory_id="clean", content="Scope Recall release checklist has trigger, verification, and cleanup.")
    _insert_memory(
        conn,
        memory_id="template",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成。",
    )
    _insert_memory(
        conn,
        memory_id="attachment",
        content="Captured screenshot MEDIA:/tmp/hermes-results/scope-recall.png with .pytest_cache context.",
        metadata={"memory_type": "resource"},
    )
    _insert_memory(
        conn,
        memory_id="stale",
        content="Temporary rollout note requires review later.",
        metadata={"memory_type": "episodic", "expires_at": "stale-review"},
    )
    _insert_memory(
        conn,
        memory_id="missing-type",
        content="Durable row without metadata type should be reviewed.",
        metadata={},
    )
    _insert_memory(
        conn,
        memory_id="archived-template",
        content="Journal digest memory: old archived noise should stay out of active lint.",
        metadata={"lifecycle": "archived"},
    )

    before = conn.total_changes
    report = memory_quality_report(conn, sample_limit=10)

    assert conn.total_changes == before
    assert report["status"] == "needs_review"
    assert report["active_rows"] == 5
    assert report["by_rule"]["template_prefix"] == 1
    assert report["by_rule"]["raw_attachment_marker"] == 1
    assert report["by_rule"]["cache_or_tmp_path"] == 1
    assert report["by_rule"]["stale_review_active"] == 1
    assert report["by_rule"]["missing_memory_type"] == 1
    sample_ids = {sample["id"] for sample in report["samples"]}
    assert "archived-template" not in sample_ids
    assert {"template", "attachment", "stale", "missing-type"} <= sample_ids


def test_memory_quality_report_is_query_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        _insert_memory(writer, memory_id="template", content="Journal digest memory: noisy template prefix.")
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        report = memory_quality_report(readonly)
    finally:
        readonly.close()

    assert report["active_lint_hits"] >= 1
    assert report["by_rule"]["template_prefix"] == 1


def test_memory_quality_accepts_legacy_category_as_memory_type():
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="legacy-category",
        content="Stable workflow memory with legacy category metadata.",
        metadata={"category": "workflow"},
    )

    report = memory_quality_report(conn, sample_limit=10)

    assert report["active_lint_hits"] == 0
    assert "missing_memory_type" not in report["by_rule"]


def test_memory_quality_ignores_candidate_review_debt_and_cache_cleanup_mentions():
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="candidate-stale",
        content="Candidate memory waiting for review should be handled by candidate debt lanes.",
        metadata={"lifecycle": "candidate", "memory_type": "workflow", "expires_at": "stale-review"},
    )
    _insert_memory(
        conn,
        memory_id="cache-cleanup-workflow",
        content="Release cleanup workflow: remove __pycache__ and .pytest_cache after tests pass.",
        metadata={"memory_type": "workflow"},
    )

    report = memory_quality_report(conn, sample_limit=10)

    assert report["active_rows"] == 1
    assert report["active_lint_hits"] == 0


def test_memory_quality_flags_raw_tmp_paths_but_not_long_structured_notes():
    conn = _conn()
    _insert_memory(
        conn,
        memory_id="raw-tmp",
        content="Captured artifact path /tmp/hermes-results/scope-recall-debug.json should not remain in durable memory.",
        metadata={"memory_type": "resource"},
    )
    _insert_memory(
        conn,
        memory_id="long-structured",
        content="Scope Recall structured release checklist. " + "verify docs and package metadata. " * 120,
        metadata={"memory_type": "workflow"},
    )

    report = memory_quality_report(conn, sample_limit=10)

    assert report["by_rule"] == {"cache_or_tmp_path": 1}
    sample_ids = {sample["id"] for sample in report["samples"]}
    assert sample_ids == {"raw-tmp"}


def test_memory_quality_report_handles_missing_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    report = memory_quality_report(conn)

    assert report["status"] == "schema_missing"
    assert report["active_rows"] == 0
    assert report["active_lint_hits"] == 0


def test_doctor_memory_quality_lint_report_is_read_only_and_recommends_review(tmp_path):
    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        _insert_memory(writer, memory_id="template", content="Journal digest memory: noisy template prefix.")
    finally:
        writer.close()

    payload, check, recommendations = memory_quality_lint_report(tmp_path)

    assert payload["status"] == "needs_review"
    assert payload["active_lint_hits"] >= 1
    assert payload["by_rule"]["template_prefix"] == 1
    assert check == {"ok": True, "failures": []}
    assert recommendations == [
        "Active memory quality lint found 1 rule hits; review runtime.memory_quality_lint samples before promoting or exporting memory."
    ]
