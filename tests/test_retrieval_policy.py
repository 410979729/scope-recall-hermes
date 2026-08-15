"""Tests for retrieval policy, lexical/vector behavior, lifecycle filters, and default limits.

They protect recall quality and prompt-budget behavior across ranking changes."""

from __future__ import annotations

import sqlite3

from scope_recall.config import DEFAULT_CONFIG
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.recall_pipeline import rank_recall_items
from scope_recall.gating import (
    matched_query_intent_terms,
    query_requests_current_state,
    semantic_query_tokens,
)
from scope_recall.scoring import lexical_score
from scope_recall.sql_store import ensure_schema
from scope_recall.storage_views import search_vector_memories


class DummyProvider:
    def __init__(self, retrieval_config, *, db_items=None, vector_items=None, curated_items=None):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._db_items = db_items
        self._vector_items = list(vector_items or [])
        self._curated_items = list(curated_items or [])

    def _search_db_memories(self, query, *, limit):
        if self._db_items is not None:
            return self._db_items[:limit]
        return [
            RecallItem(
                id="general-1",
                content="Deploy command is uv run app.",
                summary="Deploy command is uv run app.",
                source="turn-user",
                target="general",
                score=1.0,
                updated_at="2026-05-01T00:00:00+00:00",
                metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": self._scope_id},
            ),
            RecallItem(
                id="memory-1",
                content="Deploy command is uv run app.",
                summary="Deploy command is uv run app.",
                source="tool-store",
                target="memory",
                score=0.8,
                updated_at="2026-05-01T00:00:00+00:00",
                metadata={"lexical_score": 0.8, "vector_score": 0.0, "scope_id": self._shared_scope_id},
            ),
        ]

    def _search_vector_memories(self, query, *, limit):
        return self._vector_items[:limit]

    def _search_curated_memories(self, query):
        return self._curated_items

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default


def test_lexical_score_durable_target_beats_comparable_general_scratch():
    general = lexical_score(
        query="deploy command uv run app",
        content="Deploy command is uv run app.",
        summary="Deploy command is uv run app.",
        source="turn-user",
        target="general",
    )
    durable = lexical_score(
        query="deploy command uv run app",
        content="Deploy command is uv run app.",
        summary="Deploy command is uv run app.",
        source="tool-store",
        target="memory",
    )

    assert durable > general


