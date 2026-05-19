from __future__ import annotations

import sqlite3

from scope_recall.hygiene import build_hygiene_report
from scope_recall.sql_store import ensure_schema, store_row


class FakeVectorStore:
    def __init__(self, records):
        self._records = records

    def list_records(self):
        return dict(self._records)


def _insert(conn, *, memory_id, target="memory", source="tool-store", content="Memory row for hygiene testing.", allow_duplicate=False):
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="local-scope" if target == "general" else "shared-scope",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source=source,
        target=target,
        content=content,
        allow_duplicate=allow_duplicate,
    )


def test_build_hygiene_report_is_read_only_and_flags_quality_categories():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="noise-1", target="general", source="turn-user", content="[Recent Telegram chat history]\nJoy: noisy wrapper")
    _insert(conn, memory_id="assistant-1", target="general", source="turn-assistant", content="Assistant prose should be considered scratch noise.")
    _insert(conn, memory_id="short-1", target="memory", content="tiny")
    _insert(conn, memory_id="long-1", target="memory", content="x" * 2600)
    _insert(conn, memory_id="dup-1", target="memory", content="Duplicate durable note for hygiene.", allow_duplicate=True)
    _insert(conn, memory_id="dup-2", target="memory", content="Duplicate durable note for hygiene.", allow_duplicate=True)
    _insert(conn, memory_id="promote-1", target="general", source="turn-user", content="Joy prefers concise direct answers in Telegram groups.")

    before = conn.total_changes
    report = build_hygiene_report(conn, vector_store=FakeVectorStore({"noise-1": {"id": "noise-1", "target": "general"}}))
    after = conn.total_changes

    assert after == before
    assert report["total_rows"] == 7
    assert report["totals_by_target"]["general"] == 3
    assert report["runtime_wrapper_noise"]["count"] == 1
    assert report["assistant_prose_rows"]["count"] == 1
    assert report["duplicate_dedupe_keys"]["count"] == 1
    assert report["very_short_rows"]["count"] >= 1
    assert report["very_long_rows"]["count"] == 1
    assert report["general_vector_rows"]["count"] == 1
    assert report["fts_index"]["memory_rows"] == 7
    assert report["fts_index"]["fts_rows"] == 7
    assert report["fts_index"]["stale_fts_rows"] == 0
    assert report["fts_index"]["missing_fts_rows"] == 0
    assert report["fts_index"]["duplicate_fts_extra_rows"] == 0
    assert any(item["id"] == "promote-1" for item in report["likely_promotion_candidates"]["items"])
    assert {item["id"] for item in report["likely_delete_candidates"]["items"]} >= {"noise-1", "assistant-1"}


def test_build_hygiene_report_surfaces_stale_missing_and_duplicate_fts_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _insert(conn, memory_id="memory-1", target="memory", content="durable memory row")
    _insert(conn, memory_id="memory-2", target="memory", content="another durable memory row")
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", ("stale-1", "stale", "stale"))
    conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)", ("memory-1", "duplicate", "duplicate"))
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", ("memory-2",))
    conn.commit()

    before = conn.total_changes
    report = build_hygiene_report(conn)
    after = conn.total_changes

    assert after == before
    assert report["fts_index"]["memory_rows"] == 2
    assert report["fts_index"]["fts_rows"] == 3
    assert report["fts_index"]["stale_fts_rows"] == 1
    assert report["fts_index"]["missing_fts_rows"] == 1
    assert report["fts_index"]["duplicate_fts_extra_rows"] == 1
    assert report["fts_index"]["healthy"] is False
