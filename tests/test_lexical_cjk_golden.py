"""Held-out Chinese recall golden set with channel attribution.

Unlike the migration-quality fixtures, this corpus is deliberately disjoint from
the 数据库 benchmark corpus and covers synonym rewrites, typos, homophone and
near-shape confusions, high-frequency interference, negation, lifecycle-hidden
rows, scope isolation, and forbidden IDs. Every case is scored on the shadow
channel and on the legacy channel so a legacy hit can never mask a shadow
regression, and the whole golden is run with the vector companion absent and
with an empty-but-enabled vector store to prove the shadow result is stable in
both configurations.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_QUALITY_PROVENANCE,
    activate_generation,
    backfill_generation,
    create_shadow_generation,
    ensure_lexical_generation_schema,
    generation_integrity_report,
    lexical_quality_evidence_fingerprint,
    lexical_source_binding,
    mark_generation_ready,
)
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.storage_views import search_db_memories


class _Provider:
    def __init__(self, conn: sqlite3.Connection, scopes: list[str] | None = None):
        self._conn = conn
        self._lock = threading.RLock()
        self._accessible_scope_ids = scopes or ["scope-a"]
        self._retrieval_config = {"candidate_pool": 20, "min_score": 0.18}

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _config_value(_key: str, default):
        return default


def _store(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    *,
    scope: str = "scope-a",
    lifecycle: str = "promoted",
    timestamp: str,
) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id=scope,
        platform="telegram",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="",
        gateway_session_key="",
        agent_identity="aria",
        agent_workspace="workspace-a",
        session_id="session-a",
        source="user",
        target="memory",
        content=content,
        metadata=json.dumps({"lifecycle": lifecycle}),
        commit=False,
        timestamp=timestamp,
        enqueue_vector_intent=False,
    )


def _quality_receipt(conn: sqlite3.Connection) -> dict[str, object]:
    receipt = {
        "ok": True,
        "status": "ready",
        "generation_id": LEXICAL_GENERATION_ID,
        "synthetic_cjk_queries": 3,
        "synthetic_cjk_expected_found": 3,
        "live_cjk_queries": 0,
        "live_cjk_expected_found": 0,
        "english_queries": 1,
        "cjk_queries": 3,
        "cjk_expected_found": 3,
        "english_regressions": 0,
        "integrity": generation_integrity_report(conn, LEXICAL_GENERATION_ID),
        "source_binding": lexical_source_binding(conn),
        "provenance": dict(LEXICAL_QUALITY_PROVENANCE),
        "contains_raw_samples": False,
    }
    receipt["evidence_fingerprint"] = lexical_quality_evidence_fingerprint(receipt)
    return receipt


def _build_corpus() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)

    promoted = (
        (
            "m-cache-warmup",
            "上线前先预热缓存，把热点键批量加载，避免流量突发打穿到数据库。",
        ),
        (
            "m-perm-audit",
            "权限审计要覆盖服务账号的最小授权，先回收长期未用的高权限令牌。",
        ),
        (
            "m-release-window",
            "版本发布安排在低峰窗口，提前冻结变更，通知值班同学待命。",
        ),
        (
            "m-rollback-drill",
            "每个季度做一次回滚演练，验证备份可用、脚本可跑、人员熟练。",
        ),
        (
            "m-log-retention",
            "日志保留策略按合规要求配置，冷热分层，定期清理过期分片。",
        ),
        (
            "m-slow-query",
            "慢查询治理：先抓TOP耗时SQL，补索引或改写，再观察一周效果。",
        ),
        (
            "m-negation",
            "不要在高峰期直接重启缓存集群，瞬间失温会击穿数据库。",
        ),
    )
    for index, (memory_id, content) in enumerate(promoted):
        _store(
            conn,
            memory_id,
            content,
            timestamp=f"2026-06-{index + 1:02d}T08:00:00+00:00",
        )

    hidden = (
        ("m-hidden-candidate", "候选想法：用布隆过滤器拦截不存在的缓存键。", "candidate"),
        ("m-hidden-progress", "进行中的权限审计草稿，尚未评审定稿。", "in_progress"),
        ("m-hidden-archived", "旧架构的集中式缓存方案已经废弃归档。", "archived"),
    )
    for index, (memory_id, content, lifecycle) in enumerate(hidden):
        # Insert directly: store_row derives its own promoted lifecycle
        # metadata, so lifecycle-hidden fixtures must bypass it.
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, platform, user_id, chat_id, thread_id,
                gateway_session_key, agent_identity, agent_workspace, session_id,
                source, target, content, summary, metadata, created_at, updated_at
            ) VALUES (?, 'scope-a', 'telegram', 'user-a', 'chat-a', '', '', 'aria',
                      'workspace-a', 'session-a', 'user', 'memory', ?, '', ?, ?, ?)
            """,
            (
                memory_id,
                content,
                json.dumps({"lifecycle": lifecycle}),
                f"2026-07-{index + 1:02d}T08:00:00+00:00",
                f"2026-07-{index + 1:02d}T08:00:00+00:00",
            ),
        )

    _store(
        conn,
        "m-scope-b-release",
        "另一个事业群的版本发布窗口和冻结纪律安排。",
        scope="scope-b",
        timestamp="2026-06-15T08:00:00+00:00",
    )

    for index in range(20):
        _store(
            conn,
            f"m-noise-{index:02d}",
            f"值班周报第{index + 1}期：巡检监控大盘，处理告警工单，更新排班文档。",
            timestamp=f"2026-08-{index + 1:02d}T08:00:00+00:00",
        )
    conn.commit()

    create_shadow_generation(conn)
    while not backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=9)["complete"]:
        conn.commit()
    mark_generation_ready(
        conn,
        LEXICAL_GENERATION_ID,
        quality_receipt=_quality_receipt(conn),
    )
    activate_generation(conn, LEXICAL_GENERATION_ID, expected_current="")
    conn.commit()
    return conn