def test_include_general_never_suppresses_general_in_automatic_recall():
    provider = DummyProvider({"mode": "lexical", "include_general": "never", "general_weight": 0.35, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert [item.target for item in results] == ["memory"]


def test_include_general_same_scope_downranks_but_keeps_local_scratch():
    provider = DummyProvider({"mode": "lexical", "include_general": "same-scope", "general_weight": 0.35, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert [item.target for item in results] == ["memory", "general"]
    assert results[0].score > results[1].score


def test_low_importance_general_scratch_is_filtered_when_threshold_configured():
    general = RecallItem(
        id="general-low",
        content="Project Atlas dinner note after deployment discussion.",
        summary="Project Atlas dinner note after deployment discussion.",
        source="turn-user",
        target="general",
        score=1.0,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": "local-scope", "importance": 0.1},
    )
    durable = RecallItem(
        id="project-atlas",
        content="Project Atlas production deploy command is uv run atlas-server.",
        summary="Project Atlas production deploy command.",
        source="tool-store",
        target="project",
        score=0.8,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.8, "vector_score": 0.0, "scope_id": "shared-scope", "importance": 0.9},
    )
    provider = DummyProvider(
        {"mode": "lexical", "include_general": "same-scope", "general_weight": 0.35, "general_min_importance": 0.2, "min_score": 0.0},
        db_items=[general, durable],
    )

    results = RecallService(provider).search_memories("Project Atlas production deploy command", limit=5)

    assert [item.id for item in results] == ["project-atlas"]


def test_zero_or_missing_importance_general_scratch_is_filtered_when_threshold_configured():
    items = [
        RecallItem(
            id="general-zero",
            content="Project Atlas zero importance scratch.",
            summary="Project Atlas zero importance scratch.",
            source="turn-user",
            target="general",
            score=1.0,
            updated_at="2026-05-01T00:00:00+00:00",
            metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": "local-scope", "importance": 0.0},
        ),
        RecallItem(
            id="general-missing",
            content="Project Atlas missing importance scratch.",
            summary="Project Atlas missing importance scratch.",
            source="turn-user",
            target="general",
            score=1.0,
            updated_at="2026-05-01T00:00:01+00:00",
            metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": "local-scope"},
        ),
    ]
    provider = DummyProvider(
        {"mode": "lexical", "include_general": "same-scope", "general_weight": 0.35, "general_min_importance": 0.2, "min_score": 0.0},
        db_items=items,
    )

    results = RecallService(provider).search_memories("Project Atlas scratch", limit=5)

    assert results == []


def test_project_entity_mismatch_filters_cross_project_hits():
    atlas = RecallItem(
        id="project-atlas",
        content="Project Atlas production deploy command is uv run atlas-server.",
        summary="Project Atlas production deploy command.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.9, "vector_score": 0.0, "scope_id": "shared-scope", "entities": ["Project Atlas"]},
    )
    zephyr = RecallItem(
        id="project-zephyr",
        content="Project Zephyr rollback runbook uses systemctl restart zephyr-worker after queue drain.",
        summary="Project Zephyr rollback worker queue drain.",
        source="tool-store",
        target="ops",
        score=0.85,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.85, "vector_score": 0.0, "scope_id": "shared-scope", "entities": ["Project Zephyr"]},
    )
    provider = DummyProvider(
        {"mode": "lexical", "include_general": "same-scope", "entity_scope_filter_enabled": True, "min_score": 0.0},
        db_items=[atlas, zephyr],
    )

    results = RecallService(provider).search_memories("Project Zephyr rollback worker queue drain", limit=5)

    assert [item.id for item in results] == ["project-zephyr"]


def test_entity_mismatch_filters_named_entities_without_project_prefix():
    atlas = RecallItem(
        id="atlas-api",
        content="Atlas API base URL is https://atlas.internal.",
        summary="Atlas API base URL.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.9, "vector_score": 0.0, "scope_id": "shared-scope", "entities": ["Atlas"]},
    )
    northstar = RecallItem(
        id="northstar-api",
        content="Northstar API base URL is https://northstar.internal.",
        summary="Northstar API base URL.",
        source="tool-store",
        target="project",
        score=0.8,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.8, "vector_score": 0.0, "scope_id": "shared-scope", "entities": ["Northstar"]},
    )
    provider = DummyProvider(
        {"mode": "lexical", "include_general": "same-scope", "entity_scope_filter_enabled": True, "min_score": 0.0},
        db_items=[atlas, northstar],
    )

    results = RecallService(provider).search_memories("Northstar API base URL current", limit=5)

    assert [item.id for item in results] == ["northstar-api"]


def test_declared_entity_scope_overrides_an_incidental_content_mention() -> None:
    incidental = RecallItem(
        id="titan-incidental-atlas",
        content="Titan recovery notes mention Atlas only as an unrelated example.",
        summary="Titan recovery procedure.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": ["Titan"],
        },
    )
    provider = DummyProvider(
        {
            "mode": "lexical",
            "entity_scope_filter_enabled": True,
            "min_score": 0.0,
        },
        db_items=[incidental],
    )

    results = RecallService(provider).search_memories(
        "What must be verified before Atlas recovery?", limit=5
    )

    assert results == []


def test_claim_subject_accepts_mixed_case_entity_despite_other_content_names() -> None:
    atlas = RecallItem(
        id="atlas-claim-with-titan-example",
        content="Recovery steps use Titan as a contrasting example.",
        summary="Recovery procedure for the declared subject.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "claim": {
                "subject": "Atlas",
                "predicate": "recovery procedure",
                "value": "verify backup",
            },
        },
    )
    provider = DummyProvider(
        {
            "mode": "lexical",
            "entity_scope_filter_enabled": True,
            "min_score": 0.0,
        },
        db_items=[atlas],
    )

    results = RecallService(provider).search_memories(
        "What is AtLaS recovery procedure?", limit=5
    )

    assert [item.id for item in results] == ["atlas-claim-with-titan-example"]


