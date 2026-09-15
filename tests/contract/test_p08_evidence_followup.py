"""P08 evidence qualification, directed follow-up, and collection as_of contracts."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from scope_recall.contracts import ImportProvenance, import_source_fingerprint
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.events import lexical_terms
from scope_recall.core.recall_policy import (
    RecallPolicy,
    SPACE_ID,
    meaningful_query_terms,
    query_is_specific,
)
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, SearchContext, SearchLimits
from scope_recall.core.retrieval_storage import RetrievalStorage, scope_digest
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request, source_event


@dataclass
class FollowupDeadlineClock:
    now = "2026-09-06T12:00:00Z"
    _mono: float = 100.0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        return self._mono

    def expire(self) -> None:
        self._mono += 0.2


@dataclass
class RoundTrackingVectors:
    candidates: tuple[CandidateRef, ...] = ()
    calls: list[dict[str, Any]] | None = None
    clock: FollowupDeadlineClock | None = None
    expire_after_first_call: bool = False

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        self.calls.append({"query": context.query, "limit": limit, "remaining_seconds": remaining_seconds})
        if self.expire_after_first_call and len(self.calls) == 1 and self.clock is not None:
            self.clock.expire()
        return self.candidates[:limit]


@dataclass
class DirectedFollowupVectors(RoundTrackingVectors):
    h100: Any = None
    h200: Any = None

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        self.calls.append({"query": context.query, "limit": limit, "remaining_seconds": remaining_seconds})
        if len(self.calls) == 1:
            # Rejected-by-dedup work still consumes the first-round vector
            # budget; one reserved slot must remain for the directed query.
            return tuple(_vector(self.h100) for _ in range(min(limit, 47)))
        assert context.query == "h200"
        return (_vector(self.h200),)[:limit]


class LexicalBlockedStorage(RetrievalStorage):
    """Keep SQLite hydration real while forcing the missing comparison side through vector."""

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()


@dataclass
class CollectionTickClock:
    now = "2026-09-06T12:00:00Z"
    _mono: float = 100.0
    tick: float = 0.1

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        self._mono += self.tick
        return self._mono


class DeadlineAfterExactStorage(RetrievalStorage):
    def __init__(self, source, clock):
        super().__init__(clock=clock)
        self.source = source

    def exact(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return (CandidateRef("event", self.source.ref, self.source.revision, "exact_ref"),)

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        self.clock._mono += 1.0
        return ()

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        return ()


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-followup"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    return core, ctx


def _recall(core: MemoryCore, ctx, **changes):
    request = recall_request(**changes)
    return core.recall(ctx, request, deadline_seconds=changes.get("deadline_seconds", 5))


def test_comparison_single_source_explicitly_covers_both_objects(app):
    core, ctx = app
    shared = capture(core, ctx, "H100 与 H200 共用同一套部署配置。", key="TEST-p08/shared-root", when="2026-09-05T12:00:00Z")
    result = _recall(core, ctx, query="比较 H100 和 H200 部署配置", mode="history")
    assert "comparison_second_side" not in result.unmet_needs
    assert result.answerability_hint == "supported"
    assert len(result.items) == 1
    assert shared.ref in {item.ref for item in result.items}


def test_comparison_same_side_event_duplicates_stay_partial(app):
    core, ctx = app
    capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/same-side-one", when="2026-09-05T12:00:00Z")
    capture(core, ctx, "H100 仍使用第一套部署配置。", key="TEST-p08/same-side-two", when="2026-09-05T12:01:00Z")
    result = _recall(core, ctx, query="比较 H100 和 H200 部署配置", mode="history")
    assert "comparison_second_side" in result.unmet_needs
    assert result.answerability_hint == "partial"


def test_directed_followup_retrieves_second_object_and_stops_at_two_rounds(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-followup-rounds"), project_id="TEST-project", branch_id="TEST-main")
    vectors = DirectedFollowupVectors()
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=Clock(),
        vectors=vectors,
        retrieval_policy=RecallPolicy(vector_threshold=0.8),
    )
    core.recall_pipeline.storage_reader = LexicalBlockedStorage()
    core.initialize()
    vectors.h100 = capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/f1", when="2026-09-01T12:00:00Z")
    vectors.h200 = capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/f2", when="2026-09-05T12:00:00Z")
    result = core.recall(
        ctx,
        recall_request(query="比较 H100 和 H200 部署配置", mode="history"),
        deadline_seconds=5,
    )
    refs = {item.ref for item in result.items}
    assert vectors.h100.ref in refs
    assert vectors.h200.ref in refs
    assert "comparison_second_side" not in result.unmet_needs
    assert len(vectors.calls) == 2
    assert [call["limit"] for call in vectors.calls] == [47, 1]
    assert vectors.calls[0]["query"] == "比较 H100 和 H200 部署配置"
    assert vectors.calls[1]["query"] == "h200"


def _vector(source, *, score: float = 0.95) -> CandidateRef:
    return CandidateRef(
        "event",
        source.ref,
        source.revision,
        "vector",
        vector_score=score,
        vector_id=f"TEST-vector:{source.ref}@{source.revision}",
        embedding_space=SPACE_ID,
    )


def test_why_without_reason_stays_partial_but_explicit_reason_is_supported(app):
    core, ctx = app
    topic = capture(core, ctx, "TEST-project 采用 H100 方案。", key="TEST-p08/why-topic", when="2026-09-05T12:00:00Z")
    partial = _recall(core, ctx, query="为什么 TEST-project 采用 H100 方案？", mode="history")
    assert "reason_evidence" in partial.unmet_needs
    assert partial.answerability_hint == "partial"

    reason = capture(
        core,
        ctx,
        "因为成本更低，TEST-project 采用 H100 方案。",
        key="TEST-p08/why-reason",
        when="2026-09-05T12:01:00Z",
    )
    supported = _recall(core, ctx, query="为什么 TEST-project 采用 H100 方案？", mode="history")
    contents = {item.content for item in supported.items}
    assert reason.event["content"] in contents or topic.event["content"] in contents
    assert "reason_evidence" not in supported.unmet_needs
    assert supported.answerability_hint == "supported"


@pytest.mark.parametrize(
    "content, query",
    [
        ("H100更换配色的原因未知；H200因为预算不足取消。", "为什么 H100 更换配色？"),
        ("没有证据表明因为颜色而取消 H100。", "为什么 H100 被取消？"),
        ("H100更换配色的原因经过多次核对仍然未知，记录明确保留为未验证状态。", "为什么 H100 更换配色？"),
    ],
)
def test_unrelated_or_negative_reason_does_not_support_why(app, content, query):
    core, ctx = app
    capture(core, ctx, content, key="TEST-p08/why-negative", when="2026-09-05T12:00:00Z")
    result = _recall(core, ctx, query=query, mode="history")
    assert "reason_evidence" in result.unmet_needs
    assert result.answerability_hint == "partial"


def test_followup_blocked_after_initial_round_when_shared_deadline_expires(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-deadline"), project_id="TEST-project", branch_id="TEST-main")
    clock = FollowupDeadlineClock()
    vectors = RoundTrackingVectors(clock=clock, expire_after_first_call=True)
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=clock,
        vectors=vectors,
        retrieval_policy=RecallPolicy(vector_threshold=0.8),
    )
    core.recall_pipeline.storage_reader = LexicalBlockedStorage()
    core.initialize()
    h100 = capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/d1", when="2026-09-01T12:00:00Z")
    capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/d2", when="2026-09-05T12:00:00Z")
    vectors.candidates = (_vector(h100),)

    result = core.recall(
        ctx,
        recall_request(query="比较 H100 和 H200 部署配置", mode="history"),
        deadline_seconds=0.15,
    )
    assert len(vectors.calls) == 1
    assert vectors.calls[0]["query"] == "比较 H100 和 H200 部署配置"
    assert "deadline_exceeded_followup" in result.gaps
    assert "comparison_second_side" in result.unmet_needs


def test_collection_as_of_reports_complete_when_time_slice_is_fully_scanned(app):
    core, ctx = app
    old = capture(core, ctx, "旧版 H100 配置。", key="TEST-p08/asof-col", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "新版 H200 配置。", key="TEST-p08/asof-col", revision=2, when="2026-09-05T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery(
        "event",
        page_size=2,
        memory_epoch=epoch,
        scope_digest=scope_digest(ctx),
        mode="as_of",
        as_of="2026-09-03T00:00:00Z",
    )
    page = core.collection(ctx, query, deadline_seconds=5)
    assert page.coverage == "complete_for_query"
    assert len(page.items) == 1
    assert page.items[0].ref == old.ref
    assert page.items[0].content == "旧版 H100 配置。"
    assert all(item.content != "新版 H200 配置。" for item in page.items)
    assert page.next_cursor is None


def test_collection_page_size_31_uses_collection_budget(app):
    core, ctx = app
    for index in range(31):
        capture(core, ctx, f"TEST collection row {index}", key=f"TEST-p08/page31/{index:02d}", when="2026-09-05T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=31, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    page = core.collection(ctx, query, deadline_seconds=5)
    assert len(page.items) == 31
    assert page.coverage == "complete_for_query"
    assert page.next_cursor is None


def test_collection_scans_bounded_invalid_as_of_candidates_before_valid_tail(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-collection-scan"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    for index in range(3):
        capture(core, ctx, f"undated row {index}", key=f"TEST-p08/scan/a{index}", when=None)
    valid = capture(core, ctx, "historical valid tail", key="TEST-p08/scan/zvalid", when="2026-09-01T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=1, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="as_of", as_of="2026-09-02T00:00:00Z")
    page = core.collection(ctx, query, deadline_seconds=5)
    assert [item.ref for item in page.items] == [valid.ref]
    assert page.coverage == "partial"
    assert page.next_cursor is not None


def test_collection_deadline_returns_bounded_cursor_with_injected_clock(tmp_path):
    clock = CollectionTickClock()
    ctx = replace(context(tmp_path / "TEST-p08-collection-deadline"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    for index in range(8):
        capture(core, ctx, f"row {index}", key=f"TEST-p08/deadline/{index:02d}", when="2026-09-01T12:00:00Z")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=4, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    page = core.collection(ctx, query, deadline_seconds=0.5)
    assert page.coverage == "partial"
    assert page.next_cursor is not None
    assert len(page.items) < 4


def test_deadline_after_exact_still_applies_current_source_exclusion(tmp_path):
    clock = FollowupDeadlineClock()
    ctx = replace(context(tmp_path / "TEST-p08-current-exclusion"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    source = capture(core, ctx, "current source must not reappear", key="TEST-p08/current", when="2026-09-05T12:00:00Z")
    reader = DeadlineAfterExactStorage(source, clock)
    core.recall_pipeline.storage_reader = reader
    result = core.recall(
        ctx,
        recall_request(query="current source", mode="history"),
        current_source_refs=(f"{source.ref}@{source.revision}",),
        deadline_seconds=0.5,
    )
    assert not result.items
    assert "deadline_exceeded_collect" in result.gaps


def test_native_float32_boundary_is_equal_with_fixed_tolerance(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08-vector-tolerance"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(
        CoreConfig(ctx.binding),
        clock=Clock(),
        retrieval_policy=RecallPolicy(vector_threshold=0.6462987789473623),
    )
    core.initialize()
    source = capture(core, ctx, "海报保留主体高光。", key="TEST-p08/vector-tolerance", when="2026-09-05T12:00:00Z")

    class BoundaryVector:
        score = 0.6462987065315247

        def search(self, context, *, limit, remaining_seconds):
            return (_vector(source, score=self.score),)

    core.recall_pipeline.vector_port = BoundaryVector()
    result = core.recall(ctx, recall_request(query="如何吸引注意焦点", mode="history"), deadline_seconds=5)
    assert source.ref in {item.ref for item in result.items}

    BoundaryVector.score = 0.6462887789473623
    result = core.recall(ctx, recall_request(query="如何吸引注意焦点", mode="history"), deadline_seconds=5)
    assert source.ref not in {item.ref for item in result.items}


@pytest.mark.parametrize(
    "source_text, query",
    [
        ("图片 文件已上传归档。", "图片 文件 批量 改名"),
        ("录音 文件体积已记录。", "录音 文件 批量 压缩"),
    ],
)
def test_generic_short_lexical_overlap_is_not_admitted(app, source_text, query):
    core, ctx = app
    source = capture(core, ctx, source_text, key="TEST-p08/lexical-negative", when="2026-09-05T12:00:00Z")
    result = _recall(core, ctx, query=query, mode="history")
    assert source.ref not in {item.ref for item in result.items}


def test_compound_chinese_query_requires_overlap_within_one_substantive_clause(app):
    core, ctx = app
    query = "请详细说明海报主体视觉层次变化；补充边缘暗化处理依据"
    complementary = capture(
        core,
        ctx,
        "补充边缘暗化",
        key="TEST-p08/clause-aware/complementary",
        when="2026-09-05T12:01:00Z",
    )
    scattered = capture(
        core,
        replace(ctx, session_id="TEST-scattered-session"),
        "请详细补充边缘",
        key="TEST-p08/clause-aware/scattered",
        when="2026-09-05T12:02:00Z",
    )
    unrelated = capture(
        core,
        replace(ctx, session_id="TEST-unrelated-session"),
        "详细说明",
        key="TEST-p08/clause-aware/unrelated",
        when="2026-09-05T12:03:00Z",
    )
    query_terms = set(meaningful_query_terms(query))
    assert len(query_terms) == 23
    assert len(meaningful_query_terms("补充边缘暗化处理依据")) == 9
    assert len(query_terms.intersection(lexical_terms(complementary.event["content"]))) == 5
    assert len(query_terms.intersection(lexical_terms(scattered.event["content"]))) == 5
    assert len(query_terms.intersection(lexical_terms(unrelated.event["content"]))) == 3

    result = _recall(core, ctx, query=query, mode="history")
    refs = {item.ref for item in result.items}
    assert complementary.ref in refs
    assert scattered.ref not in refs
    assert unrelated.ref not in refs


def test_chinese_clause_fallback_requires_two_substantive_cjk_clauses():
    query = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet；"
        "补充边缘暗化处理依据"
    )
    matches = lexical_terms("补充边缘暗化")
    assert len(set(meaningful_query_terms(query))) == 19
    assert len(set(meaningful_query_terms(query)).intersection(matches)) == 5
    assert not query_is_specific(
        query,
        matched_terms=5.0,
        matched_query_terms=matches,
    )


def test_lexical_prelimit_keeps_direct_and_imported_evidence_ahead_of_diagnostic_envelopes(app):
    core, ctx = app
    direct = capture(
        core,
        ctx,
        "alpha bravo",
        key="TEST-p08/lexical-priority/direct",
        when="2026-09-05T12:01:00Z",
    )
    imported_event = source_event(
        source_event_key="TEST-p08/lexical-priority/imported",
        origin="imported",
        source_original_origin="human_direct",
        role="user",
        content="alpha bravo",
        occurred_at="2026-09-05T12:02:00Z",
    )
    provenance = ImportProvenance(
        "human_direct",
        "a" * 64,
        frozenset({import_source_fingerprint(imported_event)}),
    )
    imported_receipt = core.record_event(
        replace(ctx, actor_origin="imported", import_provenance=provenance),
        imported_event,
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    imported = core.source(ctx, imported_receipt.event_refs[0].ref, 1)

    diagnostics = []
    for index in range(3):
        receipt = core.record_event(
            replace(ctx, actor_origin="memory_reinjection"),
            source_event(
                source_event_key=f"TEST-p08/lexical-priority/diagnostic-{index}",
                origin="memory_reinjection",
                role="tool",
                content="alpha bravo charlie delta",
                occurred_at=f"2026-09-05T12:0{3 + index}:00Z",
            ),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        diagnostics.append(core.source(ctx, receipt.event_refs[0].ref, 1))

    search = SearchContext(
        query="alpha bravo charlie delta",
        mode="history",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(candidate_pool=3),
        deadline=core.clock.monotonic() + 5.0,
        now=core.clock.utc_now(),
        trusted_context=ctx,
    )
    with core.storage.read(ctx) as tx:
        candidates = RetrievalStorage().lexical(tx, search, limit=3)

    assert [candidate.ref for candidate in candidates] == [
        imported.ref,
        direct.ref,
        diagnostics[-1].ref,
    ]


def test_lexical_specificity_accepts_full_multi_term_match_and_one_term_keyword(app):
    core, ctx = app
    full = capture(core, ctx, "图片 文件 批量 改名完成。", key="TEST-p08/lexical-positive", when="2026-09-05T12:00:00Z")
    keyword = capture(core, ctx, "蓝色。", key="TEST-p08/lexical-keyword", when="2026-09-05T12:01:00Z")
    detailed = _recall(core, ctx, query="图片 文件 批量 改名", mode="history")
    assert full.ref in {item.ref for item in detailed.items}
    short = _recall(core, ctx, query="蓝色", mode="history")
    assert keyword.ref in {item.ref for item in short.items}