# Each case: (query, expected_ids, forbidden_ids, category)
_GOLDEN_CASES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("怎样防止热点流量把缓存打垮", ("m-cache-warmup",), (), "synonym_rewrite"),
    ("回滚演炼应该多久安排一次", ("m-rollback-drill",), (), "typo"),
    ("慢查寻问题要怎么治理", ("m-slow-query",), (), "homophone_near_shape"),
    ("版本发布应该挑什么时间窗口", ("m-release-window",), (), "high_frequency_interference"),
    ("高峰期可以直接重启缓存集群吗", ("m-negation",), (), "negation"),
    ("服务账号最小授权怎么审计", ("m-perm-audit",), (), "synonym_rewrite"),
    ("日志保留和过期分片清理策略", ("m-log-retention",), (), "high_frequency_interference"),
    (
        "候选的布隆过滤器想法",
        (),
        ("m-hidden-candidate",),
        "lifecycle_forbidden",
    ),
    (
        "进行中的权限审计草稿",
        (),
        ("m-hidden-progress",),
        "lifecycle_forbidden",
    ),
    (
        "废弃归档的缓存方案",
        (),
        ("m-hidden-archived",),
        "lifecycle_forbidden",
    ),
    (
        "版本发布窗口怎么安排",
        ("m-release-window",),
        ("m-scope-b-release",),
        "scope_isolation",
    ),
)

_ALL_FORBIDDEN_IDS = frozenset(
    {"m-hidden-candidate", "m-hidden-progress", "m-hidden-archived", "m-scope-b-release"}
)


def _search_ids(provider: _Provider, query: str, *, generation: str) -> list[str]:
    return [
        item.id
        for item in search_db_memories(
            provider,
            query,
            limit=10,
            generation_override=generation,
            allow_unreviewed_generation=True,
        )
    ]


