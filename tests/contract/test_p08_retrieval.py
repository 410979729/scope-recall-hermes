"""Deterministic P08 core retrieval contracts using synthetic TEST identity."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, PageCursor, SearchContext
from scope_recall.core.retrieval_storage import scope_digest
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p08"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    return core, ctx


def test_search_context_copies_request_and_trusted_runtime(app):
    core, ctx = app
    request = recall_request(query="TEST query", request_id="TEST-r1")
    search = SearchContext.from_request(request, ctx, now=Clock.now, deadline=999.0)
    request["query"] = "mutated"
    request["focus_refs"] = ["untrusted"]
    assert search.query == "TEST query"
    assert search.focus_refs == ()
    assert search.request_id == "TEST-r1"
    with pytest.raises(FrozenInstanceError):
        search.query = "changed"


def test_lexical_recall_is_read_only_and_returns_source_evidence(app):
    core, ctx = app
    source = capture(core, ctx, "P08 keeps H100 as an exact identifier.", key="TEST-p08/lexical")
    before = core.status(ctx)
    result = core.recall(ctx, recall_request(query="H100 exact identifier", mode="current"), deadline_seconds=5)
    after = core.status(ctx)
    assert [item.ref for item in result.items] == [source.ref]
    assert result.items[0].evidence_refs == (f"{source.ref}@{source.revision}",)
    assert after.memory_epoch == before.memory_epoch
    assert after.pending_work == before.pending_work


def test_semantic_candidate_can_admit_without_lexical_overlap(app):
    core, ctx = app
    source = capture(core, ctx, "海上晨雾项目采用暮光方案。", key="TEST-p08/vector")
    ref = source.ref

    class Vector:
        def search(self, context, *, limit, remaining_seconds):
            return (CandidateRef("event", ref, 1, "vector", vector_id="v1", embedding_space=SPACE_ID, vector_score=0.91),)

    core.recall_pipeline.vector_port = Vector()
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8)
    result = core.recall(ctx, recall_request(query="完全没有共同词的语义问题", mode="current"), deadline_seconds=5)
    assert ref in {item.ref for item in result.items}
    assert next(item for item in result.items if item.ref == ref).content == "海上晨雾项目采用暮光方案。"


def test_hard_identifiers_remain_distinct_but_comparison_keeps_both(app):
    core, ctx = app
    first = capture(core, ctx, "H100 使用第一套部署配置。", key="TEST-p08/hard", revision=1, when="2026-09-01T12:00:00Z")
    second = capture(core, ctx, "H200 使用第二套部署配置。", key="TEST-p08/hard", revision=2, when="2026-09-05T12:00:00Z")
    single = core.recall(ctx, recall_request(query="H100 部署配置", mode="current"), deadline_seconds=5)
    comparison = core.recall(ctx, recall_request(query="比较 H100 和 H200 部署配置", mode="history"), deadline_seconds=5)
    assert [item.content for item in single.items] == []
    assert {item.content for item in comparison.items} == {"H100 使用第一套部署配置。", "H200 使用第二套部署配置。"}


def test_as_of_hydrates_historical_source_before_new_revision(app):
    core, ctx = app
    old = capture(core, ctx, "旧版本 H100 配置。", key="TEST-p08/asof", revision=1, when="2026-09-01T12:00:00Z")
    capture(core, ctx, "新版本 H200 配置。", key="TEST-p08/asof", revision=2, when="2026-09-05T12:00:00Z")
    result = core.recall(ctx, recall_request(query="H100 配置", mode="as_of", as_of="2026-09-03T00:00:00Z"), deadline_seconds=5)
    assert [item.content for item in result.items] == ["旧版本 H100 配置。"]
    assert result.items[0].revision == old.revision


def test_current_source_receipt_excludes_only_that_source(app):
    core, ctx = app
    source = capture(core, ctx, "本轮刚捕获的 TEST 计划。", key="TEST-p08/current")
    key = f"{source.ref}@{source.revision}"
    result = core.recall(ctx, recall_request(query="TEST 计划", mode="current"), current_source_refs=(key,), deadline_seconds=5)
    assert result.items == ()


def test_collection_cursor_binds_epoch_scope_and_filters(app):
    core, ctx = app
    capture(core, ctx, "第一条 H100 记录。", key="TEST-p08/c1")
    capture(core, ctx, "第二条 H200 记录。", key="TEST-p08/c2")
    epoch = core.status(ctx).memory_epoch
    query = CollectionQuery("event", page_size=1, memory_epoch=epoch, scope_digest=scope_digest(ctx), mode="current")
    first = core.collection(ctx, query)
    assert first.coverage == "partial"
    assert first.next_cursor is not None
    second = core.collection(ctx, query, cursor=first.next_cursor)
    assert second.coverage == "partial"
    assert second.next_cursor is None
    assert first.items[0].ref != second.items[0].ref
    with pytest.raises(ContractError):
        core.collection(ctx, replace(query, memory_epoch=epoch - 1), cursor=first.next_cursor)


def test_cursor_encoding_is_validated_before_scope_binding(app):
    with pytest.raises(ContractError):
        PageCursor.decode("not-a-cursor")



def test_P08_lexical_skips_terms_too_common_to_separate_anything(app):
    """A term matching most of the corpus costs the most and tells the least.

    Measured on alpha: ten terms cleared 10% document frequency, every one a
    JSON field name from tool-observation envelopes (`tool`, `summary`,
    `omitted`, `exit_code`, ...), together 10.3% of the whole index. A query
    containing one walked an 18,000-row posting list to learn nothing.
    """
    from scope_recall.core.retrieval_storage import (
        _LEXICAL_DF_FLOOR,
        _discriminating_terms,
    )

    core, ctx = app
    capture(core, ctx, "TEST quarkonium 出现一次。", key="TEST-p08/rare")
    for index in range(_LEXICAL_DF_FLOOR + 2):
        capture(core, ctx, f"TEST boilerplate 第{index}条。", key=f"TEST-p08/common/{index}")

    with core.storage.read(ctx) as tx:
        assert _discriminating_terms(tx, ("quarkonium", "boilerplate")) == ("quarkonium",),             "the common term is dropped, the rare one stays"
        # A query made only of common terms must still answer: falling back to
        # the rarest of them beats returning nothing at all.
        assert _discriminating_terms(tx, ("boilerplate",)) == ("boilerplate",)
        # A term the index has never seen has no frequency and is never pruned.
        assert _discriminating_terms(tx, ("neverindexed",)) == ("neverindexed",)


def test_P08_lexical_pool_ranks_rows_naming_the_hard_identifier_first(app):
    """Hydration admits only sources naming the query's identifier, so the pool must reach them.

    Sixty sources share six generic query terms but not ``rc28``; the one that
    names it matches fewer terms.  Ranked by hits alone it fell outside the
    47-row first-round pool and every mode answered nothing.
    """
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    query = "阿乙仍然是 Scope Recall rc28"
    target = capture(core, ctx, "阿乙升级收尾：排空超时，现在还是 rc28，旧记忆和设置没动。",
                     key="TEST-p08/identifier-pool/target", when="2026-09-01T12:00:00Z")
    for index in range(60):
        capture(core, ctx, f"阿乙仍然是 Scope Recall 的测试对象（记录 {index}）",
                key=f"TEST-p08/identifier-pool/{index}", when="2026-09-02T12:00:00Z")
    # Another session reads, so the recent channel cannot supply the target.
    reader = replace(ctx, session_id="TEST-p08-identifier-pool-reader")
    search = SearchContext.from_request(recall_request(query=query, mode="history"), reader,
                                        now=Clock.now, deadline=core.clock.monotonic() + 5)
    with core.storage.read(reader) as tx:
        pool = RetrievalStorage().lexical(tx, search, limit=47)
    assert pool[0].ref == target.ref
    assert pool[0].lexical_score < pool[1].lexical_score, "identifier first, then hits"
    for mode in ("auto", "current", "history"):
        result = core.recall(reader, recall_request(query=query, mode=mode), deadline_seconds=5)
        assert [item.ref for item in result.items] == [target.ref], mode
