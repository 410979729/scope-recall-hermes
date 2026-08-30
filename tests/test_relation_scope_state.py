"""Scope-revision and frequency-receipt invariants for relation rebuilds."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.relation_extraction import extract_relation_candidates
from scope_recall.relation_frequency_index import sync_relation_frequency_memory
from scope_recall.relation_scope_state import (
    ScopeCorpusChanged,
    current_scope_corpus_revision,
    load_scope_frequency_receipt,
    store_scope_frequency_receipt,
)
from scope_recall.sql_store import ensure_schema


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_memory(conn: sqlite3.Connection, memory_id: str, scope_id: str) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES(?, ?, 'fixture', 'project', 'stable fact', 'stable fact',
                 '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z', '{}')
        """,
        (memory_id, scope_id),
    )
    conn.commit()


def test_scope_revision_tracks_only_relation_relevant_truth_mutations() -> None:
    conn = _connection()
    _insert_memory(conn, "memory-1", "scope-a")
    inserted_revision = current_scope_corpus_revision(conn, "scope-a")
    assert inserted_revision > 0

    conn.execute(
        "UPDATE memories SET last_recalled_turn=last_recalled_turn+1 WHERE id='memory-1'"
    )
    conn.commit()
    assert current_scope_corpus_revision(conn, "scope-a") == inserted_revision

    conn.execute(
        "UPDATE memories SET updated_at='2026-07-20T00:01:00Z' WHERE id='memory-1'"
    )
    conn.commit()
    timestamp_revision = current_scope_corpus_revision(conn, "scope-a")
    assert timestamp_revision == inserted_revision + 1

    conn.execute(
        "UPDATE memories SET content='changed fact', "
        "updated_at='2026-07-20T00:02:00Z' WHERE id='memory-1'"
    )
    conn.commit()
    changed_revision = current_scope_corpus_revision(conn, "scope-a")
    assert changed_revision == timestamp_revision + 1

    conn.execute("UPDATE memories SET scope_id='scope-b' WHERE id='memory-1'")
    conn.commit()
    assert current_scope_corpus_revision(conn, "scope-a") == changed_revision + 1
    moved_revision = current_scope_corpus_revision(conn, "scope-b")
    assert moved_revision > 0

    conn.execute("DELETE FROM memories WHERE id='memory-1'")
    conn.commit()
    assert current_scope_corpus_revision(conn, "scope-b") == moved_revision + 1
    conn.close()


def test_updated_at_relation_semantics_advance_scope_revision() -> None:
    """A timestamp-only change can reverse supersession eligibility."""

    conn = _connection()
    _insert_memory(conn, "legacy", "scope-a")
    _insert_memory(conn, "replacement", "scope-a")
    conn.execute(
        "UPDATE memories SET content=?, metadata=?, updated_at=? WHERE id=?",
        (
            "The old Novel Hub cache setting is deprecated.",
            '{"entities":["Novel Hub"]}',
            "2026-01-01T00:00:00+00:00",
            "legacy",
        ),
    )
    sync_relation_frequency_memory(conn, "legacy")
    conn.execute(
        "UPDATE memories SET content=?, metadata=?, updated_at=? WHERE id=?",
        (
            "This Novel Hub setting replaces the previous cache setting.",
            '{"entities":["Novel Hub"]}',
            "2026-01-02T00:00:00+00:00",
            "replacement",
        ),
    )
    sync_relation_frequency_memory(conn, "replacement")
    conn.commit()
    revision_before = current_scope_corpus_revision(conn, "scope-a")
    supersedes_before = [
        item
        for item in extract_relation_candidates(conn, scope_ids=["scope-a"])
        if item["relation_type"] == "supersedes"
    ]
    assert len(supersedes_before) == 1

    conn.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        ("2025-01-01T00:00:00+00:00", "replacement"),
    )
    sync_relation_frequency_memory(conn, "replacement")
    conn.commit()

    assert current_scope_corpus_revision(conn, "scope-a") == revision_before + 1
    assert not [
        item
        for item in extract_relation_candidates(conn, scope_ids=["scope-a"])
        if item["relation_type"] == "supersedes"
    ]
    conn.close()


def test_frequency_receipt_compare_and_swap_rejects_stale_corpus() -> None:
    conn = _connection()
    _insert_memory(conn, "memory-1", "scope-a")
    revision = current_scope_corpus_revision(conn, "scope-a")

    first = store_scope_frequency_receipt(
        conn,
        scope_id="scope-a",
        expected_corpus_revision=revision,
        visible_memory_count=1,
        blocked_entities={"novel hub"},
    )
    conn.commit()
    cached = load_scope_frequency_receipt(conn, "scope-a")
    assert cached is not None
    assert cached["blocked_entities"] == {"novel hub"}
    assert first["blocked_entities_sha256"] == cached["blocked_entities_sha256"]

    conn.execute(
        "UPDATE memories SET metadata=? WHERE id='memory-1'",
        ('{"entities":["new"]}',),
    )
    conn.commit()
    assert load_scope_frequency_receipt(conn, "scope-a") is None
    with pytest.raises(ScopeCorpusChanged):
        store_scope_frequency_receipt(
            conn,
            scope_id="scope-a",
            expected_corpus_revision=revision,
            visible_memory_count=1,
            blocked_entities={"novel hub"},
        )
    conn.close()
