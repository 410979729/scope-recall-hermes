"""Deterministic P08 core retrieval contracts using synthetic TEST identity."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, PageCursor, SearchContext
from scope_recall.core.retrieval_storage import scope_digest
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request, source_event


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
        # A kept term survives however common it is.
        assert _discriminating_terms(tx, ("quarkonium", "boilerplate"), keep=("boilerplate",)) == ("quarkonium", "boilerplate")


def test_P08_lexical_never_prunes_a_common_hard_identifier(app):
    """Hydration still requires the identifier, so SQL must still search for it.

    With 65 sources naming ``rc28`` the term clears the document-frequency
    floor.  Pruned in favour of the query's rarer (here unindexed) terms, SQL
    matched nothing hydration would admit: 63 sources were found, 64 were not.
    """
    from scope_recall.core.retrieval_storage import _LEXICAL_DF_FLOOR

    core, ctx = app
    for index in range(_LEXICAL_DF_FLOOR):
        capture(core, ctx, f"rc28 构建日志 第{index}条", key=f"TEST-p08/common-identifier/{index}",
                when="2026-09-02T12:00:00Z")
    target = capture(core, ctx, "现在还是 rc28，旧记忆和设置没动。", key="TEST-p08/common-identifier/target",
                     when="2026-09-03T12:00:00Z")
    reader = replace(ctx, session_id="TEST-p08-common-identifier-reader")
    for mode in ("auto", "current", "history"):
        result = core.recall(reader, recall_request(query="rc28 升级结果", mode=mode), deadline_seconds=5)
        assert result.items and result.items[0].ref == target.ref, mode


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

    # Memory reinjection naming the identifier, and every query term, still ranks last.
    reinjected = core.record_event(
        replace(ctx, actor_origin="memory_reinjection"),
        source_event(source_event_key="TEST-p08/identifier-pool/reinjected", origin="memory_reinjection", role="tool",
                     content="召回注入：阿乙仍然是 Scope Recall，现在还是 rc28。", occurred_at="2026-09-03T12:00:00Z"),
        scope_id="TEST-scope", remaining_seconds=10,
    ).event_refs[0]
    with core.storage.read(reader) as tx:
        pool = RetrievalStorage().lexical(tx, search, limit=100)
    assert (pool[0].ref, pool[-1].ref) == (target.ref, reinjected.ref)


# -- Chinese synonyms ------------------------------------------------------------


@contextmanager
def _without_synonyms():
    """The unexpanded lexical path: the same pipeline with an empty synonym table."""
    from scope_recall.core import recall_policy

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recall_policy, "_SYNONYMS", {})
        yield


def _lexical_pool(core, reader, query, *, mode="history"):
    from scope_recall.core.retrieval_storage import RetrievalStorage

    search = SearchContext.from_request(recall_request(query=query, mode=mode), reader,
                                        now=Clock.now, deadline=core.clock.monotonic() + 5)
    with core.storage.read(reader) as tx:
        return RetrievalStorage().lexical(tx, search, limit=47)


def _recalled(core, reader, query, mode):
    return [item.ref for item in core.recall(reader, recall_request(query=query, mode=mode), deadline_seconds=5).items]


def test_P08_synonym_table_is_short_disjoint_and_one_index_term_per_member():
    from scope_recall.core.events import lexical_terms
    from scope_recall.core.recall_policy import SYNONYM_GROUPS

    members = [member for group in SYNONYM_GROUPS for member in group]
    assert 10 <= len(SYNONYM_GROUPS) <= 20 and all(len(group) >= 2 for group in SYNONYM_GROUPS)
    assert len(members) == len(set(members)), "a word belongs to one group"
    # Two CJK characters make one index term, so a swap lines up bigram for bigram.
    assert all(len(member) == 2 and lexical_terms(member) == (member,) for member in members)
    assert "还是" not in members, "still, but also 'or'"


def test_P08_judged_probe_questions_are_outside_the_synonym_table():
    """The probe set is scored on a corpus no gate has: a table change reaching one must be re-measured."""
    from scope_recall.core.recall_policy import synonym_expansions
    from tests.contract.recall_probes import ANSWERABLE, UNANSWERABLE

    for query in (*(question for question, _pattern in ANSWERABLE), *UNANSWERABLE):
        assert synonym_expansions(query) == {}, query


@pytest.mark.parametrize("mode", ["current", "history"])
def test_P08_lexical_synonym_reaches_a_paraphrase_without_the_query_word(app, mode):
    """"阿乙装上了吗" shares one of its five terms with "阿乙已经安装好了"; three are needed."""
    core, ctx = app
    target = capture(core, ctx, "阿乙已经安装好了", key="TEST-p08/synonym/installed")
    reader = replace(ctx, session_id="TEST-p08-synonym-reader")
    query = "阿乙装上了吗"
    with _without_synonyms():
        assert _recalled(core, reader, query, mode) == []
    # 安装 and 装好 both stand in for 装上, 好了 for 上了: each query term is
    # credited once, and coverage is still counted against the query's five.
    [candidate] = _lexical_pool(core, reader, query, mode=mode)
    assert (candidate.ref, candidate.lexical_score, candidate.matched_query_terms) == (
        target.ref, 3.0, ("上了", "阿乙", "装上"))
    # The source leads; its capture episode may follow by relation.
    assert _recalled(core, reader, query, mode)[:1] == [target.ref]


def test_P08_lexical_synonym_hit_counts_once_for_the_query_term_it_stands_in_for(app):
    """A source sharing nothing with the query but synonyms of one word is one term, and stays out."""
    core, ctx = app
    one = capture(core, ctx, "新版安装包放在共享盘", key="TEST-p08/synonym-bound/one")
    every = capture(core, ctx, "先安装依赖，再把插件装好", key="TEST-p08/synonym-bound/every")
    reader = replace(ctx, session_id="TEST-p08-synonym-bound-reader")
    query = "阿乙装上了吗"
    pool = {candidate.ref: (candidate.lexical_score, candidate.matched_query_terms)
            for candidate in _lexical_pool(core, reader, query)}
    assert pool == {one.ref: (1.0, ("装上",)), every.ref: (1.0, ("装上",))}
    for mode in ("current", "history"):
        assert _recalled(core, reader, query, mode) == [], mode


def test_P08_lexical_admission_counts_only_the_query_s_own_terms():
    query = "阿乙装上了吗"
    policy = RecallPolicy(vector_threshold=None)
    credited = CandidateRef("event", "event-TEST", 1, "lexical", lexical_score=3.0,
                            matched_query_terms=("上了", "阿乙", "装上"))
    assert policy.lexical_admission(credited, query) == (True, None)
    # The same count reported over synonym terms is one query term, not three.
    inflated = replace(credited, matched_query_terms=("安装", "装上", "装好"))
    assert policy.lexical_admission(inflated, query) == (False, "lexical_insufficient_specificity")


def test_P08_lexical_synonym_ranks_a_paraphrased_release_status_into_the_pool(app):
    """"依然是 rc28" is answered by "仍然是 rc28", never by rc29, however many rc28 logs compete."""
    core, ctx = app
    query = "阿乙依然是 rc28 吗"
    target = capture(core, ctx, "阿乙仍然是 rc28", key="TEST-p08/synonym-pool/target", when="2026-09-01T12:00:00Z")
    rc29 = capture(core, ctx, "阿乙仍然是 rc29", key="TEST-p08/synonym-pool/rc29", when="2026-09-01T12:00:00Z")
    reader = replace(ctx, session_id="TEST-p08-synonym-pool-reader")
    # rc29 matches every Chinese term through the synonym; the identifier still decides.
    pool = {candidate.ref: candidate.lexical_score for candidate in _lexical_pool(core, reader, query)}
    assert pool == {target.ref: 5.0, rc29.ref: 4.0}
    for mode in ("current", "history"):
        assert _recalled(core, reader, query, mode) == [target.ref], mode

    # Sixty newer rc28 logs share three terms each, as many as the target
    # shares literally: unexpanded, they fill the pool and the answer is lost.
    for index in range(60):
        capture(core, ctx, f"阿乙 rc28 回归记录 {index}：结果当然是通过",
                key=f"TEST-p08/synonym-pool/{index}", when="2026-09-02T12:00:00Z")
    with _without_synonyms():
        assert target.ref not in {candidate.ref for candidate in _lexical_pool(core, reader, query)}
        for mode in ("current", "history"):
            assert target.ref not in _recalled(core, reader, query, mode), mode
    assert _lexical_pool(core, reader, query)[0].ref == target.ref
    for mode in ("current", "history"):
        refs = _recalled(core, reader, query, mode)
        assert refs[0] == target.ref and rc29.ref not in refs, mode


def test_P08_lexical_query_without_a_synonym_is_untouched_by_the_table(app):
    from scope_recall.core.recall_policy import synonym_expansions

    core, ctx = app
    for index, text in enumerate((
        "阿乙升级收尾：现在还是 rc28，旧记忆和设置没动。",
        "阿乙仍然是 Scope Recall 的测试对象",
        "网关安装日志：rc28 已就绪",
        "安全审计：阿乙网关日志 rc28 已归档",
    )):
        capture(core, ctx, text, key=f"TEST-p08/synonym-free/{index}", when=f"2026-09-0{index + 1}T12:00:00Z")
    reader = replace(ctx, session_id="TEST-p08-synonym-free-reader")
    for query in ("阿乙升级结果怎么样", "rc28 网关日志", "安全审计归档了吗"):
        assert synonym_expansions(query) == {}, query
        for mode in ("current", "history"):
            request = recall_request(query=query, mode=mode)
            expanded = (_lexical_pool(core, reader, query, mode=mode), core.recall(reader, request, deadline_seconds=5))
            with _without_synonyms():
                base = (_lexical_pool(core, reader, query, mode=mode), core.recall(reader, request, deadline_seconds=5))
            assert expanded[0] and expanded == base, (query, mode)
