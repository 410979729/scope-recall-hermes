"""Adversarial tests for citation-bound Reflection text grounding."""

from __future__ import annotations

import pytest

from scope_recall.reflection import ReflectionEvidence
from scope_recall.reflection_grounding import text_grounding


def _evidence() -> dict[str, ReflectionEvidence]:
    item = ReflectionEvidence(
        evidence_id="memory:aurora",
        kind="memory",
        memory_id="aurora",
        claim_id=None,
        source="benchmark",
        target="project",
        scope_id="scope-a",
        content="Aurora uses PostgreSQL for durable project state.",
        summary="Project Aurora database",
        updated_at="2026-07-01T00:00:00+00:00",
        score=0.99,
        metadata={},
    )
    return {item.evidence_id: item}


def test_long_grounded_answer_cannot_hide_one_new_lowercase_fact() -> None:
    result = text_grounding(
        "Aurora uses PostgreSQL for durable project state. "
        "Aurora uses PostgreSQL. Aurora uses redis.",
        citations=("memory:aurora",),
        evidence_by_id=_evidence(),
    )

    assert result.supported is False
    assert result.unsupported_token_count == 1


def test_entity_prefix_is_not_treated_as_morphological_support() -> None:
    result = text_grounding(
        "Aurora uses PostgreSQLPlus for durable project state.",
        citations=("memory:aurora",),
        evidence_by_id=_evidence(),
    )

    assert result.supported is False
    assert result.unsupported_token_count == 1


def _semantic_evidence() -> dict[str, ReflectionEvidence]:
    item = ReflectionEvidence(
        evidence_id="memory:semantic",
        kind="memory",
        memory_id="semantic",
        claim_id=None,
        source="benchmark",
        target="project",
        scope_id="scope-a",
        content=(
            "Alice manages Bob. PostgreSQL is not used. "
            "Migration Alpha happens before Migration Beta. "
            "If canary verification passes, production rollout may begin."
        ),
        summary="Semantic adversarial fixture",
        updated_at="2026-07-01T00:00:00+00:00",
        score=0.99,
        metadata={},
    )
    return {item.evidence_id: item}


@pytest.mark.parametrize(
    "text",
    [
        "Bob manages Alice.",
        "PostgreSQL is used.",
        "Migration Beta happens before Migration Alpha.",
        "Production rollout may begin.",
    ],
)
def test_semantic_reversal_is_not_grounded_by_lexical_coverage(text: str) -> None:
    result = text_grounding(
        text,
        citations=("memory:semantic",),
        evidence_by_id=_semantic_evidence(),
    )

    assert result.coverage == 1.0
    assert result.unsupported_token_count == 0
    assert result.supported is False


@pytest.mark.parametrize(
    "text",
    [
        "Alice manages Bob.",
        "PostgreSQL is not used.",
        "Migration Alpha happens before Migration Beta.",
        "If canary verification passes, production rollout may begin.",
    ],
)
def test_semantic_gate_preserves_matching_propositions(text: str) -> None:
    result = text_grounding(
        text,
        citations=("memory:semantic",),
        evidence_by_id=_semantic_evidence(),
    )

    assert result.supported is True
    assert result.coverage == 1.0
