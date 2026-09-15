"""Small synthetic counterexamples for authority and current-memory quality."""
from dataclasses import replace

import pytest

from scope_recall.core.background_context import background_candidates, mark_background
from scope_recall.core.recall_packet import RecallPacketCompiler
from scope_recall.core.retrieval import CandidateRef, CollectionQuery, RetrievedObject, SearchContext
from scope_recall.core.retrieval_storage import scope_digest
from tests.contract.test_v11_claims import app, accept, capture, draft
from tests.contract.test_v11_episodes import apply, resume
from tests.v11_support import recall_request


def test_background_condition_negated_in_query_is_not_applicable(app):
    core, ctx = app
    source = capture(core, ctx, "只在写文案时 TEST-project 表达偏好 简洁。")
    item = accept(core, ctx, draft(source, "简洁", kind="preference", predicate="表达偏好",
                                  conditions=["写文案时"])).items[0]
    assert item.state == "active"
    search = SearchContext.from_request(recall_request(query="这次不是写文案时，只做财务核对"), ctx,
                                        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
    assert not any(candidate.ref == item.ref for candidate, _ in selected)
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, replace(search, query="现在写文案时做财务核对"),
                                         core.recall_pipeline.storage_reader, core.clock)
    assert any(candidate.ref == item.ref for candidate, _ in selected)


@pytest.mark.parametrize("kind", ["fact", "preference", "constraint"])
def test_subject_identifier_prefix_cannot_bind_a_different_subject(app, kind):
    core, ctx = app
    source = capture(core, ctx, "TEST-project-other 表达偏好 简洁。")
    item = accept(core, ctx, draft(source, "简洁", kind=kind, predicate="表达偏好")).items[0]
    assert item.state == "proposed"
    assert core.current_claim(ctx, item.ref) is None


@pytest.mark.parametrize("kind", ["fact", "constraint"])
def test_claim_cannot_drop_negative_qualifier_from_value(app, kind):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 网站操作 不能修改网页。")
    item = accept(core, ctx, draft(source, "修改网页", kind=kind, predicate="网站操作")).items[0]
    assert item.state == "proposed"


def test_correction_identifier_prefix_does_not_revise_other_subject(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-device 配色 H100。")
    item = accept(core, ctx, draft(source, "H100", kind="fact", subject="TEST-device")).items[0]
    assert item.state == "active"
    capture(core, ctx, "TEST-device-other 配色换成 H200。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H100"
    assert len(core.claim_history(ctx, item.ref)) == 1


@pytest.mark.parametrize("text", ["TEST 不要继续任务。", "TEST do not resume the task."])
def test_negated_resume_does_not_reopen_cancelled_task(app, text):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-task")
    capture(core, ctx, "取消这个任务。")
    episode = core.episodes(ctx)[0]
    assert episode.state == "cancelled"
    capture(core, ctx, text, when="2026-09-03T12:00:00Z")
    assert core.episodes(ctx)[0].state == "cancelled"


@pytest.mark.parametrize("kind", ["fact", "preference", "constraint"])
def test_current_project_does_not_supply_missing_subject_evidence(app, kind):
    core, ctx = app
    source = capture(core, ctx, "TEST-other 表达偏好 简洁。")
    item = accept(core, ctx, draft(source, "简洁", kind=kind, predicate="表达偏好")).items[0]
    assert item.state == "proposed"


@pytest.mark.parametrize("kind", ["claim", "episode"])
def test_packet_prioritization_never_moves_background_ahead_of_query(app, kind):
    core, ctx = app
    search = SearchContext.from_request(recall_request(query="继续核对报价" if kind == "episode" else "核对报价"),
                                        ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    event = RetrievedObject("event-query", 1, "event", "报价的证据", "human_direct", "current", "trusted scope",
                            ("event-query@1",), "direct_report", True)
    background = mark_background(replace(event, ref=f"{kind}-background", kind=kind, content="其他背景",
                                         metadata=(("state", "active"),)))
    evidence = (CandidateRef("event", event.ref, 1, "lexical"), event)
    ambient = (CandidateRef(kind, background.ref, 1, "background"), background)
    order = RecallPacketCompiler._prioritize_resume_evidence(search, [evidence, ambient])
    order = RecallPacketCompiler._prioritize_current_claims(search, order)
    assert order[0][0].ref == event.ref


def test_full_negative_constraint_retains_its_polarity(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 网站操作 不能修改网页。")
    item = accept(core, ctx, draft(source, "不能修改网页", kind="constraint", predicate="网站操作")).items[0]
    assert item.state == "active"


def test_future_revision_does_not_hide_effective_preference_or_collection(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 表达偏好 简洁。")
    original = accept(core, ctx, draft(source, "简洁", kind="preference", predicate="表达偏好")).items[0]
    future = capture(core, ctx, "从2026年9月10日起 TEST-project 表达偏好 详细。", when="2026-09-06T12:00:00Z")
    changed = accept(core, ctx, draft(future, "详细", kind="preference", predicate="表达偏好",
                                     valid_from="2026-09-10T00:00:00Z")).items[0]
    assert changed.ref == original.ref and changed.revision == 2
    assert core.current_claim(ctx, original.ref).revision == 1
    search = SearchContext.from_request(recall_request(query="核对报价"), ctx,
                                        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
        assert [(candidate.ref, candidate.revision) for candidate, _ in selected] == [(original.ref, 1)]
        current = replace(search, mode="current")
        query = CollectionQuery("claim", memory_epoch=tx.status().memory_epoch, scope_digest=scope_digest(ctx))
        page = core.recall_pipeline.storage_reader.collection(tx, current, query)
    assert [(item.ref, item.revision, item.temporal_status) for item in page.items] == [(original.ref, 1, "current")]
    core.clock.now = "2026-09-11T12:00:00Z"
    search = replace(search, now=core.clock.now)
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock)
    assert [(candidate.ref, candidate.revision) for candidate, _ in selected] == [(original.ref, 2)]


def test_changed_environment_progress_is_not_current_resume_evidence(app):
    core, ctx = app
    before = replace(ctx, task_anchor="TEST-task", environment_revision="TEST-env-old")
    source = capture(core, before, "TEST 请检查环境，结构已经确认。")
    apply(core, before, resumes=[resume(source, verified_progress=[dict(text="结构已经确认", evidence_refs=[f"{source.ref}@1"])])])
    episode = core.episodes(before)[0]
    after = replace(before, environment_revision="TEST-env-new")
    search = SearchContext.from_request(recall_request(query="继续任务", mode="current"), after,
                                        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    candidate = CandidateRef("episode", episode.ref, episode.revision, "exact_ref")
    with core.storage.read(after) as tx:
        assert core.recall_pipeline.storage_reader.hydrate(tx, candidate, search) is None
        historical = core.recall_pipeline.storage_reader.hydrate(tx, candidate, replace(search, mode="history"))
    assert historical is not None and historical.temporal_status == "historical"
    assert "environment_needs_revalidation" in historical.applicability
