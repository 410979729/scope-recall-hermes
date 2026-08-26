"""Adversarial invariants for relation materialization.

These tests bind source-clause semantic gating and scope ownership to the same
observable contract: deterministic companion edges must represent a real
in-scope relation, never presentation wording or a stale pre-move peer.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.relation_extraction import (
    extract_relation_candidates,
    rebuild_extracted_relations,
    sync_extracted_relations_for_memory,
)
from scope_recall.relation_frequency_index import sync_relation_frequency_memory
from scope_recall.sql_store import ensure_schema, store_row


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    entities: list[str],
    scope_id: str = "shared-scope",
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope_id,
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="relation-invariant-fixture",
        source="tool-store",
        target="project",
        content=content,
        metadata=json.dumps(
            {
                "memory_type": "factual",
                "entities": entities,
                "importance": 0.8,
            },
            ensure_ascii=False,
        ),
        allow_duplicate=True,
    )
    conn.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        (updated_at, memory_id),
    )
    sync_relation_frequency_memory(conn, memory_id)
    conn.commit()


@pytest.mark.parametrize(
    "presentation_source",
    [
        "The marketing schedule depends on whether Redis appears in the branding screenshot.",
        "The approval schedule depends on whether Redis is shown on the dashboard chart.",
        "The product label review requires Redis to appear in the launch mockup.",
    ],
)
def test_typed_relation_rejects_source_presentation_context(
    presentation_source: str,
) -> None:
    """A runtime target cannot turn source-side presentation text into a dependency."""

    conn = _connection()
    _store(
        conn,
        memory_id="presentation-source",
        content=presentation_source,
        entities=["Redis"],
    )
    _store(
        conn,
        memory_id="redis-runtime",
        content="Redis is a healthy cache service that provides runtime storage.",
        entities=["Redis"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert not [
        item
        for item in candidates
        if item["source_memory_id"] == "presentation-source"
        and item["target_memory_id"] == "redis-runtime"
        and item["relation_type"] == "depends_on"
    ]
    conn.close()


def test_source_presentation_clause_does_not_hide_separate_runtime_clause() -> None:
    conn = _connection()
    _store(
        conn,
        memory_id="mixed-source",
        content=(
            "The Redis icon appears in the branding screenshot. "
            "Checkout deployment depends on Redis service availability."
        ),
        entities=["Checkout deployment", "Redis"],
    )
    _store(
        conn,
        memory_id="redis-runtime",
        content="Redis service availability and runtime recovery runbook.",
        entities=["Redis"],
    )

    candidates = extract_relation_candidates(conn, scope_ids=["shared-scope"])

    assert any(
        item["source_memory_id"] == "mixed-source"
        and item["target_memory_id"] == "redis-runtime"
        and item["relation_type"] == "depends_on"
        for item in candidates
    )
    conn.close()


def test_scope_move_atomically_removes_relations_touching_the_moved_memory() -> None:
    """A scope migration must not expose stale cross-scope companion edges."""

    conn = _connection()
    _store(
        conn,
        memory_id="replacement",
        content="This Novel Hub setting replaces the previous cache setting.",
        updated_at="2026-01-02T00:00:00+00:00",
        entities=["Novel Hub"],
        scope_id="scope-a",
    )
    _store(
        conn,
        memory_id="legacy",
        content="The old Novel Hub cache setting is deprecated.",
        updated_at="2026-01-01T00:00:00+00:00",
        entities=["Novel Hub"],
        scope_id="scope-a",
    )
    rebuild_extracted_relations(
        conn,
        scope_ids=["scope-a"],
        dry_run=False,
        batch_id="scope-move-fixture",
    )
    before = conn.execute(
        "SELECT COUNT(*) FROM memory_relations "
        "WHERE source_memory_id='replacement' OR target_memory_id='replacement'"
    ).fetchone()[0]
    assert before >= 1

    conn.execute(
        "UPDATE memories SET scope_id=?, updated_at=? WHERE id=?",
        ("scope-b", "2026-07-20T00:00:00+00:00", "replacement"),
    )
    conn.commit()

    after_move = conn.execute(
        "SELECT COUNT(*) FROM memory_relations "
        "WHERE source_memory_id='replacement' OR target_memory_id='replacement'"
    ).fetchone()[0]
    assert after_move == 0

    sync = sync_extracted_relations_for_memory(
        conn,
        memory_id="replacement",
        scope_ids=["scope-b"],
        batch_id="scope-move-sync",
        max_pairs=10,
    )
    assert sync["ok"] is True
    cross_scope = conn.execute(
        """
        SELECT COUNT(*)
        FROM memory_relations AS relation
        JOIN memories AS source ON source.id=relation.source_memory_id
        JOIN memories AS target ON target.id=relation.target_memory_id
        WHERE source.scope_id<>target.scope_id
        """
    ).fetchone()[0]
    assert cross_scope == 0
    conn.close()
