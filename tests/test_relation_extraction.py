"""Tests for deterministic relation extraction and graph synchronization.

They protect contradiction, dependency, supersession, and same-topic edge semantics."""

from __future__ import annotations

import json
import sqlite3

from plugins.memory import load_memory_provider

from scope_recall.relation_extraction import extract_relation_candidates, rebuild_extracted_relations, sync_extracted_relations_for_memory
from scope_recall.sql_store import ensure_schema, store_row


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(conn: sqlite3.Connection, *, memory_id: str, content: str, updated_at: str = "2026-01-01T00:00:00+00:00") -> None:
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
        session_id="relation-fixture",
        source="tool-store",
        target="project",
        content=content,
        metadata=json.dumps({"memory_type": "factual", "entities": ["Project Atlas"], "importance": 0.8}, ensure_ascii=False),
        allow_duplicate=True,
    )
    conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (updated_at, memory_id))
    conn.commit()


def test_relation_extraction_dry_run_is_query_only_on_readonly_db(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        _store(writer, memory_id="atlas-old", content="Project Atlas v1 deploy command uses old atlasctl deploy.")
        _store(writer, memory_id="atlas-new", content="Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.")
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        before = readonly.total_changes
        payload = rebuild_extracted_relations(readonly, scope_ids=["shared-scope"], dry_run=True)
    finally:
        readonly.close()

    assert payload["dry_run"] is True
    assert payload["candidate_count"] >= 1
    assert payload["inserted"] == 0
    assert before == 0


def test_relation_extraction_builds_typed_relation_edges():
    conn = _conn()
    _store(conn, memory_id="atlas-old", content="Project Atlas v1 deploy command uses old atlasctl deploy.", updated_at="2026-01-01T00:00:00+00:00")
    _store(
        conn,
        memory_id="atlas-new",
        content="Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    _store(conn, memory_id="redis-runbook", content="Redis service runbook: check redis-cli ping before Atlas deploy.")
    _store(conn, memory_id="atlas-redis", content="Project Atlas deploy depends on Redis service availability.")
    _store(conn, memory_id="platform-team", content="Platform Team owns Redis service operations.")
    _store(conn, memory_id="redis-owner", content="Redis service is owned by Platform Team.")
    _store(conn, memory_id="zephyr-runbook", content="Project Zephyr worker queue drain metrics must be green.")
    _store(conn, memory_id="atlas-affects-zephyr", content="Project Atlas deploy affects Project Zephyr worker queue drain metrics.")

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    pair_types = {(item["source_memory_id"], item["target_memory_id"], item["relation_type"]) for item in candidates}

    assert ("atlas-new", "atlas-old", "supersedes") in pair_types
    assert ("atlas-redis", "redis-runbook", "depends_on") in pair_types
    assert ("redis-owner", "platform-team", "owned_by") in pair_types
    assert ("atlas-affects-zephyr", "zephyr-runbook", "affects") in pair_types
    assert any(item[2] == "same_topic" for item in pair_types)

    result = rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="test-relations")

    assert result["inserted"] >= 6
    rows = conn.execute(
        "SELECT source_memory_id, target_memory_id, relation_type, note FROM memory_relations ORDER BY relation_type, source_memory_id"
    ).fetchall()
    relation_types = {row["relation_type"] for row in rows}
    assert {"same_topic", "supersedes", "depends_on", "owned_by", "affects"} <= relation_types
    assert all(str(row["note"]).startswith("relation-extraction:test-relations") for row in rows)


def test_relation_extraction_preserves_manual_same_key_relation_when_refreshing_generated_edges():
    conn = _conn()
    _store(conn, memory_id="atlas-a", content="Project Atlas deploy runbook validates Redis service before rollout.")
    _store(conn, memory_id="atlas-b", content="Project Atlas deploy checklist validates Redis service before rollout.")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-a', 'atlas-b', 'same_topic', 1.0, 'manual-governance:operator-reviewed', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-b', 'atlas-a', 'same_topic', 0.7, 'relation-extraction:old; stale generated edge', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()

    result = rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="manual-preserve")

    assert result["deleted"] == 1
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type, confidence, note
        FROM memory_relations
        WHERE source_memory_id IN ('atlas-a','atlas-b')
          AND target_memory_id IN ('atlas-a','atlas-b')
        ORDER BY source_memory_id, target_memory_id
        """
    ).fetchall()
    notes = {(row["source_memory_id"], row["target_memory_id"]): row["note"] for row in rows}
    assert notes[("atlas-a", "atlas-b")] == "manual-governance:operator-reviewed"
    assert notes[("atlas-b", "atlas-a")].startswith("relation-extraction:manual-preserve")


def test_relation_extraction_does_not_treat_current_or_latest_as_supersession():
    conn = _conn()
    _store(
        conn,
        memory_id="atlas-old-url",
        content="Project Atlas base URL used old endpoint https://old-atlas.invalid/v1.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="atlas-current-owner",
        content="Project Atlas current owner is Platform Team for rollout reviews.",
        updated_at="2026-02-01T00:00:00+00:00",
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    supersedes_pairs = {
        (item["source_memory_id"], item["target_memory_id"])
        for item in candidates
        if item["relation_type"] == "supersedes"
    }

    assert ("atlas-current-owner", "atlas-old-url") not in supersedes_pairs


def test_sync_relation_extraction_preserves_generated_edges_outside_pair_budget():
    conn = _conn()
    _store(
        conn,
        memory_id="old-a",
        content="Project Atlas deploy runbook validates stable health checks before rollout.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="old-b",
        content="Project Atlas deploy checklist validates stable health checks before rollout.",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('old-a', 'old-b', 'same_topic', 0.82, 'relation-extraction:previous; fixture', '2026-01-02T00:00:00+00:00')
        """
    )
    _store(
        conn,
        memory_id="new-focus",
        content="Project Atlas deploy depends on Redis service availability.",
        updated_at="2026-03-01T00:00:00+00:00",
    )
    _store(
        conn,
        memory_id="new-peer",
        content="Redis service runbook: check redis-cli ping before Atlas deploy.",
        updated_at="2026-02-15T00:00:00+00:00",
    )

    result = sync_extracted_relations_for_memory(
        conn,
        memory_id="new-focus",
        scope_ids=["shared-scope"],
        batch_id="budgeted-sync",
        max_pairs=1,
    )

    assert result["deleted"] == 0
    preserved = conn.execute(
        """
        SELECT relation_type, note
        FROM memory_relations
        WHERE source_memory_id = 'old-a' AND target_memory_id = 'old-b' AND relation_type = 'same_topic'
        """
    ).fetchone()
    assert preserved is not None
    assert preserved["note"] == "relation-extraction:previous; fixture"


