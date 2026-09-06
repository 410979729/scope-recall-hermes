"""Anonymized regressions for Chinese interrogative project-title recall.

Natural questions and exact titles must share the same promoted row. Shared
personal names plus an unmatched mixed letter-and-digit identifier must stay
empty. Ordinary English category nouns keep the baseline scorer. Each test
builds its own store so collection order cannot leak state.
"""

from __future__ import annotations

import json

from plugins.memory import load_memory_provider

from scope_recall._internal.recall.query_signal import (
    assess_candidate_admission,
    assess_query_signal,
)
from scope_recall.gating import (
    document_supports_mixed_letter_digit_identifiers,
    mixed_letter_digit_identifiers,
    semantic_query_tokens,
)
from scope_recall.scoring import lexical_score
from scope_recall.sql_store import store_row

ANON_OWNER = "开阳"
ANON_TOPIC = "短片"
ANON_TITLE = "北风把灯塔吹灭了"
ANON_CONTENT = (
    f"{ANON_TOPIC}项目《{ANON_TITLE}》自 2026-03-18 起由{ANON_OWNER}（Kaiyang）"
    "全权拥有：Alice 见证交接后接收。"
)
ANON_NATURAL_QUERY = f"{ANON_OWNER}写{ANON_TOPIC}的项目叫什么？"
ANON_TITLE_QUERY = ANON_TITLE
ANON_OWNER_IDENTIFIER_QUERY = f"{ANON_OWNER}的 WSL2 参数是什么？"
ANON_NAME_IDENTIFIER_QUERY = "Alice 的 CUDA12 参数是什么？"
ANON_MATCHED_IDENTIFIER_CONTENT = "Alice 在工作站上使用 WSL2。"
ANON_MATCHED_IDENTIFIER_QUERY = "Alice 的 WSL2 环境是什么？"
ANON_BOUNDARY_QUERY = "Alice 的 WSL2 怎么配置？"
ANON_EXACT_IDENTIFIER_CONTENT = "Alice documents WSL2."
ANON_LONGER_IDENTIFIER_CONTENT = "Alice documents WSL20."
ANON_PREFIXED_IDENTIFIER_CONTENT = "Alice documents AWSL2."
ANON_SUFFIXED_IDENTIFIER_CONTENT = "Alice documents WSL2X."
ANON_CASE_IDENTIFIER_CONTENT = "Alice documents wsl2."
ANON_PUNCT_IDENTIFIER_CONTENT = "Alice documents (WSL2)!"
ANON_MULTI_IDENTIFIER_QUERY = "Alice 的 WSL2 和 CUDA12 怎么配置？"
ANON_MULTI_IDENTIFIER_PARTIAL_CONTENT = "Alice documents CUDA12."
ANON_CATEGORY_QUERY = "Which database did Alice choose?"
ANON_CATEGORY_CONTENT = "Alice chose PostgreSQL."
ANON_WEAK_QUERY = "今天午饭吃什么比较好"
ANON_NONSENSE_QUERY = "!!! 🚀 ???"


def _plugin(tmp_path, *, lifecycle: str = "promoted"):
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": False,
                "retrieval": {
                    "mode": "lexical",
                    "min_score": 0.18,
                    "candidate_pool": 12,
                    "top_k": 5,
                    "zero_signal_gate_enabled": True,
                    "opaque_query_vector_only_enabled": False,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-cjk-interrogative",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="kaiyang",
        agent_workspace="hermes",
        user_id="operator",
        chat_id="dm",
    )
    if getattr(plugin, "_vector_enabled", None):
        plugin._vector_enabled = False
    retrieval = dict(getattr(plugin, "_retrieval_config", {}) or {})
    retrieval.update(
        {
            "mode": "lexical",
            "min_score": 0.18,
            "candidate_pool": 12,
            "top_k": 5,
            "zero_signal_gate_enabled": True,
        }
    )
    plugin._retrieval_config = retrieval
    store_row(
        plugin._require_conn(),
        memory_id="anon-shortfilm-project",
        scope_id=plugin._shared_scope_id,
        platform="cli",
        user_id="operator",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="kaiyang",
        agent_workspace="hermes",
        session_id="session-cjk-interrogative",
        source="tool-store",
        target="project",
        content=ANON_CONTENT,
        metadata=json.dumps(
            {
                "lifecycle": "candidate" if lifecycle != "promoted" else "promoted",
                "memory_type": "project",
                "kind": "project_fact",
                "category": "project",
                "needs_live_check": True,
                "entities": ["kaiyang", "short-film", ANON_OWNER, "alice"],
            },
            ensure_ascii=False,
        ),
    )
    if lifecycle != "promoted":
        conn = plugin._require_conn()
        raw = conn.execute(
            "SELECT metadata FROM memories WHERE id = ?",
            ("anon-shortfilm-project",),
        ).fetchone()
        payload = json.loads(raw[0] if raw is not None else "{}")
        payload["lifecycle"] = lifecycle
        conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), "anon-shortfilm-project"),
        )
        conn.commit()
    return plugin


