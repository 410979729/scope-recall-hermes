"""Contract tests: production recall has one orchestrator and no dual path."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sqlite3
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
import scope_recall.prompting as prompting_module
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

    def recall_limit(self) -> int:
        return self._retrieve_limit()

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
        if any(part.startswith(".") for part in rel.parts):
            continue
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
    assert any(
        str(key).startswith("recall_packet_")
        for key in (results[0].metadata or {})
    )
    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    trace = service.last_funnel_trace
    always_present = set(REQUIRED_FILTER_KEYS) - {"temporal_stale_removed"}
    assert always_present <= set(trace["filters"])
    assert "temporal_stale_removed" in (PLUGIN_ROOT / "_internal" / "recall" / "orchestrator.py").read_text(encoding="utf-8")
    for stage in ("lexical", "vector", "curated", "rrf", "merge", "graph", "fact_freshness", "ranked"):
        assert stage in trace["stages"]
    compiler_trace = trace["stages"]["compiler"]
    assert compiler_trace["same_candidate_set"] is True
    assert compiler_trace["renderer_enabled"] is True
    assert "memory-1" not in str(compiler_trace)
    assert trace["recall_mode"] == "advisory"


def test_compiler_flags_do_not_trigger_a_second_retrieval() -> None:
    provider = _DummyProvider()
    provider._config["recall_compiler"] = {
        "current_truth_enabled": True,
        "conflict_enabled": True,
        "budgeter_enabled": True,
        "renderer_enabled": True,
        "token_budget": 160,
        "per_item_token_budget": 40,
    }
    service = RecallService(provider)

    results = service.search_memories("Deploy command is uv run app.", limit=5)

    assert [item.id for item in results] == ["memory-1"]
    assert any(
        str(key).startswith("recall_packet_")
        for key in (results[0].metadata or {})
    )
    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    compiler_trace = service.last_funnel_trace["stages"]["compiler"]
    assert compiler_trace["current_truth_enabled"] is True
    assert compiler_trace["conflict_enabled"] is True
    assert compiler_trace["budgeter_enabled"] is True
    assert compiler_trace["renderer_enabled"] is True


def test_renderer_fallback_does_not_disable_conflict_or_budget_stages() -> None:
    provider = _DummyProvider()
    provider._config["recall_compiler"] = {
        "current_truth_enabled": False,
        "conflict_enabled": True,
        "budgeter_enabled": True,
        "renderer_enabled": False,
        "token_budget": 320,
        "per_item_token_budget": 96,
    }
    shared = {"relation_evidence_types": ["contradicts"]}
    left = RecallItem(
        id="left",
        content="left conflict evidence",
        summary="left conflict evidence",
        source="tool-store",
        target="memory",
        score=0.9,
        updated_at="2026-08-01T00:00:00+00:00",
        metadata={
            **shared,
            "lexical_score": 0.9,
            "relation_contradiction_ids": ["right"],
        },
    )
    right = RecallItem(
        id="right",
        content="right conflict evidence",
        summary="right conflict evidence",
        source="tool-store",
        target="memory",
        score=0.8,
        updated_at="2026-08-01T00:00:00+00:00",
        metadata={
            **shared,
            "lexical_score": 0.8,
            "relation_contradiction_ids": ["left"],
        },
    )

    def collect_conflict(_query, *, limit):
        provider.db_calls += 1
        return [left, right][:limit]

    provider._search_db_memories = collect_conflict  # type: ignore[method-assign]
    provider._recall_service = RecallService(provider)
    provider._recall_service._persisted_relation_evidence = (  # type: ignore[method-assign]
        lambda _ids: {
            "left": {
                "count": 1,
                "types": ["contradicts"],
                "outgoing": {"contradicts": [{"id": "right", "confidence": 0.9}]},
            },
            "right": {
                "count": 1,
                "types": ["contradicts"],
                "outgoing": {"contradicts": [{"id": "left", "confidence": 0.9}]},
            },
        }
    )

    rendered = render_current_turn_recall(
        provider, "Please recall the conflicting evidence now."
    )

    packet = provider._recall_service.last_recall_packet
    compiler_trace = provider._recall_service.last_funnel_trace["stages"]["compiler"]
    assert rendered.startswith("## Scope Recall Relevant Memories\n")
    assert packet.conflict_count == 2
    assert all(item.conflict for item in packet.items)
    assert compiler_trace["conflict_enabled"] is True
    assert compiler_trace["budgeter_enabled"] is True
    assert compiler_trace["renderer_enabled"] is False


def test_prompt_renderer_consumes_or_derives_from_orchestrator_packet(
    monkeypatch,
) -> None:
    provider = _DummyProvider()
    provider._recall_service = RecallService(provider)
    captured: dict[str, object] = {}
    original_derive = prompting_module.derive_recall_packet

    def capture_derive(parent_packet, selected_items):
        selected = list(selected_items)
        derived = original_derive(parent_packet, selected)
        captured.update(
            {
                "parent": parent_packet,
                "selected_ids": [item.id for item in selected],
                "derived": derived,
            }
        )
        return derived

    monkeypatch.setattr(prompting_module, "derive_recall_packet", capture_derive)

    rendered = render_current_turn_recall(
        provider, "Please recall the deploy command from durable memory."
    )

    parent = captured["parent"]
    derived = captured["derived"]
    assert parent is provider._recall_service.last_recall_packet
    assert derived.parent_candidate_fingerprint == parent.candidate_fingerprint
    assert derived.candidate_fingerprint == parent.candidate_fingerprint
    assert captured["selected_ids"] == [item.item.id for item in derived.items]
    assert rendered.startswith("## Scope Recall Packet\n")


def test_no_second_retrieval_in_prompt_rendering() -> None:
    provider = _DummyProvider()
    provider._recall_service = RecallService(provider)

    render_current_turn_recall(
        provider, "Please recall the deploy command from durable memory."
    )

    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    source = (PLUGIN_ROOT / "prompting.py").read_text(encoding="utf-8")
    assert source.count("search_memories(") == 1
    assert "CandidateSet.from_items" not in source
    assert "compile_recall_packet" not in source


def test_query_path_remains_truth_zero_write() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    conn.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    conn.commit()
    provider = _DummyProvider()
    original_search = provider._search_db_memories

    def query_truth_once(_query, *, limit):
        assert conn.execute("SELECT value FROM sentinel").fetchone() == ("unchanged",)
        return original_search(_query, limit=limit)

    provider._search_db_memories = query_truth_once  # type: ignore[method-assign]
    before_changes = conn.total_changes
    before_dump = tuple(conn.iterdump())

    RecallService(provider).search_memories("Deploy command is uv run app.", limit=5)

    assert conn.total_changes == before_changes
    assert tuple(conn.iterdump()) == before_dump
    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    conn.close()


def test_current_truth_default_removes_stale_claim_projection_without_query_write() -> None:
    provider = _DummyProvider()
    shared = {
        "scope_id": provider._shared_scope_id,
        "fact_claim_key": "fact:joy-city",
    }
    stale = RecallItem(
        id="city-old",
        content="Joy lives in Mumbai.",
        summary="Joy lives in Mumbai.",
        source="tool-store",
        target="memory",
        score=0.95,
        updated_at="2026-01-01T00:00:00+00:00",
        metadata={**shared, "fact_claim_id": "claim-old", "lexical_score": 0.95},
    )
    current = RecallItem(
        id="city-current",
        content="Joy lives in Tokyo.",
        summary="Joy lives in Tokyo.",
        source="tool-store",
        target="memory",
        score=0.75,
        updated_at="2026-08-01T00:00:00+00:00",
        metadata={**shared, "fact_claim_id": "claim-current", "lexical_score": 0.75},
    )
    def collect_once(_query, *, limit):
        provider.db_calls += 1
        return [stale, current][:limit]

    provider._search_db_memories = collect_once  # type: ignore[method-assign]
    service = RecallService(provider)
    service._fact_freshness_evidence = lambda _ids: {  # type: ignore[method-assign]
        "city-old": {"status": "stale", "fact_key": "legacy-city", "truth_type": "factual"},
        "city-current": {"status": "current", "fact_key": "legacy-city", "truth_type": "factual"},
    }

    results = service.search_memories("Where does Joy live now?", limit=5)

    assert [item.id for item in results] == ["city-current"]
    assert provider.db_calls == 1
    assert provider.vector_calls == 1
    compiler_trace = service.last_funnel_trace["stages"]["compiler"]
    assert compiler_trace["current_truth_removed"] == 1
    assert compiler_trace["active_current_truth_removed"] == 1
    assert any(
        str(key).startswith("recall_packet_")
        for key in (results[0].metadata or {})
    )


def test_current_truth_default_has_an_explicit_v1_rollback_switch() -> None:
    provider = _DummyProvider()
    provider._config["recall_compiler"] = {"current_truth_enabled": False}
    shared = {
        "scope_id": provider._shared_scope_id,
        "fact_claim_key": "fact:joy-city",
    }
    stale = RecallItem(
        id="city-old",
        content="Joy lives in Mumbai.",
        summary="Joy lives in Mumbai.",
        source="tool-store",
        target="memory",
        score=0.95,
        updated_at="2026-01-01T00:00:00+00:00",
        metadata={**shared, "fact_claim_id": "claim-old", "lexical_score": 0.95},
    )
    current = RecallItem(
        id="city-current",
        content="Joy lives in Tokyo.",
        summary="Joy lives in Tokyo.",
        source="tool-store",
        target="memory",
        score=0.75,
        updated_at="2026-08-01T00:00:00+00:00",
        metadata={**shared, "fact_claim_id": "claim-current", "lexical_score": 0.75},
    )

    def collect_once(_query, *, limit):
        provider.db_calls += 1
        return [stale, current][:limit]

    provider._search_db_memories = collect_once  # type: ignore[method-assign]
    service = RecallService(provider)
    service._fact_freshness_evidence = lambda _ids: {  # type: ignore[method-assign]
        "city-old": {"status": "stale", "fact_key": "legacy-city", "truth_type": "factual"},
        "city-current": {"status": "current", "fact_key": "legacy-city", "truth_type": "factual"},
    }

    results = service.search_memories("Where does Joy live now?", limit=5)

    assert {item.id for item in results} == {"city-old", "city-current"}
    assert len(results) == 2
    assert service.last_funnel_trace["stages"]["compiler"]["current_truth_enabled"] is False
    assert service.last_funnel_trace["stages"]["compiler"]["current_truth_removed"] == 1
    assert service.last_funnel_trace["stages"]["compiler"]["active_current_truth_removed"] == 0


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
