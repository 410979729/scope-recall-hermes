"""Tests for hygiene report script output and sanitization.

They keep operator reports useful without leaking private memory text."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_vector_store import SQLiteBruteForceVectorStore  # type: ignore[import-not-found]

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_report_hygiene_script_reports_vector_source_and_rows_without_mutating_sqlite(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="general-1",
        scope_id="local-scope",
        platform="cli",
        user_id="joy",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="turn-user",
        target="general",
        content="general scratch row should be visible when indexed",
    )
    conn.close()

    vector_dir = tmp_path / "lancedb"
    try:
        import lancedb
        import pyarrow as pa
    except Exception:
        lancedb = None
        pa = None

    if lancedb is not None and pa is not None:
        vector_dir.mkdir()
        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("scope_id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("target", pa.string()),
                pa.field("content", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("updated_at", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 2)),
            ]
        )
        db = lancedb.connect(str(vector_dir))
        db.create_table(
            "memories",
            data=pa.Table.from_pylist(
                [
                    {
                        "id": "general-1",
                        "scope_id": "local-scope",
                        "source": "turn-user",
                        "target": "general",
                        "content": "general scratch row should be visible when indexed",
                        "summary": "general scratch row should be visible when indexed",
                        "updated_at": "2026-05-18T00:00:00+00:00",
                        "vector": [1.0, 0.0],
                    }
                ],
                schema=schema,
            ),
        )

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "report.hygiene.py"),
            "--db",
            str(db_path),
            "--vector-dir",
            str(vector_dir),
            "--limit",
            "5",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)
    after_conn = sqlite3.connect(db_path)
    try:
        assert after_conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    finally:
        after_conn.close()

    before_conn = sqlite3.connect(db_path)
    try:
        before_total_changes = before_conn.total_changes
        assert before_conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    finally:
        before_conn.close()

    assert before_total_changes == 0
    assert report["vector_report_source"]["path"] == str(vector_dir.resolve())
    if lancedb is not None and pa is not None:
        assert report["vector_report_source"]["enabled"] is True
        assert report["general_vector_rows"]["count"] == 1
        assert report["general_vector_rows"]["items"][0]["id"] == "general-1"
    else:
        assert report["vector_report_source"]["enabled"] is False


def test_report_hygiene_script_reads_sqlite_bruteforce_vector_source(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="general-1",
        scope_id="local-scope",
        platform="cli",
        user_id="joy",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source="turn-user",
        target="general",
        content="general scratch row should be visible from sqlite vector companion",
    )
    conn.close()

    vector_path = tmp_path / "vector.sqlite3"
    store = SQLiteBruteForceVectorStore(vector_path, dimensions=2)
    store.open()
    try:
        store.upsert_records(
            [
                {
                    "id": "general-1",
                    "scope_id": "local-scope",
                    "source": "turn-user",
                    "target": "general",
                    "content": "general scratch row should be visible from sqlite vector companion",
                    "summary": "general scratch row should be visible from sqlite vector companion",
                    "updated_at": "2026-06-09T00:00:00+00:00",
                    "vector": [1.0, 0.0],
                }
            ]
        )
    finally:
        store.close()

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "report.hygiene.py"),
            "--db",
            str(db_path),
            "--vector-backend",
            "sqlite-bruteforce",
            "--vector-dir",
            str(vector_path),
            "--limit",
            "5",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)

    assert report["vector_report_source"]["backend"] == "sqlite-bruteforce"
    assert report["vector_report_source"]["enabled"] is True
    assert report["vector_report_source"]["path"] == str(vector_path.resolve())
    assert report["general_vector_rows"]["count"] == 1
    assert report["general_vector_rows"]["items"][0]["id"] == "general-1"
