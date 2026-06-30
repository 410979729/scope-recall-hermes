"""Tests for memory governance classification heuristics.

They keep cleanup and promotion evidence stable as low-signal patterns evolve."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.sql_store import ensure_schema, store_row


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _store(conn, *, memory_id="m1", target="memory", source="tool-store", content="Joy prefers direct answers.", metadata="{}"):
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope" if target != "general" else "local-scope",
        platform="telegram",
        user_id="joy",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="agent-a",
        agent_workspace="hermes",
        session_id="session-a",
        source=source,
        target=target,
        content=content,
        metadata=metadata,
    )
    return json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()["metadata"])


def test_store_row_adds_structured_classification_metadata_for_durable_rows():
    conn = _conn()

    metadata = _store(
        conn,
        target="user",
        source="tool-store",
        content="Joy prefers direct answers and no surprise model switches.",
        metadata=json.dumps({"entities": ["joy"], "tags": ["manual"]}),
    )

    assert metadata["kind"] == "user_preference"
    assert metadata["lifecycle"] == "promoted"
    assert metadata["authority"] == "agent_tool"
    assert metadata["confidence"] >= 0.8
    assert metadata["sensitivity"] == "normal"
    assert metadata["expires_at"] is None
    assert "joy" in metadata["entities"]
    assert "manual" in metadata["tags"]
    assert "target:user" in metadata["tags"]
    assert "kind:user_preference" in metadata["tags"]
    # Backward-compatible fields remain present for older tools/reports.
    assert metadata["category"] == "preference"
    assert metadata["tier"] == "core"


def test_general_rows_are_classified_as_local_scratch_raw_observations():
    conn = _conn()

    metadata = _store(
        conn,
        target="general",
        source="turn-user",
        content="This is a one-off temporary scratch note from the current chat.",
    )

    assert metadata["kind"] == "raw_observation"
    assert metadata["lifecycle"] == "scratch"
    assert metadata["authority"] == "user_turn"
    assert metadata["scope_mode"] == "local"
    assert metadata["confidence"] <= 0.62
    assert "target:general" in metadata["tags"]


def test_invalid_metadata_is_preserved_without_losing_classification():
    conn = _conn()

    metadata = _store(
        conn,
        target="ops",
        source="turn-extracted",
        content="Restart gateway service after plugin rollout.",
        metadata="not-json",
    )

    assert metadata["kind"] == "ops_procedure"
    assert metadata["lifecycle"] == "promoted"
    assert metadata["authority"] == "rule_extracted"
    assert metadata["raw_metadata"] == "not-json"