def test_structured_entity_scope_is_casefolded_and_cannot_be_rescued_by_prose() -> None:
    atlas = RecallItem(
        id="atlas-structured",
        content="Atlas recovery notes mention Titan only as an unrelated example.",
        summary="Atlas recovery procedure.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": ["atlas"],
            "claim": {"subject": "Atlas", "predicate": "procedure", "value": "x"},
        },
    )
    titan = RecallItem(
        id="titan-structured",
        content="Recovery notes mention Project Atlas only as an unrelated example.",
        summary="Recovery procedure for the declared subject.",
        source="tool-store",
        target="project",
        score=0.8,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.8,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": ["titan"],
        },
    )
    provider = DummyProvider(
        {
            "mode": "lexical",
            "entity_scope_filter_enabled": True,
            "min_score": 0.0,
        },
        db_items=[atlas, titan],
    )
    service = RecallService(provider)

    assert service._project_entities("project\nproject atlas") == {"project:atlas"}

    for query in ("Atlas recovery", "AtLaS recovery", "atlas recovery"):
        assert [item.id for item in service.search_memories(query, limit=5)] == [
            "atlas-structured"
        ]
    for query in ("Titan recovery", "TiTaN recovery", "titan recovery"):
        assert [item.id for item in service.search_memories(query, limit=5)] == [
            "titan-structured"
        ]
    assert service.search_memories("Project Atlas recovery", limit=5)[0].id == (
        "atlas-structured"
    )


def test_generic_metadata_terms_do_not_override_a_conflicting_proper_name() -> None:
    titan = RecallItem(
        id="titan-with-generic-entities",
        content="Titan recovery procedure requires a verified backup.",
        summary="Titan recovery procedure.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": ["recovery", "procedure"],
        },
    )
    service = RecallService(
        DummyProvider(
            {
                "mode": "lexical",
                "entity_scope_filter_enabled": True,
                "min_score": 0.0,
            },
            db_items=[titan],
        )
    )

    assert service.search_memories("Quartz recovery procedure", limit=5) == []


def test_generic_chinese_system_question_keeps_strong_semantic_hit():
    current_host = RecallItem(
        id="current-windows-host",
        content="玉衡当前运行在原生 Windows 11 本机。",
        summary="玉衡当前运行环境",
        source="tool-store",
        target="memory",
        score=0.82,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={
            "lexical_score": 0.0,
            "vector_score": 0.82,
            "scope_id": "shared-scope",
            "entities": ["玉衡", "windows"],
        },
    )
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": True,
            "min_score": 0.18,
            "vector_only_min_score": 0.30,
        },
        db_items=[],
        vector_items=[current_host],
    )
    service = RecallService(provider)

    results = service.search_memories("你现在跑什么系统", limit=5)

    assert [item.id for item in results] == ["current-windows-host"]
    assert service.last_funnel_trace["filters"]["entity_scope_mismatch"] == 0


def test_chinese_location_question_drops_interrogative_fragments():
    assert semantic_query_tokens("玉衡在哪") == ["玉衡"]
    assert semantic_query_tokens("开阳星在哪") == ["开阳星"]
    assert semantic_query_tokens("北斗玉衡在哪") == ["北斗玉衡"]


def test_cjk_polite_prefix_is_not_part_of_explicit_scope_entity() -> None:
    service = RecallService(
        DummyProvider(
            {
                "mode": "lexical",
                "include_general": "same-scope",
                "entity_scope_filter_enabled": True,
                "min_score": 0.0,
            },
            db_items=[],
        )
    )

    assert service._explicit_query_scope_entities("请告诉我星河目前API 地址") == {"星河"}
    assert service._explicit_query_scope_entities("我想知道云舟现在的生产端口") == {"云舟"}


def test_cjk_entity_outranks_generic_artifact_anchor_metadata() -> None:
    xinghe = RecallItem(
        id="xinghe-current",
        content=(
            "星河目前的API 地址是https://api.xinghe.example/v2，这是最近一次在线配置核验结果。"
            "\n\nArtifact anchors: URL https://api.xinghe.example/v2"
        ),
        summary="星河当前 API 地址",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-08-10T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": [
                "anchors",
                "api",
                "artifact",
                "https://api.xinghe.example/v2",
                "url",
                "星河",
            ],
        },
    )
    service = RecallService(
        DummyProvider(
            {
                "mode": "lexical",
                "entity_scope_filter_enabled": True,
                "min_score": 0.0,
            },
            db_items=[xinghe],
        )
    )

    for query in (
        "星河现在的API 地址是什么？",
        "请告诉我星河目前API 地址",
        "星河最近核验的API 地址是多少",
    ):
        assert [item.id for item in service.search_memories(query, limit=5)] == [
            "xinghe-current"
        ]


