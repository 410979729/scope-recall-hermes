"""Tests for legacy hygiene migration helpers.

They ensure old metadata is normalized without rewriting unrelated memory truth."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.sql_store import ensure_schema, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(conn: sqlite3.Connection, *, memory_id: str, target: str, source: str, content: str, metadata: dict | None = None) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="local-scope" if target == "general" else "shared-scope",
        platform="cli",
        user_id="joy",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="session",
        source=source,
        target=target,
        content=content,
        metadata=json.dumps(metadata or {}, ensure_ascii=False),
        allow_duplicate=True,
    )


def _run_migration(db_path: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "migrate.legacy_hygiene.py"), "--db", str(db_path), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_legacy_hygiene_migration_dry_run_is_read_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _db(db_path)
    _store(conn, memory_id="general-raw", target="general", source="turn-user", content="legacy raw turn that should be archived, not deleted")
    _store(conn, memory_id="durable-missing", target="memory", source="tool-store", content="restart gateway after model changes with uv run")
    conn.execute("UPDATE memories SET metadata = '{}' WHERE id = 'durable-missing'")
    conn.commit()
    before = {row["id"]: row["metadata"] for row in conn.execute("SELECT id, metadata FROM memories")}
    conn.close()

    report = _run_migration(db_path)

    after_conn = sqlite3.connect(db_path)
    after_conn.row_factory = sqlite3.Row
    try:
        after = {row["id"]: row["metadata"] for row in after_conn.execute("SELECT id, metadata FROM memories")}
    finally:
        after_conn.close()
    assert before == after
    assert report["dry_run"] is True
    assert report["planned_archive_legacy_scratch"] == 1
    assert report["planned_normalize_durable_metadata"] == 1
    assert report["applied_archive_legacy_scratch"] == 0
    assert report["applied_normalize_durable_metadata"] == 0


def test_legacy_hygiene_migration_apply_archives_and_normalizes_with_backup(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = _db(db_path)
    _store(conn, memory_id="general-raw", target="general", source="turn-user", content="legacy raw turn that should be archived, not deleted")
    _store(conn, memory_id="durable-missing", target="memory", source="tool-store", content="restart gateway after model changes with uv run")
    conn.execute("UPDATE memories SET metadata = '{}' WHERE id = 'durable-missing'")
    conn.commit()
    conn.close()

    report = _run_migration(db_path, "--apply")

    assert report["dry_run"] is False
    assert report["applied_archive_legacy_scratch"] == 1
    assert report["applied_normalize_durable_metadata"] == 1
    assert Path(report["backup"]).exists()
    assert report["after"]["legacy_scratch_remaining"] == 0
    assert report["after"]["durable_missing_lifecycle_or_category"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        general_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'general-raw'").fetchone()[0])
        durable_meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'durable-missing'").fetchone()[0])
    finally:
        conn.close()
    assert general_meta["lifecycle"] == "archived"
    assert general_meta["category"] == "legacy-scratch"
    assert general_meta["legacy_hygiene"]["action"] == "archive_legacy_scratch"
    assert durable_meta["lifecycle"] == "promoted"
    assert durable_meta["category"] in {"procedure", "fact"}
    assert durable_meta["legacy_hygiene"]["action"] == "normalize_durable_metadata"


def test_recall_filters_archived_lifecycle_rows():
    class Provider:
        _retrieval_config = {}
        _scope_id = "local-scope"

        def _config_value(self, key, default=None):
            return default

    service = RecallService(Provider())
    active = RecallItem("active", "durable fact", "durable fact", "tool-store", "memory", 0.5, "2026-06-13T00:00:00+00:00", {"lifecycle": "promoted"})
    archived = RecallItem("archived", "old raw scratch", "old raw scratch", "turn-user", "general", 0.9, "2026-06-13T00:00:00+00:00", {"lifecycle": "archived"})

    assert service._filter_recall_lifecycle([active, archived]) == [active]
