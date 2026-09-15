"""P13 v4 bounded storage fault probes (TEST data only)."""
from __future__ import annotations

import hashlib
import sqlite3

from scope_recall.core import CoreConfig, MemoryCore
from tests.v11_support import context


def test_p13_sqlite_full_keeps_core_source_transaction_uncommitted(tmp_path):
    """A real SQLite page limit must fail the write and leave no source row."""
    ctx = context(tmp_path / "sqlite-full")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    database = core.storage.path
    content = "Z" * 60_000
    values = (
        "p13-full-event", "p13-full", 1, "p13-full", 0, None,
        "TEST-scope", "TEST-session", "P13", "main", "human_direct", "user",
        content, hashlib.sha256(content.encode()).hexdigest(), "p13-full-hash",
        "2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z",
        "instant", "complete", None, "P13-V4-SQLITE-FULL", None, "{}", "[]", 0, 0,
    )
    columns = (
        "event_id,source_event_key,source_revision,source_group_key,segment_index,segment_total,"
        "scope_id,session_id,project_id,branch_id,origin,role,content,content_sha256,event_sha256,"
        "occurred_at,recorded_at,persisted_at,time_precision,capture_state,source_original_origin,"
        "dataset_id,import_provenance_sha256,extra_json,capture_gaps_json,read_blocked,suppressed"
    )
    with sqlite3.connect(database, timeout=0.2) as connection:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        connection.execute(f"PRAGMA max_page_count={page_count}")
        connection.commit()
        try:
            connection.execute(
                f"INSERT INTO source_events({columns}) VALUES ({','.join('?' for _ in values)})",
                values,
            )
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            assert "database or disk is full" in str(exc).lower()
        else:
            raise AssertionError("SQLite page limit did not produce SQLITE_FULL")
        assert connection.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
