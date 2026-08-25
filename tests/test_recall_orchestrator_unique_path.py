"""Contract tests: production recall has one orchestrator and no dual path."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

from scope_recall._internal.recall import orchestrator as orchestrator_module
from scope_recall._internal.recall.orchestrator import (
    REQUIRED_FILTER_KEYS,
    REQUIRED_TRACE_STAGES,
    run_search,
)
from scope_recall._internal.recall.request import RecallSearchRequest
from scope_recall._internal.recall.tuning import (
    DEFAULT_MIN_SCORE,
    FRESHNESS_HINTS,
    INTENT_UNMATCHED_BM25_FACTOR,
)
from scope_recall.memory_queries import context_payload
from scope_recall.models import RecallItem
from scope_recall.prompting import render_current_turn_recall
from scope_recall.recall import RecallService
from scope_recall.tooling import ScopeRecallToolService

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ORCH_MARKERS = (
    "_search_db_memories",
    "_search_vector_memories",
    "merge_recall_candidates",
    "temporal_stale_removed",
)


class _DummyProvider:
    def __init__(self) -> None:
        self._retrieval_config = {"mode": "lexical", "min_score": 0.01}
        self._vector_config = {}
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._config = {"auto_recall": True, "query_char_limit": 1000}
        self._scope = SimpleNamespace(agent_context="primary")
        self._last_recall_turns: dict[str, int] = {}
        self._current_turn = 10
        self.db_calls = 0
        self.vector_calls = 0
        self.db_limits: list[int] = []
        self.vector_limits: list[int] = []

    def _search_db_memories(self, query, *, limit):
        self.db_calls += 1
        self.db_limits.append(limit)
        return [
            RecallItem(
                id="memory-1",
                content="Deploy command is uv run app.",
                summary="Deploy command is uv run app.",
                source="tool-store",
                target="memory",
                score=0.8,
                updated_at="2026-05-01T00:00:00+00:00",
                metadata={
                    "lexical_score": 0.8,
                    "vector_score": 0.0,
                    "scope_id": self._shared_scope_id,
                },
            )
        ][:limit]

    def _search_vector_memories(self, query, *, limit):
        self.vector_calls += 1
        self.vector_limits.append(limit)
        return []

    def _search_vector_memories_with_vector(self, query_vector, *, limit):
        self.vector_calls += 1
        self.vector_limits.append(limit)
        return []

    def _search_curated_memories(self, query):
        return []

    def recall_service_view(self):
        return getattr(self, "_recall_service", None)

    def retrieval_status_view(self):
        return {
            "config": dict(self._retrieval_config),
            "mode": str(self._retrieval_config.get("mode") or "lexical"),
            "lexical_weight": 1.0,
            "vector_weight": 0.0,
        }

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default

    def _normalize_query(self, query: str, limit: int) -> str:
        return query[:limit]

    def _retrieve_limit(self) -> int:
        return 5

    def _mark_recalled(self, memory_ids: list[str]) -> None:
        return None


def _item(memory_id: str = "safe-1") -> RecallItem:
    return RecallItem(
        id=memory_id,
        content="secret-looking token",
        summary="secret-looking token",
        source="tool-store",
        target="memory",
        score=0.9,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.9, "vector_score": 0.0, "original": "keep"},
    )


def _production_python_files() -> list[Path]:
    files = []
    for path in PLUGIN_ROOT.rglob("*.py"):
        rel = path.relative_to(PLUGIN_ROOT)
        if any(part in {"tests", "__pycache__", "build", "dist"} for part in rel.parts):
            continue
        files.append(path)
    return files


def _orchestration_functions(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        chunk = ast.get_source_segment(source, node) or ""
        if all(marker in chunk for marker in ORCH_MARKERS):
            found.append(node.name)
    return found


def test_production_tree_has_one_search_orchestrator() -> None:
    hits: list[tuple[str, str]] = []
    for path in _production_python_files():
        source = path.read_text(encoding="utf-8")
        for name in _orchestration_functions(source):
            hits.append((path.relative_to(PLUGIN_ROOT).as_posix(), name))
    assert hits == [("_internal/recall/orchestrator.py", "run_search")]


def test_recall_facade_no_longer_contains_search_collection() -> None:
    source = (PLUGIN_ROOT / "recall.py").read_text(encoding="utf-8")
    assert "_search_db_memories" not in source
    assert "_search_vector_memories" not in source
    assert "merge_recall_candidates" not in source
    assert "_recall_orchestrator.run_search" in source
    tree = ast.parse(source)
    internal = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RecallService"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "_search_memories_internal"
        for node in [item]
    )
    text = ast.get_source_segment(source, internal) or ""
    assert text.count("return") == 1
    assert "run_search" in text
    assert "lexical" not in text


def test_search_memories_cannot_bypass_sanitized_egress() -> None:
    parameters = inspect.signature(RecallService.search_memories).parameters
    assert "sanitize_output" not in parameters
    assert "query_vector" not in parameters


def test_run_search_is_the_unique_caller_entry(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_run_search(host, request):
        calls.append((request.query, request.limit))
        host.last_funnel_trace = {"query": request.query, "filters": {}, "stages": {}}
        host.last_rejected_candidates = []
        return []

    monkeypatch.setattr(orchestrator_module, "run_search", fake_run_search)

    provider = _DummyProvider()
    provider._recall_service = RecallService(provider)

    provider._recall_service.search_memories("alpha query", limit=3)
    context_payload(provider, query="memory-queries query", limit=2)
    render_current_turn_recall(provider, "Please recall the deploy command now.")
    ScopeRecallToolService(provider)._handle_search({"query": "tooling query", "limit": 4})

    spec = importlib.util.spec_from_file_location(
        "scope_recall_graph_benchmark_runtime_contract",
        PLUGIN_ROOT / "scripts" / "benchmark.graph_relations.py",
    )
    assert spec is not None and spec.loader is not None
    graph_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(graph_mod)
    alt_orch = sys.modules.get(
        "scope_recall_graph_benchmark_runtime._internal.recall.orchestrator"
    )
    if alt_orch is not None:
        monkeypatch.setattr(alt_orch, "run_search", fake_run_search)
    graph_mod._run_search({"mode": "lexical", "min_score": 0.01}, [], [])

    queries = [item[0] for item in calls]
    assert any(query == "alpha query" for query in queries)
    assert any(query == "memory-queries query" for query in queries)
    assert any("deploy command" in query for query in queries)
    assert any(query == "tooling query" for query in queries)
    assert any(query == "Project Atlas deploy command" for query in queries)
    assert provider.db_calls == 0
    assert provider.vector_calls == 0
    tooling_src = (PLUGIN_ROOT / "reflection_tooling.py").read_text(encoding="utf-8")
    assert "build_reflection_evidence_pack(" in tooling_src
    assert "search_memories(" not in tooling_src


def test_reflection_tooling_calls_search_through_run_search(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_search(host, request):
        calls.append(request.query)
        host.last_funnel_trace = {}
        host.last_rejected_candidates = []
        return []

    monkeypatch.setattr(orchestrator_module, "run_search", fake_run_search)

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Conn:
        total_changes = 0

        def execute(self, *_a, **_k):
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    from scope_recall.reflection import build_reflection_evidence_pack

    provider = SimpleNamespace(
        _config={"reflection": {"enabled": True}},
        _lock=_Lock(),
        _require_conn=lambda: _Conn(),
        _accessible_scope_ids=["local-scope"],
        _scope_id="local-scope",
        _retrieval_config={"mode": "lexical", "min_score": 0.01},
        _vector_config={},
    )
    pack = build_reflection_evidence_pack(provider, query="reflection unique path")
    assert calls == ["reflection unique path"]
    assert list(pack.evidence) == []
    source = (PLUGIN_ROOT / "reflection_tooling.py").read_text(encoding="utf-8")
    assert "build_reflection_evidence_pack(" in source
    assert "search_memories(" not in source


def test_patched_run_search_prevents_dual_provider_collection(monkeypatch) -> None:
    provider = _DummyProvider()
    service = RecallService(provider)

    def fake_run_search(host, request):
        host.last_funnel_trace = {"stages": {}, "filters": {}}
        host.last_rejected_candidates = []
        return []

    monkeypatch.setattr(orchestrator_module, "run_search", fake_run_search)
    assert service.search_memories("no dual", limit=3) == []
    assert provider.db_calls == 0
    assert provider.vector_calls == 0


def test_unpatched_search_collects_each_source_once() -> None:
    provider = _DummyProvider()
    service = RecallService(provider)
    results = service.search_memories("Deploy command is uv run app.", limit=5)
    assert [item.id for item in results] == ["memory-1"]
    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    trace = service.last_funnel_trace
    always_present = set(REQUIRED_FILTER_KEYS) - {"temporal_stale_removed"}
    assert always_present <= set(trace["filters"])
    assert "temporal_stale_removed" in (PLUGIN_ROOT / "_internal" / "recall" / "orchestrator.py").read_text(encoding="utf-8")
    for stage in ("lexical", "vector", "curated", "rrf", "merge", "graph", "fact_freshness", "ranked"):
        assert stage in trace["stages"]
    assert trace["recall_mode"] == "advisory"


def test_vector_top_k_controls_vector_depth_without_expanding_final_limit() -> None:
    provider = _DummyProvider()
    provider._retrieval_config["candidate_pool"] = 3
    provider._vector_config["top_k"] = 11
    service = RecallService(provider)

    results = service.search_memories("Deploy command is uv run app.", limit=1)

    assert len(results) == 1
    assert provider.db_limits == [3]
    assert provider.vector_limits == [11]
    assert service.last_funnel_trace["vector_top_k"] == 11


def test_monkeypatch_safe_item_still_binds_orchestrator(monkeypatch) -> None:
    import scope_recall.recall as recall_module

    seen: list[str] = []

    def fake_safe(item):
        seen.append(item.id)
        return item

    monkeypatch.setattr(recall_module, "_safe_recall_item", fake_safe)
    provider = _DummyProvider()
    provider._search_db_memories = lambda query, *, limit: []  # type: ignore[method-assign]
    service = RecallService(provider)
    # Force a rejected row through the orchestrator sanitize path.
    low = RecallItem(
        id="below",
        content="unrelated chatter",
        summary="unrelated chatter",
        source="tool-store",
        target="memory",
        score=0.0,
        updated_at="2026-05-01T00:00:00+00:00",
        metadata={"lexical_score": 0.01, "vector_score": 0.0},
    )

    def fake_db(query, *, limit):
        return [low]

    provider._search_db_memories = fake_db  # type: ignore[method-assign]
    service.search_memories("zzzz", limit=5)
    assert "below" in seen
    assert service.last_rejected_candidates[0].id == "below"


def test_monkeypatch_service_lifecycle_filter_still_binds() -> None:
    provider = _DummyProvider()
    service = RecallService(provider)
    calls = {"n": 0}

    def hide_all(items):
        calls["n"] += 1
        return []

    service._filter_recall_lifecycle = hide_all  # type: ignore[method-assign]
    assert service.search_memories("Deploy command is uv run app.", limit=5) == []
    assert calls["n"] >= 1


def test_tuning_is_the_unique_constant_owner() -> None:
    recall_source = (PLUGIN_ROOT / "recall.py").read_text(encoding="utf-8")
    orch_source = (PLUGIN_ROOT / "_internal" / "recall" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    assert "FRESHNESS_HINTS" in recall_source
    assert "_FRESHNESS_HINTS = {" not in recall_source
    assert "INTENT_UNMATCHED_BM25_FACTOR" in orch_source
    assert "raw_bm25_score * 0.25" not in orch_source
    assert DEFAULT_MIN_SCORE == 0.18
    assert "当前" in FRESHNESS_HINTS
    assert INTENT_UNMATCHED_BM25_FACTOR == 0.25


def test_required_diagnostics_remain_in_orchestrator_source() -> None:
    source = (PLUGIN_ROOT / "_internal" / "recall" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    for key in REQUIRED_FILTER_KEYS:
        assert key in source
    for stage in REQUIRED_TRACE_STAGES:
        assert stage in source
    for token in (
        "bm25_score",
        "_search_vector_memories",
        "_entity_graph_scores",
        "_temporal_current_candidates",
        "_fact_freshness_evidence",
        "build_reflection" not in source and "fact_freshness",
        "_explicit_query_scope_entities" not in source and "entity_scope_mismatch",
    ):
        if isinstance(token, bool):
            assert token
        else:
            assert token in source


def test_run_search_request_object_is_real() -> None:
    request = RecallSearchRequest(query="q", limit=2, recall_mode="strict")
    assert request.sanitize_output is True
    assert request.query_vector is None
    assert run_search is orchestrator_module.run_search