def test_cjk_prose_prefix_is_not_treated_as_a_hard_entity_scope() -> None:
    configuration = RecallItem(
        id="current-config-location",
        content="当前配置保存在受控目录，修改前需要先读取现有文件。",
        summary="当前配置的保存位置",
        source="tool-store",
        target="memory",
        score=0.9,
        updated_at="2026-08-09T00:00:00+00:00",
        metadata={
            "lexical_score": 0.9,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": [],
        },
    )
    provider = DummyProvider(
        {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": True,
            "min_score": 0.0,
        },
        db_items=[configuration],
    )
    service = RecallService(provider)

    results = service.search_memories("配置现在存在哪里", limit=5)

    assert [item.id for item in results] == ["current-config-location"]
    assert service.last_funnel_trace["filters"]["entity_scope_mismatch"] == 0


def test_historical_location_question_does_not_request_current_state():
    assert query_requests_current_state("玉衡在哪") is True
    assert query_requests_current_state("玉衡以前在哪") is False
    assert query_requests_current_state("where is Yuheng now") is True
    assert query_requests_current_state("where was Yuheng previously") is False


def test_chinese_location_intent_ranks_current_host_above_operator_noise():
    query = "玉衡在哪"
    operator_noise = RecallItem(
        id="release-noise",
        content="Scope Recall 玉衡在 release 收口阶段必须复查测试门禁。",
        summary="玉衡在 release 收口阶段的测试门禁",
        source="journal-digest",
        target="memory",
        score=1.0,
        updated_at="2026-07-20T00:00:00+00:00",
        metadata={
            "lexical_score": lexical_score(
                query=query,
                content="Scope Recall 玉衡在 release 收口阶段必须复查测试门禁。",
                summary="玉衡在 release 收口阶段的测试门禁",
                source="journal-digest",
                target="memory",
            ),
            "vector_score": 0.0,
            "bm25_score": 1.0,
            "scope_id": "shared-scope",
            "entities": ["玉衡", "scope-recall"],
        },
    )
    current_host = RecallItem(
        id="current-windows-host",
        content="玉衡在新家 Windows 本机，当前 live 根位于本机工作区。",
        summary="玉衡当前所在主机和 live 根",
        source="builtin-curated",
        target="user",
        score=0.0,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={
            "lexical_score": lexical_score(
                query=query,
                content="玉衡在新家 Windows 本机，当前 live 根位于本机工作区。",
                summary="玉衡当前所在主机和 live 根",
                source="builtin-curated",
                target="user",
            ),
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "entities": ["玉衡", "windows"],
        },
    )
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": True,
            "min_score": 0.18,
        },
        db_items=[operator_noise],
        curated_items=[current_host],
    )

    results = RecallService(provider).search_memories(query, limit=5)

    assert [item.id for item in results][:2] == ["current-windows-host", "release-noise"]


def test_intent_match_breaks_equal_score_tie_before_recency():
    identity = RecallItem(
        id="identity",
        content="玉衡是北斗第五星。",
        summary="玉衡身份",
        source="builtin-curated",
        target="user",
        score=1.0,
        updated_at="2026-07-31T00:00:00+00:00",
        metadata={"base_score": 1.0, "intent_matched": False},
    )
    current_host = RecallItem(
        id="current-host",
        content="玉衡在 Windows 本机。",
        summary="玉衡当前主机",
        source="builtin-curated",
        target="user",
        score=1.0,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={"base_score": 1.0, "intent_matched": True},
    )

    ranked = rank_recall_items([identity, current_host])

    assert [item.id for item in ranked] == ["current-host", "identity"]