def _metrics(
    results: dict[str, list[str]],
) -> dict[str, float]:
    reciprocal_ranks: list[float] = []
    dcg_gains: list[float] = []
    precisions: list[float] = []
    false_positive_hits = 0
    expected_cases = 0
    for query, expected, forbidden, _category in _GOLDEN_CASES:
        ids = results[query]
        false_positive_hits += sum(1 for item in forbidden if item in ids)
        if not expected:
            continue
        expected_cases += 1
        first_rank = 0
        for rank, memory_id in enumerate(ids, start=1):
            if memory_id in expected:
                first_rank = rank
                break
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        gains = [
            (1.0 / math.log2(rank + 1)) if memory_id in expected else 0.0
            for rank, memory_id in enumerate(ids, start=1)
        ]
        ideal = [1.0 / math.log2(rank + 1) for rank in range(1, len(expected) + 1)]
        dcg_gains.append(sum(gains) / max(sum(ideal), 1e-9))
        precisions.append(
            sum(1 for memory_id in ids[:10] if memory_id in expected) / 10.0
        )
    return {
        "mrr": sum(reciprocal_ranks) / max(len(reciprocal_ranks), 1),
        "ndcg_at_10": sum(dcg_gains) / max(len(dcg_gains), 1),
        "precision_at_10": sum(precisions) / max(len(precisions), 1),
        "false_positive_rate": false_positive_hits / max(len(_GOLDEN_CASES), 1),
        "expected_cases": float(expected_cases),
    }


def _run_golden(provider: _Provider, *, generation: str) -> dict[str, list[str]]:
    return {
        query: _search_ids(provider, query, generation=generation)
        for query, _expected, _forbidden, _category in _GOLDEN_CASES
    }


def test_held_out_chinese_golden_shadow_channel_metrics() -> None:
    conn = _build_corpus()
    provider = _Provider(conn)

    shadow_results = _run_golden(provider, generation=LEXICAL_GENERATION_ID)
    legacy_results = _run_golden(provider, generation="")
    shadow = _metrics(shadow_results)

    # Forbidden IDs must never surface, on either channel.
    for query, _expected, forbidden, category in _GOLDEN_CASES:
        for memory_id in forbidden:
            assert memory_id not in shadow_results[query], (category, query, memory_id)
            assert memory_id not in legacy_results[query], (category, query, memory_id)
    assert not any(
        memory_id in ids
        for ids in shadow_results.values()
        for memory_id in _ALL_FORBIDDEN_IDS
    )

    # Shadow channel quality floor on held-out Chinese queries.
    assert shadow["mrr"] >= 0.6
    assert shadow["ndcg_at_10"] >= 0.5
    assert shadow["precision_at_10"] >= 0.05
    assert shadow["false_positive_rate"] == 0.0

    # Attribution: the shadow channel must not be worse than legacy anywhere,
    # and must be strictly better on at least two expected-id cases, so a
    # legacy hit can never mask a shadow-channel regression.
    shadow_case_mrr: dict[str, float] = {}
    legacy_case_mrr: dict[str, float] = {}
    for query, expected, _forbidden, _category in _GOLDEN_CASES:
        if not expected:
            continue
        for store, results in (
            (shadow_case_mrr, shadow_results),
            (legacy_case_mrr, legacy_results),
        ):
            rank = 0
            for index, memory_id in enumerate(results[query], start=1):
                if memory_id in expected:
                    rank = index
                    break
            store[query] = 1.0 / rank if rank else 0.0
    strictly_better = sum(
        shadow_case_mrr[query] > legacy_case_mrr[query] for query in shadow_case_mrr
    )
    assert all(
        shadow_case_mrr[query] >= legacy_case_mrr[query] for query in shadow_case_mrr
    )
    assert strictly_better >= 2, (shadow_case_mrr, legacy_case_mrr)
    conn.close()


def test_held_out_golden_is_stable_with_vector_companion_enabled() -> None:
    conn = _build_corpus()
    provider = _Provider(conn)
    without_vector = _run_golden(provider, generation=LEXICAL_GENERATION_ID)

    # A vector companion that is enabled but returns no hits must not change
    # shadow-channel behavior; degradation remains attributable to the shadow
    # channel itself.
    provider._vector_ready = True
    provider._vector_store = type("_EmptyVectorStore", (), {"search": lambda self, *a, **k: []})()
    provider._embedder = object()
    provider._vector_config = {"top_k": 10}
    with_vector = _run_golden(provider, generation=LEXICAL_GENERATION_ID)

    assert with_vector == without_vector
    assert _metrics(with_vector)["false_positive_rate"] == 0.0
    conn.close()
