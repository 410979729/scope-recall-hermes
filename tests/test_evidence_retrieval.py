"""Tests for multi-query evidence-set fusion used by long-context recall."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

from scope_recall.evidence_retrieval import merge_evidence_rankings
from scope_recall.models import RecallItem
from scope_recall.provider import ScopeRecallMemoryProvider
from scope_recall.recall import RecallService
from scope_recall.schemas import SCOPE_RECALL_SEARCH_SCHEMA
from scope_recall.tooling import ScopeRecallToolService


def _item(memory_id: str, score: float) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"content {memory_id}",
        summary=f"summary {memory_id}",
        source="tool-store",
        target="general",
        score=score,
        updated_at="2026-08-09T00:00:00+00:00",
        metadata={
            "original": memory_id,
            "candidate_admission": {"admitted": True},
        },
    )


def test_trace_state_is_request_local_across_threads() -> None:
    service = RecallService(object())
    barrier = threading.Barrier(2)

    def write_then_read(request_index: int, query: str) -> int:
        service.last_funnel_trace = {
            "query": query,
            "request_label": query,
            "request_index": request_index,
        }
        service.last_evidence_set_trace = {
            "primary_query": query,
            "request_label": query,
            "request_index": request_index,
        }
        barrier.wait(timeout=2.0)
        assert service.last_evidence_set_trace["request_index"] == request_index
        assert "primary_query" not in service.last_evidence_set_trace
        assert "request_label" not in service.last_evidence_set_trace
        return int(service.last_funnel_trace["request_index"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write_then_read, index, query)
            for index, query in enumerate(("alpha", "beta"))
        ]
        assert [future.result(timeout=2.0) for future in futures] == [0, 1]


def test_direct_search_signature_cannot_bypass_egress_sanitization() -> None:
    import inspect

    parameters = inspect.signature(RecallService.search_memories).parameters

    assert "sanitize_output" not in parameters
    assert "query_vector" not in parameters


def test_evidence_set_sanitizes_only_final_merged_window(monkeypatch) -> None:
    import scope_recall.recall as recall_module

    service = RecallService(object())
    internal_calls = []
    safe_calls = []

    rankings = {
        "main": [_item("a", 0.9), _item("b", 0.8)],
        "variant": [_item("c", 0.95), _item("d", 0.7)],
    }

    def fake_search(query, *, limit, recall_mode, sanitize_output=None):
        internal_calls.append(sanitize_output)
        service.last_funnel_trace = {"query": query}
        return rankings[query]

    def fake_safe(item):
        safe_calls.append(item.id)
        return item

    monkeypatch.setattr(service, "_search_memories_internal", fake_search)
    monkeypatch.setattr(recall_module, "_safe_recall_item", fake_safe)

    merged = service.search_evidence_set(
        "main",
        query_variants=["variant"],
        limit=2,
        per_query_limit=2,
    )

    assert internal_calls == [False, False]
    assert len(merged) == 2
    assert safe_calls == [item.id for item in merged]


def test_sanitizer_only_processes_ranked_egress_window(monkeypatch) -> None:
    import scope_recall.recall as recall_module

    ranked = [_item("first", 0.9), _item("second", 0.8), _item("third", 0.7)]
    calls = []

    def fake_safe(item):
        calls.append(item.id)
        return item

    monkeypatch.setattr(recall_module, "_safe_recall_item", fake_safe)

    returned = recall_module._sanitize_recall_window(ranked, limit=2)

    assert [item.id for item in returned] == ["first", "second"]
    assert calls == ["first", "second"]


def test_merge_evidence_rankings_preserves_query_diversity_and_rrf_provenance() -> None:
    merged = merge_evidence_rankings(
        [
            ("Alice concert", [_item("a", 0.9), _item("b", 0.8)]),
            ("Bob concert", [_item("c", 0.85), _item("a", 0.7)]),
        ],
        limit=3,
    )

    assert [item.id for item in merged] == ["a", "c", "b"]
    assert merged[0].metadata["evidence_query_hits"] == 2
    assert merged[0].metadata["evidence_query_ranks"] == {
        "Alice concert": 1,
        "Bob concert": 2,
    }
    assert merged[1].metadata["evidence_query_hits"] == 1
    assert merged[0].metadata["original"] == "a"


def test_merge_evidence_rankings_deduplicates_repeated_query_variants() -> None:
    merged = merge_evidence_rankings(
        [
            ("same query", [_item("a", 0.9)]),
            ("same query", [_item("a", 0.9), _item("b", 0.8)]),
        ],
        limit=5,
    )

    assert [item.id for item in merged] == ["a"]
    assert merged[0].metadata["evidence_query_hits"] == 1


def test_merge_evidence_rankings_reserves_top_three_from_specialist_variant() -> None:
    shared = [_item(f"shared-{index:02d}", 0.9 - index / 1000) for index in range(60)]
    specialist = [shared[0], shared[1], _item("specialist-gold", 0.95), *shared[2:]]

    merged = merge_evidence_rankings(
        [
            ("broad one", shared),
            ("broad two", shared),
            ("specific taekwondo", specialist),
        ],
        limit=10,
    )

    assert "specialist-gold" in [item.id for item in merged]
    gold = next(item for item in merged if item.id == "specialist-gold")
    assert gold.metadata["evidence_query_ranks"]["specific taekwondo"] == 3


def test_large_evidence_set_reserves_sixth_specialist_hit() -> None:
    shared = [_item(f"shared-{index:02d}", 0.9 - index / 1000) for index in range(60)]
    specialist = [*shared[:5], _item("specialist-gold", 0.95), *shared[5:]]
    rankings = [
        *[(f"broad-{index}", shared) for index in range(6)],
        ("specific bridge evidence", specialist),
    ]

    default_merged = merge_evidence_rankings(rankings, limit=50)
    assert "specialist-gold" not in [item.id for item in default_merged]

    merged = merge_evidence_rankings(rankings, limit=50, diversity_depth=6)

    assert "specialist-gold" in [item.id for item in merged]
    gold = next(item for item in merged if item.id == "specialist-gold")
    assert gold.metadata["evidence_query_ranks"]["specific bridge evidence"] == 6


def test_merge_evidence_rankings_shared_top_hit_does_not_consume_diversity_slot() -> None:
    merged = merge_evidence_rankings(
        [
            ("main", [_item("shared", 0.95), _item("main-gold", 0.80)]),
            ("variant", [_item("shared", 0.90), _item("variant-gold", 0.70)]),
        ],
        limit=3,
    )

    assert [item.id for item in merged] == ["shared", "main-gold", "variant-gold"]


def test_recall_service_evidence_set_deduplicates_variants_and_captures_trace() -> None:
    service = RecallService(object())
    calls = []

    def fake_search(query, *, limit, recall_mode, sanitize_output=True):
        assert sanitize_output is False
        calls.append((query, limit, recall_mode))
        service.last_funnel_trace = {"query": query, "final": {"returned_count": 1}}
        return {
            "main question": [_item("a", 0.9), _item("b", 0.7)],
            "second subject": [_item("c", 0.8), _item("a", 0.6)],
        }[query]

    service._search_memories_internal = fake_search
    merged = service.search_evidence_set(
        "main question",
        query_variants=["second subject", "main question", "  "],
        limit=3,
        per_query_limit=2,
        recall_mode="advisory",
    )

    assert calls == [
        ("main question", 2, "advisory"),
        ("second subject", 2, "advisory"),
    ]
    assert [item.id for item in merged] == ["a", "c", "b"]
    assert service.last_evidence_set_trace["query_count"] == 2
    assert service.last_evidence_set_trace["returned_ids"] == ["a", "c", "b"]
    assert len(service.last_evidence_set_trace["query_traces"]) == 2
    assert "query" not in service.last_funnel_trace
    assert service.last_funnel_trace["final"]["returned_count"] == 1
    serialized_trace = json.dumps(
        service.last_evidence_set_trace,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "main question" not in serialized_trace
    assert "second subject" not in serialized_trace


def test_public_trace_setters_recursively_remove_query_and_candidate_text() -> None:
    service = RecallService(object())
    marker = "PRIVATE-QUERY-MARKER-771"

    service.last_funnel_trace = {
        "query": marker,
        "nested": {
            "content": marker,
            "summary": marker,
            "request_label": marker,
            "status": "ok",
        },
    }
    service.last_evidence_set_trace = {
        "queries": [marker],
        "query_traces": [
            {
                "raw_query": marker,
                "request_label": marker,
                "returned_ids": ["memory-1"],
            }
        ],
    }

    serialized = json.dumps(
        {
            "funnel": service.last_funnel_trace,
            "evidence": service.last_evidence_set_trace,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert marker not in serialized
    assert service.last_funnel_trace["nested"] == {"status": "ok"}
    assert service.last_evidence_set_trace["query_traces"] == [
        {"returned_ids": ["memory-1"]}
    ]


def test_recall_service_batches_query_embeddings_once_without_changing_fusion() -> None:
    class BatchProvider:
        def __init__(self) -> None:
            self.batch_calls: list[list[str]] = []

        def _embed_query_variants(self, queries: list[str]) -> list[list[float]]:
            self.batch_calls.append(list(queries))
            return [[1.0, 0.0], [0.0, 1.0]]

    provider = BatchProvider()
    service = RecallService(provider)
    search_calls: list[tuple[str, list[float] | None]] = []

    def fake_search(
        query,
        *,
        limit,
        recall_mode,
        query_vector=None,
        sanitize_output=True,
    ):
        assert sanitize_output is False
        search_calls.append((query, query_vector))
        service.last_funnel_trace = {"query": query}
        return {
            "main question": [_item("a", 0.9), _item("b", 0.7)],
            "second subject": [_item("c", 0.8), _item("a", 0.6)],
        }[query]

    service._search_memories_internal = fake_search
    merged = service.search_evidence_set(
        "main question",
        query_variants=["second subject"],
        limit=3,
        per_query_limit=2,
    )

    assert provider.batch_calls == [["main question", "second subject"]]
    assert search_calls == [
        ("main question", [1.0, 0.0]),
        ("second subject", [0.0, 1.0]),
    ]
    assert [item.id for item in merged] == ["a", "c", "b"]



def test_provider_batches_query_embeddings_through_embedder_adapter() -> None:
    class FakeEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed_queries(self, queries):
            self.calls.append(list(queries))
            return [[1.0], [2.0]]

    provider = object.__new__(ScopeRecallMemoryProvider)
    provider._vector_ready = True
    provider._embedder = FakeEmbedder()

    vectors = provider._embed_query_variants(["alpha", "beta"])

    assert vectors == [[1.0], [2.0]]
    assert provider._embedder.calls == [["alpha", "beta"]]


def test_provider_delegates_precomputed_vector_search(monkeypatch) -> None:
    calls = []

    def fake_search(provider, query_vector, *, limit):
        calls.append((provider, list(query_vector), limit))
        return [_item("vector-hit", 0.8)]

    monkeypatch.setattr(
        "scope_recall.provider.search_vector_memories_with_vector",
        fake_search,
    )
    provider = object.__new__(ScopeRecallMemoryProvider)

    results = provider._search_vector_memories_with_vector([0.2, 0.8], limit=5)

    assert [item.id for item in results] == ["vector-hit"]
    assert calls == [(provider, [0.2, 0.8], 5)]


def test_precomputed_vector_search_does_not_reembed_query() -> None:
    from scope_recall.storage_views import search_vector_memories_with_vector

    class FailingEmbedder:
        @staticmethod
        def embed_query(_query):
            raise AssertionError("precomputed vector path must not re-embed")

    class FakeVectorStore:
        def __init__(self) -> None:
            self.calls = []

        def search(self, vector, *, scope_id, limit):
            self.calls.append((list(vector), scope_id, limit))
            return []

    class FakeProvider:
        _vector_ready = True
        _embedder = FailingEmbedder()
        _vector_store = FakeVectorStore()
        _vector_config = {"top_k": 7}
        _retrieval_config = {}
        _accessible_scope_ids = ["scope-a"]

    provider = FakeProvider()
    results = search_vector_memories_with_vector(
        provider,
        [0.25, 0.75],
        limit=3,
    )

    assert results == []
    assert provider._vector_store.calls == [([0.25, 0.75], "scope-a", 7)]


def test_search_schema_exposes_bounded_query_variants() -> None:
    properties = SCOPE_RECALL_SEARCH_SCHEMA["parameters"]["properties"]
    query_variants = properties["query_variants"]

    assert properties["limit"]["maximum"] == 50
    assert query_variants["type"] == "array"
    assert query_variants["maxItems"] == 7
    assert query_variants["items"]["maxLength"] == 1000
    diversity_depth = properties["evidence_diversity_depth"]
    assert diversity_depth["minimum"] == 1
    assert diversity_depth["maximum"] == 6
    assert diversity_depth["default"] == 3


def test_search_tool_routes_variants_to_evidence_set_and_returns_trace() -> None:
    class FakeRecallService:
        last_temporal_query_diagnostics = {}
        last_funnel_trace = {}
        last_evidence_set_trace = {"strategy": "multi_query_rrf_diversity"}

        def search_evidence_set(
            self,
            query,
            *,
            query_variants,
            limit,
            per_query_limit,
            diversity_depth,
            recall_mode,
        ):
            self.call = {
                "query": query,
                "query_variants": query_variants,
                "limit": limit,
                "per_query_limit": per_query_limit,
                "diversity_depth": diversity_depth,
                "recall_mode": recall_mode,
            }
            return [_item("a", 0.9)]

    class FakeProvider:
        _retrieval_config = {"top_k": 5}
        _recall_service = FakeRecallService()

        @staticmethod
        def _normalize_query(value, limit):
            return str(value).strip()[:limit]

        @staticmethod
        def _config_value(_key, default):
            return default

    service = ScopeRecallToolService(FakeProvider())
    assert service._retrieval_limit({"limit": 50}) == 50
    payload = service._handle_search(
        {
            "query": "main question",
            "query_variants": ["second subject"],
            "evidence_diversity_depth": 6,
            "limit": 3,
            "include_trace": True,
        }
    )

    import json

    decoded = json.loads(payload)
    assert decoded["count"] == 1
    assert decoded["evidence_set_trace"]["strategy"] == "multi_query_rrf_diversity"
    assert FakeProvider._recall_service.call == {
        "query": "main question",
        "query_variants": ["second subject"],
        "limit": 3,
        "per_query_limit": 3,
        "diversity_depth": 6,
        "recall_mode": "advisory",
    }
