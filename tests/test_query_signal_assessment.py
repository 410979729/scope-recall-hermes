"""Pure-contract tests for zero-signal recall candidate admission."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scope_recall._internal.recall.query_signal import (
    CandidateAdmission,
    QuerySignalAssessment,
    assess_candidate_admission,
    assess_query_signal,
    is_opaque_query,
)


@pytest.mark.parametrize(
    "query",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "c799ccd3",
        "U2NvcGVSZWNhbGxSYW5kb21QYXlsb2Fk",
        "qzxvbnmkjhgf",
        "qzxvbnmkjhgfdspoiuytrewq",
        "峰猫泪珠墙锁qzxvbn",
        "㐀㐁㐂㐃㐄㐅㐆㐇",
        "!!! 🚀 ???",
        "the",
    ],
)
def test_opaque_queries_cannot_use_vector_only_evidence(query: str) -> None:
    assert is_opaque_query(query) is True

    admission = assess_candidate_admission(
        query,
        candidate_id="unrelated",
        content="Completely unrelated durable memory.",
        vector_score=0.99,
        vector_background_score=0.50,
    )

    assert admission.admitted is False
    assert admission.vector_evidence is False
    assert "no_admissible_evidence" in admission.reason_codes


@pytest.mark.parametrize(
    "query,candidate_id,content",
    [
        (
            "550e8400-e29b-41d4-a716-446655440000",
            "memory:550e8400-e29b-41d4-a716-446655440000",
            "UUID audit anchor.",
        ),
        ("c799ccd3", "memory-1", "Release commit c799ccd3 passed review."),
        ("PR-57", "memory-2", "PR-57 tracks the retrieval regression."),
    ],
)
def test_opaque_identifier_exact_lexical_match_is_admitted(
    query: str,
    candidate_id: str,
    content: str,
) -> None:
    admission = assess_candidate_admission(
        query,
        candidate_id=candidate_id,
        content=content,
        vector_score=0.01,
        vector_background_score=0.0,
    )
    assessment = assess_query_signal(query, [admission])

    assert admission.admitted is True
    assert admission.exact_identifier_evidence is True
    assert admission.vector_evidence is False
    assert assessment.state == "identifier_exact_only"
    assert assessment.exact_lexical_match is True


@pytest.mark.parametrize(
    "query,content",
    [
        ("c799ccd3", "The longer value 00c799ccd3ff is not the requested commit."),
        ("c799ccd3", "The value prefix-c799ccd3-suffix is a different identifier."),
        ("PR-57", "PR-57-extra is a child identifier, not the requested PR."),
        ("XAS-OPS-404", "XAS-OPS-404-next is a different work item."),
    ],
)
def test_partial_identifier_is_not_exact_evidence(query: str, content: str) -> None:
    admission = assess_candidate_admission(
        query,
        candidate_id="memory-1",
        content=content,
        vector_score=0.0,
    )

    assert admission.admitted is False
    assert admission.exact_identifier_evidence is False


def test_stopword_exact_phrase_is_not_identifier_evidence() -> None:
    admission = assess_candidate_admission(
        "the",
        candidate_id="ordinary-memory",
        content="The workstation timezone is America/New_York.",
        vector_score=0.99,
    )

    assert admission.admitted is False
    assert admission.exact_identifier_evidence is False


@pytest.mark.parametrize(
    "query",
    [
        "请回忆长期记忆的架构方案",
        "显卡驱动安装教程",
        "机箱风扇转速控制",
        "量子纠缠实验方法",
        "生物化学实验步骤",
        "机器视觉目标检测",
        "深度学习训练技巧",
        "城市交通规划研究",
    ],
)
def test_natural_chinese_query_is_not_treated_as_opaque(query: str) -> None:
    assert is_opaque_query(query) is False

    admission = assess_candidate_admission(
        query,
        candidate_id="current-windows-host",
        content="The persistent knowledge subsystem uses a relational truth ledger.",
        vector_score=0.82,
        vector_background_score=0.70,
    )
    assessment = assess_query_signal(query, [admission])

    assert admission.admitted is True
    assert admission.vector_evidence is True
    assert assessment.state == "positive"
    assert assessment.vector_only_admissible is True


@pytest.mark.parametrize("query", ["system", "cryptic", "sphinx"])
def test_short_natural_english_word_is_not_treated_as_opaque(query: str) -> None:
    assert is_opaque_query(query) is False

    admission = assess_candidate_admission(
        query,
        candidate_id="semantic-neighbor",
        content="A strong semantic paraphrase with no literal overlap.",
        vector_score=0.82,
        vector_background_score=0.70,
    )

    assert admission.admitted is True
    assert admission.vector_evidence is True


@pytest.mark.parametrize(
    "query",
    ["系统system", "加密cryptic", "狮身人面像sphinx", "节奏rhythms"],
)
def test_natural_mixed_language_query_is_not_treated_as_opaque(query: str) -> None:
    assert is_opaque_query(query) is False

    admission = assess_candidate_admission(
        query,
        candidate_id="mixed-language-neighbor",
        content="A strong semantic paraphrase with no literal overlap.",
        vector_score=0.82,
        vector_background_score=0.70,
    )

    assert admission.admitted is True
    assert admission.vector_evidence is True


def test_natural_multiword_query_with_identifier_is_not_wholly_opaque() -> None:
    query = "Orion rollback checkpoint Delta-7"

    assert is_opaque_query(query) is False
    admission = assess_candidate_admission(
        query,
        candidate_id="orion-rollback",
        content="Orion rollback uses checkpoint Delta-7.",
    )

    assert admission.admitted is True
    assert admission.lexical_evidence is True


def test_rare_random_cjk_still_allows_strict_exact_lexical_match() -> None:
    query = "㐀㐁㐂㐃㐄㐅㐆㐇"
    admission = assess_candidate_admission(
        query,
        candidate_id="rare-cjk-fixture",
        content=f"Exact audit token: {query}.",
        vector_score=0.0,
    )

    assert admission.admitted is True
    assert admission.exact_identifier_evidence is True
    assert admission.vector_evidence is False
    assert assess_query_signal(query, [admission]).state == "identifier_exact_only"


@pytest.mark.parametrize(
    "score,background,expected_reason",
    [
        (0.0, 0.0, "vector_score_not_positive"),
        (0.69, 0.20, "vector_only_below_min_score"),
        (0.80, 0.78, "vector_only_below_min_margin"),
    ],
)
def test_vector_only_requires_positive_absolute_score_and_margin(
    score: float,
    background: float,
    expected_reason: str,
) -> None:
    admission = assess_candidate_admission(
        "memory architecture database storage",
        candidate_id="vector-candidate",
        content="No shared surface terms are present here.",
        vector_score=score,
        vector_background_score=background,
    )

    assert admission.admitted is False
    assert admission.vector_evidence is False
    assert expected_reason in admission.reason_codes


def test_vector_only_accepts_strong_separated_semantic_hit() -> None:
    admission = assess_candidate_admission(
        "memory architecture database storage",
        candidate_id="scope-recall-architecture",
        content="Semantic paraphrase without the literal query vocabulary.",
        vector_score=0.82,
        vector_background_score=0.74,
    )

    assert admission.admitted is True
    assert admission.vector_evidence is True
    assert admission.lexical_evidence is False


def test_single_vector_neighbor_cannot_invent_a_background_baseline() -> None:
    admission = assess_candidate_admission(
        "durable preference retrieval",
        candidate_id="only-vector-row",
        content="A semantic paraphrase with different terminology.",
        vector_score=0.76,
        vector_background_score=None,
    )

    assert admission.admitted is False
    assert admission.vector_evidence is False
    assert "vector_background_unavailable" in admission.reason_codes


def test_lexical_authority_is_recomputed_and_priors_cannot_manufacture_it() -> None:
    positive = assess_candidate_admission(
        "deploy command",
        candidate_id="deploy-memory",
        content="Production deploy command is uv run app.",
        source="tool-store",
        target="project",
    )
    unrelated = assess_candidate_admission(
        "deploy command",
        candidate_id="trusted-but-unrelated",
        content="The preferred timezone is America/New_York.",
        source="builtin-curated",
        target="user",
    )

    assert positive.admitted is True
    assert positive.lexical_evidence is True
    assert unrelated.admitted is False
    assert unrelated.lexical_evidence is False
    assert unrelated.curated_evidence is False


def test_curated_requires_real_alias_aware_lexical_evidence() -> None:
    admission = assess_candidate_admission(
        "What response style does Joy prefer?",
        candidate_id="curated:reply-style",
        content="Joy prefers concise replies.",
        source="builtin-curated",
        target="user",
    )

    assert admission.admitted is True
    assert admission.lexical_evidence is True
    assert admission.curated_evidence is True


def test_temporal_authority_requires_query_side_semantic_match() -> None:
    matched = assess_candidate_admission(
        "Where does Joy live now?",
        candidate_id="current-city",
        content="Current fact projection.",
        temporal_authoritative=True,
        temporal_text="Joy lives in Tokyo.",
    )
    unrelated = assess_candidate_admission(
        "Where does Joy live now?",
        candidate_id="current-timezone",
        content="Current fact projection.",
        temporal_authoritative=True,
        temporal_text="The workstation timezone is America/New_York.",
    )

    assert matched.admitted is True
    assert matched.temporal_evidence is True
    assert unrelated.admitted is False
    assert unrelated.temporal_evidence is False


def test_query_signal_exposes_all_four_states() -> None:
    lexical = assess_candidate_admission(
        "deploy command",
        candidate_id="deploy",
        content="The deploy command is uv run app.",
    )
    weak = assess_candidate_admission(
        "memory architecture",
        candidate_id="near-neighbor",
        content="Unrelated content.",
        vector_score=0.65,
        vector_background_score=0.60,
    )
    exact = assess_candidate_admission(
        "c799ccd3",
        candidate_id="commit-memory",
        content="Commit c799ccd3 is the release candidate.",
    )

    assert assess_query_signal("deploy command", [lexical]).state == "positive"
    assert assess_query_signal("memory architecture", [weak]).state == "weak"
    assert assess_query_signal("!!!", []).state == "none"
    assert assess_query_signal("c799ccd3", [exact]).state == "identifier_exact_only"


def test_contracts_are_immutable_and_reason_codes_are_tuples() -> None:
    admission = assess_candidate_admission(
        "deploy command",
        candidate_id="deploy",
        content="The deploy command is uv run app.",
    )
    assessment = assess_query_signal("deploy command", [admission])

    assert isinstance(admission, CandidateAdmission)
    assert isinstance(assessment, QuerySignalAssessment)
    assert isinstance(admission.reason_codes, tuple)
    assert isinstance(assessment.semantic_tokens, tuple)
    with pytest.raises(FrozenInstanceError):
        admission.admitted = False  # type: ignore[misc]
