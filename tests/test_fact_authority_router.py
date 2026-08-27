"""Program 3A regressions for strict Fact authority routing."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_authority import FactAuthorityLane, route_fact_authority
from scope_recall.fact_evolution import execute_pipeline_proposal
from scope_recall.governance import normalize_memory_type
from scope_recall.sql_store import ensure_schema


@pytest.mark.parametrize(
    "memory_type",
    ["factual", "preference", "project", "resource", "constraint"],
)
def test_only_frozen_allowlist_is_claim_backed(memory_type: str):
    route = route_fact_authority(memory_type)
    assert route.canonical_type == memory_type
    assert route.lane is FactAuthorityLane.CLAIM_BACKED
    assert route.claim_backed is True


@pytest.mark.parametrize(
    "memory_type",
    [
        "procedure",
        "workflow",
        "tool_trace",
        "summary",
        "mental_model",
        "pitfall",
        "decision",
        "episodic",
    ],
)
def test_frozen_semantic_types_remain_memory_only(memory_type: str):
    route = route_fact_authority(memory_type)
    assert route.canonical_type == memory_type
    assert route.lane is FactAuthorityLane.MEMORY_ONLY
    assert route.claim_backed is False


@pytest.mark.parametrize("alias", ["experience", "narrative", "scratch"])
def test_compatibility_aliases_resolve_only_to_episodic(alias: str):
    route = route_fact_authority(alias)
    assert route.canonical_type == "episodic"
    assert route.lane is FactAuthorityLane.MEMORY_ONLY
    assert route.reason_code == "memory_only_compatibility_alias"


@pytest.mark.parametrize(
    "memory_type",
    [None, "", "mystery", "profile", "pref", "doc", "rule", "policy", "fact"],
)
def test_missing_unknown_legacy_aliases_and_projection_marker_never_authorize_claim(
    memory_type: object,
):
    route = route_fact_authority(memory_type)
    assert route.lane is FactAuthorityLane.REVIEW
    assert route.claim_backed is False


def test_legacy_classifier_fallback_is_retained_but_cannot_decide_authority():
    assert normalize_memory_type("mystery") == "factual"
    assert route_fact_authority("mystery").lane is FactAuthorityLane.REVIEW


def _proposal(*, action: EvolutionAction = EvolutionAction.ADD, target_ids=()):
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=ClaimDraft.from_parts(
            subject="Joy",
            predicate="lives in",
            value="Bangalore",
            scope_id="scope-a",
        ),
        target_ids=tuple(target_ids),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-authority-router",
                "Joy lives in Bangalore.",
                "Joy",
            ),
        ),
        confidence=0.99,
        reason="strict authority router regression",
    )


def _execute(
    conn: sqlite3.Connection,
    *,
    memory_type: object,
    proposal: EvolutionProposal | None = None,
):
    return execute_pipeline_proposal(
        conn,
        proposal=proposal or _proposal(),
        lane="nightly",
        run_id="authority-router",
        source_key=f"authority-router:{memory_type!r}",
        trusted_scope_id="scope-a",
        writable_scope_ids=("scope-a",),
        actor="scope-recall:test",
        source="nightly",
        target="user",
        content="Joy lives in Bangalore.",
        metadata={"memory_type": memory_type},
        runtime_config={
            "fact_evolution": {"enabled": True, "nightly_mode": "auto_apply"}
        },
        dry_run=False,
    )


@pytest.mark.parametrize("memory_type", [None, "", "mystery", "workflow", "experience"])
def test_application_use_case_fails_closed_with_zero_writes(memory_type: object):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    result = _execute(conn, memory_type=memory_type)

    assert result.action is EvolutionAction.REVIEW
    assert result.status == "review"
    assert result.applied is False
    assert "memory_type_not_claim_authoritative" in result.receipt["reason_codes"]
    for table in ("memories", "fact_claims", "fact_action_receipts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "memory_type",
    ["factual", "preference", "project", "resource", "constraint"],
)
def test_application_use_case_preserves_canonical_projection_type(memory_type: str):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    result = _execute(conn, memory_type=memory_type)

    assert result.applied is True
    assert len(result.receipt["projection_pairs"]) == 1
    pair = result.receipt["projection_pairs"][0]
    assert pair["memory_type"] == memory_type
    metadata = json.loads(
        str(
            conn.execute(
                "SELECT metadata FROM memories WHERE id = ?",
                (pair["memory_id"],),
            ).fetchone()[0]
        )
    )
    assert metadata["memory_type"] == memory_type
    assert metadata["fact_claim_id"] == pair["claim_id"]
    conn.close()


def test_internal_projection_marker_cannot_authorize_a_new_claim():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    result = _execute(conn, memory_type="fact")

    assert result.status == "review"
    assert "internal_projection_not_claim_authority" in result.receipt["reason_codes"]
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    conn.close()


def test_internal_projection_marker_does_not_authorize_unowned_target_mutation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    timestamp = "2026-08-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "unowned-projection",
            "scope-a",
            "test",
            "user",
            "Unowned row",
            "Unowned row",
            timestamp,
            timestamp,
            '{"memory_type":"fact","lifecycle":"active"}',
        ),
    )
    conn.commit()

    result = _execute(
        conn,
        memory_type="fact",
        proposal=_proposal(
            action=EvolutionAction.RETRACT,
            target_ids=("unowned-projection",),
        ),
    )

    assert result.status == "review"
    assert "memory_type_not_claim_authoritative" in result.receipt["reason_codes"]
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0
    conn.close()