def test_relation_extraction_does_not_add_non_conflict_edges_for_contradicting_pair():
    conn = _conn()
    _store(conn, memory_id="atlas-redis", content="Project Atlas deploy depends on Redis service availability.")
    _store(conn, memory_id="redis-runbook", content="Redis service runbook: check redis-cli ping before Atlas deploy.")
    conn.execute(
        """
        INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES('atlas-redis', 'redis-runbook', 'contradicts', 1.0, 'fixture-conflict', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.commit()

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])
    blocked_types = {"same_topic", "supersedes", "depends_on", "owned_by", "affects"}

    assert not [
        item
        for item in candidates
        if {item["source_memory_id"], item["target_memory_id"]} == {"atlas-redis", "redis-runbook"}
        and item["relation_type"] in blocked_types
    ]

    rebuild_extracted_relations(conn, scope_ids=["shared-scope"], dry_run=False, batch_id="conflict-skip")
    rows = conn.execute(
        """
        SELECT source_memory_id, target_memory_id, relation_type, note
        FROM memory_relations
        WHERE source_memory_id IN ('atlas-redis', 'redis-runbook')
           OR target_memory_id IN ('atlas-redis', 'redis-runbook')
        """
    ).fetchall()
    pair_rows = [row for row in rows if {row["source_memory_id"], row["target_memory_id"]} == {"atlas-redis", "redis-runbook"}]
    assert [(row["relation_type"], row["note"]) for row in pair_rows] == [("contradicts", "fixture-conflict")]


def test_update_memory_rebuilds_extracted_relations_for_updated_content(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-update",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        source = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas deploy depends on Redis service availability.",
                    "target": "project",
                    "memory_type": "procedure",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        target = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Redis service runbook: check redis-cli ping before Atlas deploy.",
                    "target": "project",
                    "memory_type": "procedure",
                    "entities": ["Redis service"],
                    "allow_duplicate": True,
                },
            )
        )
        source_id = source["id"]
        target_id = target["id"]
        conn = plugin._require_conn()
        before = conn.execute(
            """
            SELECT relation_type
            FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = 'depends_on'
            """,
            (source_id, target_id),
        ).fetchall()
        assert before

        updated, _, _ = plugin._update_memory(source_id, "Project Atlas release notes mention documentation cleanup only.", "project")
        assert updated is True

        after = conn.execute(
            """
            SELECT relation_type, note
            FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = 'depends_on'
            """,
            (source_id, target_id),
        ).fetchall()
    finally:
        plugin.shutdown()

    assert after == []


def test_provider_store_adds_rebuildable_relation_edges(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-relation-extraction",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    try:
        first = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas v1 deploy command uses old atlasctl deploy.",
                    "target": "project",
                    "memory_type": "factual",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        second = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {
                    "content": "Project Atlas v2 supersedes v1 deploy command and uses uv run atlas deploy.",
                    "target": "project",
                    "memory_type": "factual",
                    "entities": ["Project Atlas"],
                    "allow_duplicate": True,
                },
            )
        )
        with plugin._lock:
            rows = plugin._require_conn().execute(
                """
                SELECT relation_type, source_memory_id, target_memory_id
                FROM memory_relations
                WHERE source_memory_id IN (?, ?) OR target_memory_id IN (?, ?)
                """,
                (first["id"], second["id"], first["id"], second["id"]),
            ).fetchall()
    finally:
        plugin.shutdown()

    assert any(row["relation_type"] == "same_topic" for row in rows)
    assert any(row["relation_type"] == "supersedes" and row["source_memory_id"] == second["id"] for row in rows)
