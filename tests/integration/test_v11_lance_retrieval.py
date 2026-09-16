"""P08 integration against the real LanceDB helper and SQLite truth."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorPort, LanceVectorRecord, physical_partition_scope_id
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID
from scope_recall.vector.process_store import ProcessLanceVectorStore
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None,
    reason="P08 native integration requires the isolated LanceDB environment",
)


class SyntheticQueryEmbedding:
    """Marked TEST-only embedding port; no network or model API is used."""

    def embed_query(self, text: str, *, remaining_seconds: float):
        assert text
        assert remaining_seconds > 0
        return (1.0, 0.0)


class SearchCallRecorder:
    def __init__(self, store: ProcessLanceVectorStore) -> None:
        self._store = store
        self.calls: list[dict[str, Any]] = []

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"scope_id": scope_id, "limit": limit, "vector": list(vector)})
        return self._store.search(vector, scope_id=scope_id, limit=limit)


@pytest.fixture
def native_app(tmp_path: Path):
    ctx = replace(context(tmp_path / "TEST-P08-native"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    store = ProcessLanceVectorStore(tmp_path / "TEST-lance", table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        yield core, ctx, store
    finally:
        store.close()


def _record(source, *, vector_id: str, space: str = SPACE_ID, project: str | None = "TEST-project", branch: str | None = "TEST-main", revision: int | None = None, embedding: tuple[float, ...] = (1.0, 0.0)):
    return LanceVectorRecord(
        object_kind="event",
        object_ref=source.ref,
        object_revision=source.revision if revision is None else revision,
        vector_id=vector_id,
        embedding_space=space,
        embedding=embedding,
        scope_id=source.scope_id,
        agent_id="TEST-agent",
        installation_id="TEST-installation",
        project_id=project,
        branch_id=branch,
    )


def _expected_partitions(ctx, *, logical_scope_id: str, embedding_space: str = SPACE_ID):
    combos = (
        ("TEST-project", "TEST-main"),
        ("TEST-project", None),
        (None, "TEST-main"),
        (None, None),
    )
    return {
        physical_partition_scope_id(
            agent_id=ctx.binding.agent_id,
            installation_id=ctx.binding.installation_id,
            embedding_space=embedding_space,
            logical_scope_id=logical_scope_id,
            project_id=project_id,
            branch_id=branch_id,
        )
        for project_id, branch_id in combos
    }


def test_construction_is_lazy_and_native_query_returns_metadata_only(tmp_path):
    store = ProcessLanceVectorStore(tmp_path / "not-created", table_name="TEST_vectors", dimensions=2)
    port = LanceVectorPort(store, SyntheticQueryEmbedding())
    assert not (tmp_path / "not-created").exists()
    assert port is not None


def test_native_scope_and_metadata_boundary_hydrates_only_sqlite_truth(native_app):
    core, ctx, store = native_app
    source = capture(core, ctx, "海上晨雾项目采用暮光方案。", key="TEST-P08/native", revision=1)
    newer = capture(core, ctx, "海上晨雾项目采用晨星方案。", key="TEST-P08/native", revision=2, when="2026-09-05T12:00:00Z")
    writer = LanceIndexWriter(store)
    writer.upsert_records(
        (
            _record(source, vector_id="v-stale"),
            _record(newer, vector_id="v-good"),
            _record(newer, vector_id="v-old-space", space="wrong-space"),
            _record(newer, vector_id="v-cross-project", project="OTHER-project"),
        )
    )
    before = core.status(ctx)
    recorder = SearchCallRecorder(store)
    port = LanceVectorPort(recorder, SyntheticQueryEmbedding())
    search_ctx = _search_context(ctx, query="完全没有共同词的语义问题")
    port_candidates = port.search(search_ctx, limit=10, remaining_seconds=5)
    expected_partitions = _expected_partitions(ctx, logical_scope_id=source.scope_id)
    assert {call["scope_id"] for call in recorder.calls} == expected_partitions
    assert all(call["scope_id"].startswith("p08-v1-") for call in recorder.calls)
    assert {item.vector_id for item in port_candidates} == {"v-stale", "v-good"}
    assert "v-old-space" not in {item.vector_id for item in port_candidates}
    assert all(item.embedding_space == SPACE_ID for item in port_candidates)
    assert all(not hasattr(item, "content") for item in port_candidates)

    core.recall_pipeline.vector_port = port
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8)
    result = core.recall(ctx, recall_request(query="完全没有共同词的语义问题", mode="current"), deadline_seconds=5)
    assert [item.content for item in result.items] == [newer.event["content"]]
    assert result.items[0].revision == 2
    assert "vector_old_or_mismatched_space" not in result.gaps
    after = core.status(ctx)
    assert after.memory_epoch == before.memory_epoch
    assert after.pending_work == before.pending_work


def test_native_partition_prefilter_avoids_limit_starvation(native_app):
    core, ctx, store = native_app
    source = capture(core, ctx, "松林团队采用霞光协议。", key="TEST-P08/starvation", revision=1)
    writer = LanceIndexWriter(store)
    writer.upsert_records(
        (
            _record(source, vector_id="v-star-cross-project", project="OTHER-project"),
            _record(source, vector_id="v-star-old-space", space="wrong-space"),
            _record(source, vector_id="v-star-valid", embedding=(0.8, 0.6)),
        )
    )
    raw_logical_hits = store.search([1.0, 0.0], scope_id=source.scope_id, limit=2)
    assert raw_logical_hits == []

    recorder = SearchCallRecorder(store)
    port = LanceVectorPort(recorder, SyntheticQueryEmbedding())
    valid_partition = physical_partition_scope_id(
        agent_id="TEST-agent",
        installation_id="TEST-installation",
        embedding_space=SPACE_ID,
        logical_scope_id=source.scope_id,
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    search_ctx = _search_context(ctx, query="完全没有共同词的语义问题")
    port_candidates = port.search(search_ctx, limit=2, remaining_seconds=5)
    assert valid_partition in {call["scope_id"] for call in recorder.calls}
    assert {item.vector_id for item in port_candidates} == {"v-star-valid"}

    core.recall_pipeline.vector_port = port
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.5)
    before = core.status(ctx)
    bounded_ctx = replace(
        search_ctx,
        limits=replace(search_ctx.limits, vector_limit=2, relation_hops=0, relation_objects=0),
    )
    pipeline_result = core.recall_pipeline.search(bounded_ctx)
    after = core.status(ctx)
    assert [candidate.vector_id for candidate in pipeline_result.candidates] == ["v-star-valid"]
    assert [item.content for item in pipeline_result.items] == [source.event["content"]]
    assert after.memory_epoch == before.memory_epoch
    assert after.pending_work == before.pending_work


def test_native_global_partition_does_not_expand_permissions(native_app):
    core, ctx, store = native_app
    allowed = capture(core, ctx, "全局白名单条目。", key="TEST-P08/global", revision=1)
    other_project = capture(core, ctx, "其他项目条目。", key="TEST-P08/other", revision=1)
    writer = LanceIndexWriter(store)
    writer.upsert_records(
        (
            _record(allowed, vector_id="v-global", project=None, branch=None),
            _record(other_project, vector_id="v-other-project", project="OTHER-project"),
            _record(other_project, vector_id="v-other-branch", project="TEST-project", branch="OTHER-branch"),
        )
    )
    port = LanceVectorPort(store, SyntheticQueryEmbedding())
    search_ctx = _search_context(ctx, query="完全没有共同词的语义问题")
    candidates = port.search(search_ctx, limit=10, remaining_seconds=5)
    assert {item.vector_id for item in candidates} == {"v-global"}
    candidate = candidates[0]
    assert candidate.ref == allowed.ref
    assert candidate.revision == 1
    assert candidate.embedding_space == SPACE_ID

    core.recall_pipeline.vector_port = port
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.5)
    result = core.recall_pipeline.search(
        replace(
            search_ctx,
            limits=replace(search_ctx.limits, relation_hops=0, relation_objects=0),
        )
    )
    assert [item.ref for item in result.items] == [allowed.ref]
    assert all(item.ref != other_project.ref for item in result.items)


def _search_context(ctx, *, query: str):
    from scope_recall.core.retrieval import SearchContext

    return SearchContext.from_request(
        recall_request(query=query, mode="current"),
        ctx,
        now=Clock.now,
        deadline=Clock().monotonic() + 5,
    )
