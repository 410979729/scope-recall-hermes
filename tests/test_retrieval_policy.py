from __future__ import annotations

import sqlite3

from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.scoring import lexical_score
from scope_recall.sql_store import ensure_schema
from scope_recall.storage_views import search_vector_memories


class DummyProvider:
    def __init__(self, retrieval_config, *, db_items=None, vector_items=None):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._db_items = db_items
        self._vector_items = list(vector_items or [])

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
        return []

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

