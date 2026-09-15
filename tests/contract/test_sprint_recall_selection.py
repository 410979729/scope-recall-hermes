"""Focused regressions for bounded selection; no model or host calls."""
import json
from dataclasses import replace

from scope_recall.core.background_context import background_candidates
from scope_recall.core.recall import RetrievalPipeline
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext, SearchLimits
from tests.contract.test_v11_claims import app, accept, capture, draft
from tests.v11_support import recall_request


def _search(core, ctx, query):
    return SearchContext.from_request(recall_request(query=query), ctx,
        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)


def test_old_relevant_preference_survives_busy_recent_window(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 输出格式 Markdown。")
    older = accept(core, ctx, draft(source, "Markdown", kind="preference", predicate="输出格式")).items[0]
    for index in range(26):
        source = capture(core, ctx, f"TEST-project 配色{index} 蓝色。", when="2026-09-03T12:00:00Z")
        accept(core, ctx, draft(source, "蓝色", kind="preference", predicate=f"配色{index}"))
    search = _search(core, ctx, "输出格式如何选择")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
    assert older.ref in {candidate.ref for candidate, _ in selected}
    assert len(selected) <= 2


def test_matching_condition_selects_exception_instead_of_general_value(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 表达偏好 简洁。")
    general = accept(core, ctx, draft(source, "简洁", kind="preference", predicate="表达偏好")).items[0]
    source = capture(core, ctx, "只在写文案时 TEST-project 表达偏好 详细。")
    exception = accept(core, ctx, draft(source, "详细", kind="preference", predicate="表达偏好", conditions=["写文案时"])).items[0]
    search = _search(core, ctx, "现在帮我写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
        ordinary = background_candidates(tx, replace(search, query="现在不是写文案，只核对报价"), core.recall_pipeline.storage_reader, core.clock)
    assert [candidate.ref for candidate, _ in selected] == [exception.ref]
    assert [candidate.ref for candidate, _ in ordinary] == [general.ref]


def _pair(ref, text, root, score=.016):
    content = json.dumps(dict(subject="TEST-project", predicate="state", value_text=text, conditions=[]))
    obj = RetrievedObject(ref, 1, "claim", content, "human_direct", "current", "trusted scope",
                          (root,), "direct_report", True)
    return CandidateRef("claim", ref, 1, "lexical", fusion_score=score), obj


def test_conflicting_matching_conditions_do_not_get_arbitrary_precedence(app):
    core, ctx = app
    items = []
    for condition, value in (("写文案时", "详细"), ("用中文时", "简洁")):
        source = capture(core, ctx, f"只在{condition} TEST-project 表达偏好 {value}。")
        items.append(accept(core, ctx, draft(source, value, kind="preference", predicate="表达偏好",
                                             conditions=[condition])).items[0])
    search = _search(core, ctx, "现在用中文写文案")
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
    assert not {item.ref for item in items}.intersection(candidate.ref for candidate, _ in selected)


def test_ranking_covers_another_part_of_question_before_duplicate_evidence(app):
    core, ctx = app
    first = _pair("claim-a", "deployment ready", "event-a@1")
    repeat = _pair("claim-b", "deployment ready", "event-a@1")
    other = _pair("claim-c", "rollback ready", "event-c@1", score=.015)
    search = _search(core, ctx, "deployment rollback")
    selected = core.recall_pipeline._rank_hydrated([first, repeat, other], search)
    assert [candidate.ref for candidate, _ in selected[:2]] == ["claim-a", "claim-c"]
    historical = core.recall_pipeline._rank_hydrated([first, repeat, other], replace(search, mode="history"))
    assert [candidate.ref for candidate, _ in historical] == ["claim-a", "claim-b", "claim-c"]


def test_large_middle_candidate_does_not_hide_a_small_later_candidate():
    first = _pair("claim-a", "short", "event-a@1")
    large = _pair("claim-b", "x" * 3000, "event-b@1")
    last = _pair("claim-c", "short", "event-c@1")
    kept = RetrievalPipeline(None)._apply_budget([first, large, last], SearchLimits(budget_tokens=128))
    assert [candidate.ref for candidate, _ in kept] == ["claim-a", "claim-c"]
