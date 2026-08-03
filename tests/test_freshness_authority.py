"""Authority contracts between declared and runtime fact freshness."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.freshness import (
    attach_freshness_metadata,
    memory_freshness_map,
)
from scope_recall.sql_store import ensure_schema, store_row


def _current_companion() -> dict[str, object]:
    return {
        "status": "current",
        "fact_key": "service_endpoint",
        "truth_type": "config",
        "validator_kind": "manual",
        "last_checked_at": "2026-08-02T12:00:00+00:00",
        "valid_until": "2099-01-01T00:00:00+00:00",
        "stale_reason": "",
        "superseded_by": "",
        "needs_live_check": False,
    }


def test_companion_freshness_is_authoritative_in_output_projection():
    declared = {
        "status": "stale",
        "stale_reason": "declaration captured before a later live check",
        "validator_kind": "manual",
    }
    metadata = {"memory_type": "factual", "freshness": dict(declared)}

    penalty = attach_freshness_metadata(metadata, _current_companion())

    assert penalty == 0.0
    assert metadata["freshness_authority"] == "fact_freshness"
    assert metadata["freshness_declared"] == declared
    assert metadata["freshness"] == {
        "status": "current",
        "fact_key": "service_endpoint",
        "truth_type": "config",
        "validator_kind": "manual",
        "last_checked_at": "2026-08-02T12:00:00+00:00",
        "valid_until": "2099-01-01T00:00:00+00:00",
        "stale_reason": "",
        "superseded_by": "",
        "needs_live_check": False,
    }
    assert metadata["fact_freshness_status"] == "current"
    assert metadata["needs_live_check"] is False
    assert "freshness_warning" not in metadata


def test_missing_companion_fails_closed_even_when_metadata_declares_current():
    declared = {"status": "current", "last_checked_at": "2099-01-01T00:00:00+00:00"}
    metadata = {
        "freshness": dict(declared),
        "fact_freshness_status": "current",
        "needs_live_check": False,
        "fact_freshness_penalty": 0.0,
    }

    penalty = attach_freshness_metadata(metadata, None)

    assert penalty > 0.0
    assert metadata["freshness_authority"] == "fact_freshness"
    assert metadata["freshness_declared"] == declared
    assert metadata["freshness"] == {
        "status": "untracked",
        "needs_live_check": True,
    }
    assert metadata["fact_freshness_status"] == "untracked"
    assert metadata["needs_live_check"] is True
    assert "UNTRACKED" in metadata["freshness_warning"]


def test_authoritative_projection_never_rewrites_declared_sqlite_metadata():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        declared = {
            "memory_type": "factual",
            "freshness": {
                "status": "stale",
                "stale_reason": "state before restore",
                "validator_kind": "manual",
            },
        }
        store_row(
            conn,
            memory_id="restored-freshness",
            scope_id="shared-scope",
            platform="telegram",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="freshness-authority",
            source="tool-store",
            target="ops",
            content="Service endpoint freshness authority test.",
            metadata=json.dumps(declared, ensure_ascii=False, sort_keys=True),
        )
        conn.execute(
            """
            UPDATE fact_freshness
            SET status='current', stale_reason='',
                last_checked_at='2026-08-02T12:00:00+00:00',
                valid_until='2099-01-01T00:00:00+00:00'
            WHERE subject_id='restored-freshness'
            """
        )
        conn.commit()
        stored_before = conn.execute(
            "SELECT metadata FROM memories WHERE id='restored-freshness'"
        ).fetchone()[0]

        projected = json.loads(stored_before)
        companion = memory_freshness_map(conn, ["restored-freshness"])[
            "restored-freshness"
        ]
        attach_freshness_metadata(projected, companion)

        stored_after = conn.execute(
            "SELECT metadata FROM memories WHERE id='restored-freshness'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert stored_after == stored_before
    assert projected["freshness_declared"] == declared["freshness"]
    assert projected["freshness"]["status"] == "current"
    assert projected["fact_freshness_status"] == "current"
