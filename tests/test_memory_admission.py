"""Admission policy tests for automatically generated durable-memory candidates.

Automatic extraction is evidence collection; it must not silently become trusted
profile state before freshness and routing checks run.
"""
from __future__ import annotations

import json

from scope_recall.journal_candidates import JournalDigestCandidate, candidate_metadata
from scope_recall.memory_admission import (
    automatic_admission_metadata,
    is_time_sensitive_snapshot,
)
from scope_recall.memory_quality import quality_decision_for_memory


def _row(content: str, metadata: dict, *, source: str = "journal-digest") -> dict:
    return {
        "id": "candidate",
        "scope_id": "shared",
        "source": source,
        "target": "ops",
        "content": content,
        "summary": content,
        "updated_at": "2026-07-21T00:00:00+00:00",
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


def test_time_sensitive_snapshot_requires_live_check_and_candidate_lifecycle() -> None:
    content = "Scope Recall 1.8.0 当前仍为 NO-GO，在线模型已切换为 Ultra。"

    assert is_time_sensitive_snapshot(content) is True
    metadata = automatic_admission_metadata(
        content=content,
        memory_type="decision",
        source="journal-digest",
        recommended_action="promote",
    )

    assert metadata["lifecycle"] == "candidate"
    assert metadata["needs_live_check"] is True
    assert metadata["freshness_status"] == "needs_live_check"
    assert metadata["truth_type"] == "operational_snapshot"
    assert metadata["validator_kind"] == "manual"
    assert metadata["automatic_admission"]["time_sensitive"] is True


def test_normative_rules_that_mention_current_state_are_not_snapshots() -> None:
    assert is_time_sensitive_snapshot(
        "发布汇报必须区分 current live 与 candidate source，并核对当前版本。"
    ) is False
    assert is_time_sensitive_snapshot(
        "模型变更后必须检查当前 provider/version，再运行smoke。"
    ) is False


def test_normative_references_after_current_fields_are_not_state_assertions() -> None:
    assert is_time_sensitive_snapshot(
        "The current provider status should be documented before release."
    ) is False
    assert is_time_sensitive_snapshot("当前模型状态应该在发布前核对。") is False


def test_concrete_current_values_remain_time_sensitive_even_with_followup_rules() -> None:
    assert is_time_sensitive_snapshot(
        "The current provider is codex and should be reviewed before release."
    ) is True
    assert is_time_sensitive_snapshot("当前模型是Ultra，发布前应该核对。") is True


def test_reusable_workflow_is_routed_to_experience_review_not_directly_promoted() -> None:
    content = "遇到journal积压时，先做doctor，再分批digest并验证backlog下降。"
    metadata = automatic_admission_metadata(
        content=content,
        memory_type="workflow",
        source="nightly-digest",
        recommended_action="promote",
    )

    assert metadata["lifecycle"] == "candidate"
    assert metadata["automatic_admission"]["route"] == "experience_review"
    decision = quality_decision_for_memory(
        _row(
            content,
            {
                **metadata,
                "memory_type": "workflow",
                "confidence": 0.95,
                "importance": 0.9,
                "evidence_refs": ["journal:1"],
            },
            source="nightly-digest",
        )
    )
    assert decision.action == "keep_candidate"
    assert decision.reason == "automatic_digest_requires_experience_review"


def test_journal_candidate_metadata_applies_admission_policy() -> None:
    candidate = JournalDigestCandidate(
        content="当前在线端口为18700，候选版本1.8.0仍是NO-GO。",
        target="ops",
        memory_type="decision",
        importance=0.9,
        confidence=0.92,
        entry_ids=[1],
        session_ids=["session-a"],
    )

    metadata = candidate_metadata(candidate, "run-a")

    assert metadata["lifecycle"] == "candidate"
    assert metadata["needs_live_check"] is True
    assert metadata["automatic_admission"]["source"] == "journal-digest"


def test_manual_tool_store_is_outside_automatic_admission_policy() -> None:
    metadata = automatic_admission_metadata(
        content="Joy prefers concise Chinese replies.",
        memory_type="preference",
        source="tool-store",
        recommended_action="promote",
    )

    assert metadata == {}
