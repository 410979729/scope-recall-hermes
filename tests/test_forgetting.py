from __future__ import annotations

import json
import sqlite3

from scope_recall.forgetting import build_forgetting_report, run_forgetting
from scope_recall.sql_store import ensure_schema, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    target: str = "memory",
    source: str = "tool-store",
    content: str = "Memory row for forgetting tests.",
    allow_duplicate: bool = False,
    metadata: dict | None = None,
):
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="local-scope" if target == "general" else "shared-scope",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source=source,
        target=target,
        content=content,
        allow_duplicate=allow_duplicate,
        metadata=metadata,
    )


def _metadata(conn: sqlite3.Connection, memory_id: str) -> dict:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    assert row is not None
    return json.loads(row["metadata"] or "{}")


def test_forgetting_report_is_read_only_and_finds_soft_archive_candidates():
    conn = _conn()
    _insert(conn, memory_id="assistant-1", target="general", source="turn-assistant", content="Assistant scratch prose.")
    _insert(conn, memory_id="short-1", target="memory", content="tiny")
    _insert(conn, memory_id="dup-1", target="memory", content="Duplicate durable note for forgetting.", allow_duplicate=True)
    _insert(conn, memory_id="dup-2", target="memory", content="Duplicate durable note for forgetting.", allow_duplicate=True)
    _insert(conn, memory_id="keep-1", target="project", content="Joy 决定：scope-recall 需要自动经验提取与遗忘机制。")

    before = conn.total_changes
    report = build_forgetting_report(conn, accessible_scope_ids=["shared-scope", "local-scope"], limit=20)
    after = conn.total_changes

    assert after == before
    assert report["total_rows"] == 5
    assert report["soft_archive_candidates"]["count"] >= 3
    assert any(item["id"] == "assistant-1" for item in report["soft_archive_candidates"]["items"])
    assert any(item["id"] == "short-1" for item in report["soft_archive_candidates"]["items"])
    assert report["duplicate_groups"]["count"] == 1
    assert report["hard_delete_candidates"]["count"] == 0


def test_forgetting_report_flags_journal_template_transcript_noise_for_soft_archive():
    conn = _conn()
    _insert(
        conn,
        memory_id="template-noise",
        target="ops",
        source="journal-digest",
        content="Operations workflow summary from journal digest: user: 继续 assistant: 完成：测试通过。",
    )
    _insert(conn, memory_id="keep-1", target="project", content="Joy 决定：scope-recall 需要自动经验提取与遗忘机制。")

    report = build_forgetting_report(conn, accessible_scope_ids=["shared-scope", "local-scope"], limit=20)

    assert any(
        item["id"] == "template-noise" and item["reason"] == "journal-template-transcript-noise"
        for item in report["soft_archive_candidates"]["items"]
    )
    assert not any(item["id"] == "keep-1" for item in report["soft_archive_candidates"]["items"])


def test_forgetting_run_soft_archives_without_physical_delete_by_default():
    conn = _conn()
    _insert(conn, memory_id="assistant-1", target="general", source="turn-assistant", content="Assistant scratch prose.")
    _insert(conn, memory_id="dup-1", target="memory", content="Duplicate durable note for forgetting.", allow_duplicate=True)
    _insert(conn, memory_id="dup-2", target="memory", content="Duplicate durable note for forgetting.", allow_duplicate=True)
    _insert(conn, memory_id="keep-1", target="project", content="Joy 决定：scope-recall 需要自动经验提取与遗忘机制。")

    dry = run_forgetting(conn, accessible_scope_ids=["shared-scope", "local-scope"], dry_run=True)
    assert dry["archived"] >= 2
    assert _metadata(conn, "assistant-1").get("lifecycle") != "archived"

    applied = run_forgetting(conn, accessible_scope_ids=["shared-scope", "local-scope"], dry_run=False)
    assert applied["archived"] >= 2
    assert applied["deleted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 4

    assistant_meta = _metadata(conn, "assistant-1")
    assert assistant_meta["lifecycle"] == "archived"
    assert assistant_meta["forget_reason"] == "assistant-prose-scratch"
    assert assistant_meta["archived_at"]

    dup2_meta = _metadata(conn, "dup-2")
    assert dup2_meta["lifecycle"] == "archived"
    assert dup2_meta["superseded_by"] == "dup-1"

    assert _metadata(conn, "keep-1").get("lifecycle") != "archived"

    second = run_forgetting(conn, accessible_scope_ids=["shared-scope", "local-scope"], dry_run=False)
    assert second["archived"] == 0


def test_forgetting_soft_archive_persists_after_connection_reopen(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="assistant-1", target="general", source="turn-assistant", content="Assistant scratch prose.")

    applied = run_forgetting(conn, accessible_scope_ids=["local-scope"], dry_run=False)
    assert applied["archived"] == 1
    conn.close()

    reopened = sqlite3.connect(db_path)
    reopened.row_factory = sqlite3.Row
    try:
        metadata = _metadata(reopened, "assistant-1")
        assert metadata["lifecycle"] == "archived"
        assert metadata["forget_reason"] == "assistant-prose-scratch"
    finally:
        reopened.close()


class FakeVectorStore:
    def __init__(self):
        self.deleted_ids: list[list[str]] = []

    def delete_by_ids(self, ids: list[str]) -> None:
        self.deleted_ids.append(list(ids))


def test_forgetting_hard_delete_removes_vector_records():
    conn = _conn()
    secret = "sk-" + "F" * 24
    _insert(conn, memory_id="secret-row", target="ops", content="Temporary api_key=" + secret + " should be hard-deleted.")
    _insert(
        conn,
        memory_id="keep-row",
        target="ops",
        content="Durable safe ops memory should stay.",
        metadata={
            "relation_types": ["contradicts"],
            "conflict_review_ids": ["secret-row"],
            "conflict_count": 1,
            "conflict_review_count": 1,
            "needs_conflict_review": True,
        },
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES (?, ?, 'contradicts', 1.0, 'delete cleanup test', '2026-01-01T00:00:00+00:00')
        """,
        ("secret-row", "keep-row"),
    )
    vector_store = FakeVectorStore()

    applied = run_forgetting(
        conn,
        accessible_scope_ids=["shared-scope", "local-scope"],
        dry_run=False,
        hard_delete=True,
        vector_store=vector_store,
    )

    assert applied["deleted"] == 1
    assert applied["delete_ids"] == ["secret-row"]
    assert vector_store.deleted_ids == [["secret-row"]]
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", ("secret-row",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", ("keep-row",)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?", ("secret-row", "secret-row")).fetchone()[0] == 0
    keep_meta = _metadata(conn, "keep-row")
    assert keep_meta["conflict_review_ids"] == []
    assert keep_meta["conflict_count"] == 0
    assert keep_meta["conflict_review_count"] == 0
    assert keep_meta["needs_conflict_review"] is False
    assert "contradicts" not in keep_meta["relation_types"]