def _search_ids(plugin, query: str):
    payload = json.loads(
        plugin.handle_tool_call(
            "scope_recall_search",
            {"query": query, "limit": 5, "include_trace": True},
        )
    )
    return [
        str(item.get("id") or "")
        for item in payload.get("results") or []
        if isinstance(item, dict)
    ], payload


def test_natural_interrogative_keeps_subject_and_topic_units() -> None:
    tokens = semantic_query_tokens(ANON_NATURAL_QUERY)

    assert ANON_OWNER in tokens
    assert ANON_TOPIC in tokens
    assert "项目" in tokens
    assert "什么" not in tokens
    assert mixed_letter_digit_identifiers(ANON_NATURAL_QUERY) == []
    assert mixed_letter_digit_identifiers(ANON_CATEGORY_QUERY) == []
    assert mixed_letter_digit_identifiers(ANON_OWNER_IDENTIFIER_QUERY) == ["wsl2"]
    assert mixed_letter_digit_identifiers(ANON_NAME_IDENTIFIER_QUERY) == ["cuda12"]


def test_natural_interrogative_admits_same_row_as_exact_title() -> None:
    natural = assess_candidate_admission(
        ANON_NATURAL_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.0,
    )
    title = assess_candidate_admission(
        ANON_TITLE_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.0,
    )
    natural_score = lexical_score(
        query=ANON_NATURAL_QUERY,
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
    )

    assert natural.admitted is True
    assert natural.lexical_evidence is True
    assert title.admitted is True
    assert natural_score >= 0.18
    assert assess_query_signal(ANON_NATURAL_QUERY, [natural]).state == "positive"
    assert assess_query_signal(ANON_TITLE_QUERY, [title]).state == "positive"


def test_alice_category_noun_keeps_baseline_lexical_score() -> None:
    score = lexical_score(
        query=ANON_CATEGORY_QUERY,
        content=ANON_CATEGORY_CONTENT,
        summary="",
        source="tool-store",
        target="project",
    )
    admission = assess_candidate_admission(
        ANON_CATEGORY_QUERY,
        candidate_id="anon-alice-postgres",
        content=ANON_CATEGORY_CONTENT,
        summary="",
        source="tool-store",
        target="project",
        vector_score=0.0,
    )

    assert score == 0.375
    assert admission.admitted is True
    assert admission.lexical_evidence is True


def test_unmatched_mixed_identifier_is_not_lexical_or_vector_evidence() -> None:
    owner = assess_candidate_admission(
        ANON_OWNER_IDENTIFIER_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.99,
        vector_background_score=0.50,
    )
    alice = assess_candidate_admission(
        ANON_NAME_IDENTIFIER_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.99,
        vector_background_score=0.50,
    )
    weak = assess_candidate_admission(
        ANON_WEAK_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.0,
    )
    nonsense = assess_candidate_admission(
        ANON_NONSENSE_QUERY,
        candidate_id="anon-shortfilm-project",
        content=ANON_CONTENT,
        summary=ANON_CONTENT,
        source="tool-store",
        target="project",
        vector_score=0.0,
    )

    assert (
        lexical_score(
            query=ANON_OWNER_IDENTIFIER_QUERY,
            content=ANON_CONTENT,
            summary=ANON_CONTENT,
            source="tool-store",
            target="project",
        )
        == 0.0
    )
    assert owner.admitted is False
    assert owner.lexical_evidence is False
    assert owner.vector_evidence is False
    assert "unmatched_mixed_identifier" in owner.reason_codes
    assert alice.admitted is False
    assert alice.vector_evidence is False
    assert weak.admitted is False
    assert nonsense.admitted is False


def test_matched_mixed_identifier_is_admitted() -> None:
    admission = assess_candidate_admission(
        ANON_MATCHED_IDENTIFIER_QUERY,
        candidate_id="anon-wsl2-env",
        content=ANON_MATCHED_IDENTIFIER_CONTENT,
        summary=ANON_MATCHED_IDENTIFIER_CONTENT,
        source="tool-store",
        target="ops",
        vector_score=0.0,
    )

    assert admission.admitted is True
    assert admission.lexical_evidence is True
    assert assess_query_signal(ANON_MATCHED_IDENTIFIER_QUERY, [admission]).state == "positive"


def _high_vector_admission(query: str, content: str):
    return assess_candidate_admission(
        query,
        candidate_id="anon-identifier-boundary",
        content=content,
        summary=content,
        source="tool-store",
        target="ops",
        vector_score=0.99,
        vector_background_score=0.50,
    )