def test_current_os_query_prefers_current_state_and_suppresses_weak_local_hits():
    query = "你现在跑什么系统"
    stale_decision = RecallItem(
        id="stale-wsl-decision",
        content="Agent 部署决策：推荐 Windows 11 宿主机加 Linux 虚拟机或 WSL2。",
        summary="旧部署决策",
        source="journal-digest",
        target="project",
        score=1.0,
        updated_at="2026-06-22T00:00:00+00:00",
        metadata={
            "lexical_score": lexical_score(
                query=query,
                content="Agent 部署决策：推荐 Windows 11 宿主机加 Linux 虚拟机或 WSL2。",
                summary="旧部署决策",
                source="journal-digest",
                target="project",
            ),
            "bm25_score": 1.0,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "memory_type": "decision",
        },
    )
    weak_local_hit = RecallItem(
        id="local-task-preference",
        content="默认明确且可回滚的本机任务可以自主推进。",
        summary="本机任务操作偏好",
        source="builtin-curated",
        target="user",
        score=0.0,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={
            "lexical_score": lexical_score(
                query=query,
                content="默认明确且可回滚的本机任务可以自主推进。",
                summary="本机任务操作偏好",
                source="builtin-curated",
                target="user",
            ),
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "memory_type": "preference",
        },
    )
    current_host = RecallItem(
        id="current-windows-host",
        content="玉衡在新家 Windows 本机，拒绝 WSL2。",
        summary="玉衡当前所在系统",
        source="builtin-curated",
        target="user",
        score=0.0,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={
            "lexical_score": lexical_score(
                query=query,
                content="玉衡在新家 Windows 本机，拒绝 WSL2。",
                summary="玉衡当前所在系统",
                source="builtin-curated",
                target="user",
            ),
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "memory_type": "preference",
        },
    )
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": True,
            "min_score": 0.18,
        },
        db_items=[stale_decision],
        curated_items=[weak_local_hit, current_host],
    )

    results = RecallService(provider).search_memories(query, limit=5)

    assert [item.id for item in results][:2] == [
        "current-windows-host",
        "stale-wsl-decision",
    ]
    assert "local-task-preference" not in {item.id for item in results}
    by_id = {item.id: item for item in results}
    assert by_id["current-windows-host"].metadata["intent_matched"] is True


def test_current_os_query_does_not_rank_linux_manual_above_debian_answer():
    query = "你现在跑什么系统"
    current_answer = RecallItem(
        id="real-debian-current",
        content="玉衡当前使用 Debian 12 系统。",
        summary="玉衡当前操作系统",
        source="tool-store",
        target="memory",
        score=0.945,
        updated_at="2026-07-31T00:00:00+00:00",
        metadata={
            "lexical_score": 0.945,
            "vector_score": 0.0,
            "base_score": 0.945,
            "scope_id": "shared-scope",
            "memory_type": "factual",
        },
    )
    linux_manual = RecallItem(
        id="unrelated-linux-manual",
        content="本机资料库保存了一份 Linux 安装手册。",
        summary="Linux 安装资料",
        source="builtin-curated",
        target="user",
        score=0.25,
        updated_at="2026-07-30T00:00:00+00:00",
        metadata={
            "lexical_score": 0.25,
            "vector_score": 0.0,
            "base_score": 0.25,
            "scope_id": "shared-scope",
            "memory_type": "resource",
        },
    )
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": True,
            "min_score": 0.0,
        },
        db_items=[current_answer],
        curated_items=[linux_manual],
    )

    results = RecallService(provider).search_memories(query, limit=5)

    assert [item.id for item in results][:2] == [
        "real-debian-current",
        "unrelated-linux-manual",
    ]
    by_id = {item.id: item for item in results}
    assert by_id["real-debian-current"].metadata["intent_matched"] is True
    assert by_id["unrelated-linux-manual"].metadata["intent_matched"] is False


def test_current_os_query_requires_answer_evidence_for_platform_documents():
    query = "你现在跑什么系统"
    cases = [
        ("Ubuntu 24.04", "资料库保存了一份 Ubuntu 安装文档。"),
        ("Rocky Linux 9", "资料库保存了一份 Rocky Linux 迁移指南。"),
        ("Windows 11", "资料库保存了一份 Windows 安装检查表。"),
    ]

    for index, (platform_name, document) in enumerate(cases):
        answer = RecallItem(
            id=f"answer-{index}",
            content=f"玉衡当前运行在 {platform_name} 系统上。",
            summary="玉衡当前操作系统",
            source="tool-store",
            target="memory",
            score=0.9,
            updated_at="2026-07-31T00:00:00+00:00",
            metadata={
                "lexical_score": 0.9,
                "vector_score": 0.0,
                "base_score": 0.9,
                "scope_id": "shared-scope",
                "memory_type": "factual",
            },
        )
        reference = RecallItem(
            id=f"reference-{index}",
            content=document,
            summary="平台参考文档",
            source="builtin-curated",
            target="user",
            score=0.3,
            updated_at="2026-07-30T00:00:00+00:00",
            metadata={
                "lexical_score": 0.3,
                "vector_score": 0.0,
                "base_score": 0.3,
                "scope_id": "shared-scope",
                "memory_type": "resource",
            },
        )
        service = RecallService(
            DummyProvider(
                {
                    "mode": "hybrid",
                    "include_general": "same-scope",
                    "entity_scope_filter_enabled": True,
                    "min_score": 0.0,
                },
                db_items=[answer],
                curated_items=[reference],
            )
        )

        results = service.search_memories(query, limit=5)
        by_id = {item.id: item for item in results}

        assert [item.id for item in results][:2] == [answer.id, reference.id]
        assert by_id[answer.id].metadata["intent_matched"] is True
        assert by_id[reference.id].metadata["intent_matched"] is False


