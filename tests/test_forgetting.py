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
