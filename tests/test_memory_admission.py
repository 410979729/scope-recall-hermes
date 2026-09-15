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
        default_lifecycle="promoted",
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


def test_adverb_continuation_is_not_a_current_state_marker() -> None:
    """Bare 持续 ("keeps/continuously") must not count as "currently is".

    Two real production rows were mislabelled as time-sensitive snapshots purely
    because an engineering description contained 持续 next to a domain noun that
    _VOLATILE_VALUE_RE treats as a volatile value (状态 / 候选).
    """

    assert is_time_sensitive_snapshot(
        '示教时畅通状态位置①按3秒、位置②按1秒固化护栏工作点，触摸屏设"护栏回波丢失+持续N秒（如3秒）"抗抖再触发停轨。'
    ) is False
    assert is_time_sensitive_snapshot(
        "journal-digest 能持续把 journal 转成记忆候选，但候选默认落在 needs_review 待审区。"
    ) is False
    # persisted-state phrasing must keep being detected
    assert is_time_sensitive_snapshot("该模型持续处于 NO-GO 状态，需先复核。") is True


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


def test_explicit_promoted_default_marks_stable_automatic_digest_as_promoted() -> None:
    metadata = automatic_admission_metadata(
        content="Scope Recall release verification uses a stable two-stage review workflow.",
        memory_type="workflow",
        source="nightly-digest",
        default_lifecycle="promoted",
    )

    assert metadata["lifecycle"] == "promoted"
    assert metadata["candidate_status"] == "promoted"
    assert metadata["automatic_admission"]["route"] == "experience_review"


def test_unknown_automatic_digest_lifecycle_falls_back_to_candidate() -> None:
    metadata = automatic_admission_metadata(
        content="Stable preference extracted from an automatic digest.",
        memory_type="preference",
        source="journal-digest",
        default_lifecycle="auto-activate",
    )

    assert metadata["lifecycle"] == "candidate"
    assert metadata["candidate_status"] == "needs_review"


def test_structured_evolution_keeps_dedicated_path_with_promoted_default() -> None:
    metadata = automatic_admission_metadata(
        content="The project codename changed from Atlas to Borealis.",
        memory_type="fact",
        source="nightly-digest",
        default_lifecycle="promoted",
        structured_evolution=True,
    )

    assert metadata["automatic_admission"]["route"] == "fact_evolution"
    assert "lifecycle" not in metadata
    assert "candidate_status" not in metadata


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