def test_archived_duplicate_does_not_suppress_active_duplicate():
    archived = RecallItem(
        id="archived-newer",
        content="Project Atlas deploy command is uv run atlas-server.",
        summary="Project Atlas deploy command.",
        source="tool-store",
        target="project",
        score=1.0,
        updated_at="2026-06-01T00:00:00+00:00",
        metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": "shared-scope", "lifecycle": "archived"},
    )
    active = RecallItem(
        id="active-older",
        content="Project Atlas deploy command is uv run atlas-server.",
        summary="Project Atlas deploy command.",
        source="tool-store",
        target="project",
        score=0.8,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.8, "vector_score": 0.0, "scope_id": "shared-scope"},
    )
    provider = DummyProvider(
        {"mode": "lexical", "include_general": "same-scope", "min_score": 0.0},
        db_items=[archived, active],
    )

    results = RecallService(provider).search_memories("Project Atlas deploy command", limit=5)

    assert [item.id for item in results] == ["active-older"]


def test_include_general_always_allows_general_debug_mode():
    provider = DummyProvider({"mode": "lexical", "include_general": "always", "general_weight": 1.0, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert {item.target for item in results} == {"memory", "general"}

def test_timezone_preference_answer_outranks_generic_timezone_documents():
    query = "工作电脑应该使用哪个时区"
    preference = RecallItem(
        id="timezone-preference",
        source="builtin-curated",
        target="user",
        content="时区偏好：工作电脑使用美国东部时区 America/New_York。",
        summary="工作电脑使用 America/New_York。",
        score=0.47,
        updated_at="2026-07-01T00:00:00+00:00",
        metadata={"memory_type": "preference", "importance": 0.8, "lexical_score": 0.47, "vector_score": 0.0},
    )
    generic_rollout = RecallItem(
        id="generic-rollout",
        source="journal-digest",
        target="memory",
        content="模型 rollout 可能受账号批次和时区问题影响。",
        summary="模型 rollout 与时区问题。",
        score=1.0,
        updated_at="2026-07-01T00:00:00+00:00",
        metadata={"memory_type": "workflow", "importance": 0.8, "lexical_score": 1.0, "vector_score": 0.0},
    )
    utc_parser = RecallItem(
        id="generic-utc-parser",
        source="journal-digest",
        target="project",
        content="naive datetime 必须按 UTC 解析，不能使用本地 timezone。",
        summary="时间戳按 UTC 解析。",
        score=0.68,
        updated_at="2026-07-01T00:00:00+00:00",
        metadata={"memory_type": "constraint", "importance": 1.0, "lexical_score": 0.68, "vector_score": 0.0},
    )
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "lexical_weight": 1.0,
            "vector_weight": 0.0,
            "min_score": 0.0,
            "top_k": 3,
            "candidate_pool": 3,
            "fusion_strategy": "weighted",
            "freshness_base_weight": 0.0,
            "freshness_step_weight": 0.0,
            "freshness_max_weight": 0.0,
        },
        db_items=[generic_rollout, utc_parser],
        curated_items=[preference],
    )

    results = RecallService(provider).search_memories(query, limit=3)

    assert results[0].id == "timezone-preference"
    assert results[0].metadata["intent_matched"] is True
    assert all(
        item.metadata["intent_matched"] is False
        for item in results
        if item.id != "timezone-preference"
    )
    assert "america/new_york" in matched_query_intent_terms(
        "Which timezone should this workstation use?",
        "This workstation uses America/New_York.",
    )


