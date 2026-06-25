from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import subprocess
import sys

from scope_recall.forgetting import _archive_memory
from scope_recall.graph_relations import backfill_supersedes_from_metadata, graph_relation_stats, upsert_relation
from scope_recall.sql_store import ensure_schema


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    scope_id: str = "scope-a",
    content: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    text = content or f"Memory {memory_id} content"
    now = "2026-06-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, source, target, content,
            summary, metadata, created_at, updated_at
        )
        VALUES (?, ?, 'cli', 'joy', '', '', '', 'yuheng', 'hermes', 'test-session',
                'tool-store', 'project', ?, ?, ?, ?, ?)
        """,
        (memory_id, scope_id, text, text, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), now, now),
    )
    conn.commit()


def _relation_count(conn: sqlite3.Connection, source_id: str, target_id: str, relation_type: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = ?
            """,
            (source_id, target_id, relation_type),
        ).fetchone()[0]
    )


def test_upsert_relation_is_idempotent_and_rejects_invalid_edges(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_memory(conn, "new")
        _insert_memory(conn, "old")

        assert upsert_relation(conn, source_memory_id="new", target_memory_id="old", relation_type="supersedes", confidence=2.0)
        assert not upsert_relation(conn, source_memory_id="new", target_memory_id="old", relation_type="supersedes", confidence=0.4)
        assert not upsert_relation(conn, source_memory_id="new", target_memory_id="new", relation_type="supersedes")
        assert not upsert_relation(conn, source_memory_id="new", target_memory_id="old", relation_type="semantic_guess")
        assert not upsert_relation(conn, source_memory_id="new", target_memory_id="missing", relation_type="supports")

        row = conn.execute("SELECT confidence FROM memory_relations WHERE source_memory_id = 'new' AND target_memory_id = 'old'").fetchone()
        assert row["confidence"] == 1.0
        assert _relation_count(conn, "new", "old", "supersedes") == 1
    finally:
        conn.close()


def test_backfill_supersedes_from_metadata_is_dry_run_safe_and_apply_idempotent(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_memory(conn, "new")
        _insert_memory(conn, "old", metadata={"lifecycle": "archived", "superseded_by": "new"})
        _insert_memory(conn, "missing-old", metadata={"lifecycle": "archived", "superseded_by": "missing-new"})
        _insert_memory(conn, "self-old", metadata={"lifecycle": "archived", "superseded_by": "self-old"})
        _insert_memory(conn, "cross-new", scope_id="scope-b")
        _insert_memory(conn, "cross-old", metadata={"lifecycle": "archived", "superseded_by": "cross-new"})
        _insert_memory(conn, "hidden-new", metadata={"lifecycle": "archived"})
        _insert_memory(conn, "hidden-old", metadata={"lifecycle": "archived", "superseded_by": "hidden-new"})

        dry_run = backfill_supersedes_from_metadata(conn, apply=False)
        assert dry_run["dry_run"] is True
        assert dry_run["candidate_supersedes"] == 5
        assert dry_run["inserted_supersedes"] == 0
        assert dry_run["skipped_missing_target"] == 1
        assert dry_run["skipped_self_relation"] == 1
        assert dry_run["skipped_cross_scope"] == 1
        assert dry_run["skipped_hidden_target"] == 1
        assert _relation_count(conn, "new", "old", "supersedes") == 0

        applied = backfill_supersedes_from_metadata(conn, apply=True)
        conn.commit()
        assert applied["inserted_supersedes"] == 1
        assert _relation_count(conn, "new", "old", "supersedes") == 1

        second = backfill_supersedes_from_metadata(conn, apply=True)
        assert second["inserted_supersedes"] == 0
        assert second["existing_supersedes"] == 1
        assert _relation_count(conn, "new", "old", "supersedes") == 1
    finally:
        conn.close()


def test_graph_relation_stats_reports_density_orphans_and_lifecycle_hidden_peers(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_memory(conn, "new")
        _insert_memory(conn, "old", metadata={"lifecycle": "archived"})
        assert upsert_relation(conn, source_memory_id="new", target_memory_id="old", relation_type="supersedes")
        conn.execute(
            """
            INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES ('new', 'missing', 'supports', 1.0, 'orphan fixture', '2026-06-01T00:00:00+00:00')
            """
        )

        stats = graph_relation_stats(conn, accessible_scope_ids=["scope-a"])

        assert stats["memory_relations"] == 2
        assert stats["relation_types"] == {"supports": 1, "supersedes": 1}
        assert stats["orphan_relations"] == 1
        assert stats["lifecycle_hidden_peer_relations"] == 1
        assert stats["scoped_memory_relations"] == 1
    finally:
        conn.close()


def test_archive_memory_creates_supersedes_edge_only_for_existing_same_scope_replacement(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_memory(conn, "new")
        _insert_memory(conn, "old")
        _insert_memory(conn, "other-new", scope_id="scope-b")
        _insert_memory(conn, "cross-old")
        _insert_memory(conn, "hidden-new", metadata={"lifecycle": "archived"})
        _insert_memory(conn, "hidden-old")
        _insert_memory(conn, "missing-old")

        assert _archive_memory(conn, memory_id="old", reason="test", superseded_by="new") is True
        assert _archive_memory(conn, memory_id="cross-old", reason="test", superseded_by="other-new") is True
        assert _archive_memory(conn, memory_id="hidden-old", reason="test", superseded_by="hidden-new") is True
        assert _archive_memory(conn, memory_id="missing-old", reason="test", superseded_by="missing-new") is True

        assert _relation_count(conn, "new", "old", "supersedes") == 1
        assert _relation_count(conn, "other-new", "cross-old", "supersedes") == 0
        assert _relation_count(conn, "hidden-new", "hidden-old", "supersedes") == 0
        assert _relation_count(conn, "missing-new", "missing-old", "supersedes") == 0
    finally:
        conn.close()


def test_backfill_cli_accepts_explicit_dry_run_flag(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_memory(conn, "new")
        _insert_memory(conn, "old", metadata={"lifecycle": "archived", "superseded_by": "new"})
    finally:
        conn.close()

    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "scripts/backfill.graph_relations.py", "--db-path", str(tmp_path / "memory.sqlite3"), "--dry-run"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["backfill"]["candidate_supersedes"] == 1
    assert payload["backfill"]["inserted_supersedes"] == 0
