"""Tests for graph companion hygiene repair and orphan detection.

Graph rows are rebuildable evidence, so repairs must compare against SQLite truth."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.graph import ensure_graph_schema
from scope_recall.graph_hygiene import (
    graph_hygiene_counts,
    repair_graph_hygiene,
    repair_relation_rebuild_debt,
)
from scope_recall.sql_store import ensure_schema


def _conn(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_graph_schema(conn)
    return conn


def test_graph_hygiene_dry_run_reports_planned_deletes_without_mutating(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO memories(id, scope_id, content, summary, source, target, metadata, created_at, updated_at) VALUES ('archived-1', 'scope-a', 'old', 'old', 'test', 'memory', ?, 'now', 'now')",
            (json.dumps({"lifecycle": "archived"}, sort_keys=True),),
        )
        conn.execute(
            "INSERT INTO memories(id, scope_id, content, summary, source, target, metadata, created_at, updated_at) VALUES ('active-1', 'scope-a', 'new', 'new', 'test', 'memory', '{}', 'now', 'now')"
        )
        conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('archived-1', 'Project A', 1.0, 'test')")
        conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('missing-1', 'Missing', 1.0, 'test')")
        conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('active-1', 'Active', 1.0, 'test')")
        conn.commit()
    finally:
        conn.close()

    dry = repair_graph_hygiene(hermes_home, apply=False)

    assert dry["dry_run"] is True
    assert dry["ok"] is False
    assert dry["deleted"]["memory_entities"] == 2
    verifier = sqlite3.connect(db_path)
    verifier.row_factory = sqlite3.Row
    try:
        counts = graph_hygiene_counts(verifier)
        assert counts["hidden_lifecycle_entities"] == 1
        assert counts["orphan_entities"] == 1
        assert verifier.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 3
    finally:
        verifier.close()

    applied = repair_graph_hygiene(hermes_home, apply=True)

    assert applied["ok"] is True
    assert applied["deleted"]["memory_entities"] == 2
    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 1
    finally:
        verifier.close()


def test_graph_hygiene_removes_candidate_and_in_progress_graph_edges(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        for memory_id, lifecycle in (("candidate-1", "candidate"), ("in-progress-1", "in_progress"), ("active-1", "")):
            metadata = {"lifecycle": lifecycle} if lifecycle else {}
            conn.execute(
                "INSERT INTO memories(id, scope_id, content, summary, source, target, metadata, created_at, updated_at) VALUES (?, 'scope-a', ?, ?, 'test', 'memory', ?, 'now', 'now')",
                (memory_id, memory_id, memory_id, json.dumps(metadata, sort_keys=True)),
            )
        conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('candidate-1', 'Candidate Entity', 1.0, 'test')")
        conn.execute("INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('in-progress-1', 'In Progress Entity', 1.0, 'test')")
        conn.execute(
            "INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at) VALUES ('candidate-1', 'active-1', 'same_topic', 0.8, 'test-candidate', 'now')"
        )
        conn.execute(
            "INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at) VALUES ('in-progress-1', 'active-1', 'same_topic', 0.8, 'test-in-progress', 'now')"
        )
        conn.commit()
    finally:
        conn.close()

    applied = repair_graph_hygiene(hermes_home, apply=True)

    assert applied["ok"] is True
    assert applied["deleted"] == {"memory_entities": 2, "memory_relations": 2}
    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 0
        assert verifier.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0
    finally:
        verifier.close()


def test_relation_rebuild_repair_seeds_drains_and_backs_up_sqlite(tmp_path):
    hermes_home = tmp_path / "hermes"
    db_dir = hermes_home / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = _conn(db_path)
    try:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO memories(
                    id, scope_id, content, summary, source, target,
                    metadata, created_at, updated_at
                ) VALUES(?, 'scope-a', ?, ?, 'test', 'project', '{}', ?, ?)
                """,
                (
                    f"relation-{index}",
                    f"Project Atlas relation fixture {index}",
                    f"fixture {index}",
                    f"2026-07-19T00:0{index}:00+00:00",
                    f"2026-07-19T00:0{index}:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = repair_relation_rebuild_debt(
        hermes_home,
        seed=True,
        drain=True,
        scope_ids=["scope-a"],
        max_events=6,
        pair_limit=1,
    )

    assert result["ok"] is True
    assert result["seed"] == {"eligible": 3, "queued": 3}
    assert result["drain"]["failed"] == 0
    assert result["drain"]["events_completed"] == 3
    assert result["after"]["unresolved"] == 0
    backup_path = result["backup_path"]
    assert backup_path
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        backup.close()

    reseeded = repair_relation_rebuild_debt(
        hermes_home,
        seed=True,
        drain=False,
        scope_ids=["scope-a"],
    )
    assert reseeded["after"]["pending"] == 3
    assert reseeded["after"]["unresolved"] == 3