def test_hybrid_vector_only_match_suppresses_low_confidence_unrelated_ops_row():
    vector_item = RecallItem(
        id="ops-openclaw",
        content="OpenClaw sibling upgrade pitfall for 天璇 and 天权.",
        summary="OpenClaw sibling upgrade pitfall for 天璇 and 天权.",
        source="tool-store",
        target="ops",
        score=0.59,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.0, "vector_score": 0.59, "scope_id": "shared-scope"},
    )
    provider = DummyProvider(
        {"mode": "hybrid", "include_general": "same-scope", "general_weight": 0.35, "min_score": 0.18},
        db_items=[],
        vector_items=[vector_item],
    )

    results = RecallService(provider).search_memories("普通无关对话测试：今天午饭吃什么比较好", limit=5)

    assert results == []


def test_hybrid_vector_only_match_keeps_high_confidence_semantic_hit():
    vector_item = RecallItem(
        id="memory-scope-recall",
        content="Scope Recall uses SQLite truth storage and LanceDB semantic companion.",
        summary="Scope Recall architecture: SQLite truth + LanceDB semantic companion.",
        source="tool-store",
        target="memory",
        score=0.78,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.0, "vector_score": 0.78, "scope_id": "shared-scope"},
    )
    provider = DummyProvider(
        {"mode": "hybrid", "include_general": "same-scope", "general_weight": 0.35, "min_score": 0.18},
        db_items=[],
        vector_items=[vector_item],
    )

    results = RecallService(provider).search_memories("memory architecture database storage", limit=5)

    assert [item.id for item in results] == ["memory-scope-recall"]


def test_vector_only_filter_uses_packaged_default_threshold_without_override():
    threshold = float(DEFAULT_CONFIG["retrieval"]["vector_only_min_score"])
    below = RecallItem(
        id="below-default",
        content="Vector-only candidate immediately below the packaged default.",
        summary="below default",
        source="tool-store",
        target="memory",
        score=threshold - 0.001,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={
            "lexical_score": 0.0,
            "vector_score": threshold - 0.001,
            "scope_id": "shared-scope",
        },
    )
    at_default = RecallItem(
        id="at-default",
        content="Vector-only candidate exactly at the packaged default.",
        summary="at default",
        source="tool-store",
        target="memory",
        score=threshold,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={
            "lexical_score": 0.0,
            "vector_score": threshold,
            "scope_id": "shared-scope",
        },
    )
    config = {
        "mode": "hybrid",
        "include_general": "same-scope",
        "min_score": 0.0,
        "fact_freshness_untracked_penalty": 0.0,
    }

    below_service = RecallService(
        DummyProvider(config, db_items=[], vector_items=[below])
    )
    at_service = RecallService(
        DummyProvider(config, db_items=[], vector_items=[at_default])
    )

    assert below_service.search_memories("semantic-only query", limit=5) == []
    assert [item.id for item in at_service.search_memories("semantic-only query", limit=5)] == [
        "at-default"
    ]
    assert (
        below_service.last_funnel_trace["filters"]["vector_only_below_min_score"]
        == 1
    )


class NoopLock:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


class FakeEmbedder:
    def embed_query(self, query):  # noqa: ARG002
        return [0.0, 0.0]


class FakeVectorStore:
    def search(self, query_vector, *, scope_id, limit):  # noqa: ARG002
        return [
            {
                "id": "stale-vector-only",
                "scope_id": scope_id,
                "source": "tool-store",
                "target": "memory",
                "content": "Deleted secret should not return from stale vector companion.",
                "summary": "Deleted secret stale vector.",
                "updated_at": "2026-05-01T00:00:00+00:00",
                "_distance": 0.05,
            }
        ]


class VectorProvider:
    def __init__(self, conn):
        self._conn = conn
        self._lock = NoopLock()
        self._vector_ready = True
        self._vector_store = FakeVectorStore()
        self._embedder = FakeEmbedder()
        self._vector_config = {"top_k": 5}
        self._retrieval_config = {"vector_min_score": 0.1}
        self._accessible_scope_ids = ["shared-scope"]

    def _require_conn(self):
        return self._conn


def test_vector_search_drops_rows_missing_sql_truth():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    results = search_vector_memories(VectorProvider(conn), "deleted secret", limit=5)

    assert results == []