def test_mixed_identifier_rejects_longer_and_prefixed_near_matches() -> None:
    for content in (
        ANON_LONGER_IDENTIFIER_CONTENT,
        ANON_PREFIXED_IDENTIFIER_CONTENT,
        ANON_SUFFIXED_IDENTIFIER_CONTENT,
    ):
        assert (
            document_supports_mixed_letter_digit_identifiers(ANON_BOUNDARY_QUERY, content)
            is False
        )
        admission = _high_vector_admission(ANON_BOUNDARY_QUERY, content)
        assert admission.admitted is False
        assert admission.lexical_evidence is False
        assert admission.vector_evidence is False
        assert "unmatched_mixed_identifier" in admission.reason_codes


def test_mixed_identifier_accepts_exact_case_and_punctuation() -> None:
    for content in (
        ANON_EXACT_IDENTIFIER_CONTENT,
        ANON_CASE_IDENTIFIER_CONTENT,
        ANON_PUNCT_IDENTIFIER_CONTENT,
        ANON_MATCHED_IDENTIFIER_CONTENT,
    ):
        assert (
            document_supports_mixed_letter_digit_identifiers(ANON_BOUNDARY_QUERY, content)
            is True
        )
        admission = _high_vector_admission(ANON_BOUNDARY_QUERY, content)
        assert admission.admitted is True
        assert admission.lexical_evidence is True


def test_mixed_identifier_multi_identifier_needs_only_one_supported_token() -> None:
    assert mixed_letter_digit_identifiers(ANON_MULTI_IDENTIFIER_QUERY) == [
        "wsl2",
        "cuda12",
    ]
    assert (
        document_supports_mixed_letter_digit_identifiers(
            ANON_MULTI_IDENTIFIER_QUERY,
            ANON_MULTI_IDENTIFIER_PARTIAL_CONTENT,
        )
        is True
    )
    assert (
        document_supports_mixed_letter_digit_identifiers(
            ANON_MULTI_IDENTIFIER_QUERY,
            ANON_LONGER_IDENTIFIER_CONTENT,
        )
        is False
    )
    admitted = _high_vector_admission(
        ANON_MULTI_IDENTIFIER_QUERY,
        ANON_MULTI_IDENTIFIER_PARTIAL_CONTENT,
    )
    rejected = _high_vector_admission(
        ANON_MULTI_IDENTIFIER_QUERY,
        ANON_LONGER_IDENTIFIER_CONTENT,
    )
    assert admitted.admitted is True
    assert admitted.lexical_evidence is True
    assert rejected.admitted is False
    assert "unmatched_mixed_identifier" in rejected.reason_codes


def test_search_natural_question_returns_promoted_project(tmp_path) -> None:
    plugin = _plugin(tmp_path)
    try:
        ids, payload = _search_ids(plugin, ANON_NATURAL_QUERY)
        assert "anon-shortfilm-project" in ids, payload
        assert payload.get("count") >= 1
    finally:
        plugin.shutdown()


def test_search_exact_title_returns_same_promoted_project(tmp_path) -> None:
    plugin = _plugin(tmp_path)
    try:
        ids, payload = _search_ids(plugin, ANON_TITLE_QUERY)
        assert ids == ["anon-shortfilm-project"], payload
    finally:
        plugin.shutdown()


def test_search_name_plus_unmatched_identifier_is_empty(tmp_path) -> None:
    plugin = _plugin(tmp_path)
    try:
        owner_ids, owner_payload = _search_ids(plugin, ANON_OWNER_IDENTIFIER_QUERY)
        alice_ids, alice_payload = _search_ids(plugin, ANON_NAME_IDENTIFIER_QUERY)
        assert owner_ids == [], owner_payload
        assert alice_ids == [], alice_payload
    finally:
        plugin.shutdown()


def test_search_weak_overlap_and_nonsense_are_empty(tmp_path) -> None:
    plugin = _plugin(tmp_path)
    try:
        weak_ids, weak_payload = _search_ids(plugin, ANON_WEAK_QUERY)
        nonsense_ids, nonsense_payload = _search_ids(plugin, ANON_NONSENSE_QUERY)
        assert weak_ids == [], weak_payload
        assert nonsense_ids == [], nonsense_payload
    finally:
        plugin.shutdown()


def test_search_archived_project_stays_out_of_ordinary_recall(tmp_path) -> None:
    plugin = _plugin(tmp_path, lifecycle="archived")
    try:
        ids, payload = _search_ids(plugin, ANON_NATURAL_QUERY)
        assert ids == [], payload
        title_ids, title_payload = _search_ids(plugin, ANON_TITLE_QUERY)
        assert title_ids == [], title_payload
    finally:
        plugin.shutdown()
