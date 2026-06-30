"""Tests for graph companion hygiene repair and orphan detection.

Graph rows are rebuildable evidence, so repairs must compare against SQLite truth."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.graph import ensure_graph_schema
from scope_recall.graph_hygiene import graph_hygiene_counts, repair_graph_hygiene
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


def test_graph_hygiene_preserves_candidate_and_in_progress_graph_edges(tmp_path):
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
    assert applied["deleted"] == {"memory_entities": 0, "memory_relations": 0}
    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 2
        assert verifier.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 2
    finally:
        verifier.close()
