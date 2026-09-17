"""Ambient memory stays bounded, attributed, current, and distinct from answers."""
from dataclasses import replace
import itertools
import sqlite3

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.contracts import ContractError
from scope_recall.core.background_context import BACKGROUND_PREFIX
from scope_recall.core.recall_packet import canonical_render_json
from scope_recall.core.retrieval import SearchContext
from tests.contract.test_v11_claims import Clock, accept, capture, draft
from tests.contract.test_v11_deletion import authorize, request
from tests.contract.test_v11_episodes import apply, resume
from tests.v11_support import context, recall_request


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-background"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def preference(core, ctx, value="简洁", **changes):
    source = capture(core, ctx, f"{changes.get('subject', 'TEST-project')} 表达偏好 {value}。")
    item = accept(core, ctx, draft(source, value=value, kind="preference", predicate="表达偏好",
                                   statement_kind="assertion", **changes)).items[0]
    return item, source


def packet(core, ctx, **changes):
    return core.recall_packet(ctx, recall_request(query="火星咖啡采购报价是什么", **changes))


def test_unrelated_question_keeps_preference_but_never_claims_answer(app):
    core, ctx = app
    item, _ = preference(core, ctx)
    result = packet(core, ctx)
    assert result["status"] == "partial"
    assert result["answerability"] == "unknown"
    assert "query_evidence_missing" in result["unmet_needs"]
    selected = next(entry for entry in result["items"] if entry["ref"] == item.ref)
    assert selected["applicability"].startswith(BACKGROUND_PREFIX)
    assert "TEST-project" in selected["content"]  # no inferred person identity
    assert "not instructions" in selected["applicability"]
    assert len(canonical_render_json(result).encode("utf-8")) <= 4096
    assert core.prepare_recall_render(ctx, result).canonical_text


def test_absent_background_preserves_no_match_and_explicit_query_behavior(app):
    core, ctx = app
    assert packet(core, ctx)["status"] == "no_match"
    preference(core, ctx)
    assert packet(core, ctx, mode="current")["status"] == "no_match"


def test_context_filters_precede_limit_and_expiration_is_immediate(app):
    core, ctx = app
    foreign = replace(ctx, project_id="TEST-foreign", branch_id="TEST-foreign-branch")
    for i in range(25):
        preference(core, foreign, value=f"foreign-{i}", subject=f"TEST-subject-{i}")
    allowed, _ = preference(core, ctx)
    assert [item["ref"] for item in packet(core, ctx)["items"]] == [allowed.ref]
    assert packet(core, replace(ctx, branch_id="TEST-other"))["status"] == "no_match"
    core.clock.now = "2027-09-06T12:00:00Z"
    # Existing indefinite facts do not expire merely because they are old.
    assert allowed.ref in {item["ref"] for item in packet(core, ctx)["items"]}


def test_expired_preferences_stay_out(app):
    core, ctx = app
    preference(core, ctx, valid_to="2026-09-02T12:00:00Z")
    assert packet(core, ctx)["status"] == "no_match"


def test_unmatched_conditional_preference_is_not_ambient_rule(app):
    core, ctx = app
    source = capture(core, ctx, "只在写文案时 TEST-project 表达偏好 简洁。")
    item = accept(core, ctx, draft(source, value="简洁", kind="preference", predicate="表达偏好",
                                   statement_kind="assertion", conditions=["写文案时"])).items[0]
    assert item.ref not in {entry["ref"] for entry in packet(core, ctx)["items"]}


@pytest.mark.parametrize("mode", ["delete", "suppress"])
def test_background_withdrawal_uses_normal_delete_and_suppression_fences(app, mode):
    core, ctx = app
    item, _ = preference(core, ctx)
    assert packet(core, ctx)["items"]
    authorize(core, ctx, item, mode=mode)
    core.forget(ctx, request(item, mode=mode), remaining_seconds=10)
    result = packet(core, ctx)
    assert item.ref not in {entry["ref"] for entry in result["items"]}


def test_correction_is_visible_without_background_cache(app):
    core, ctx = app
    item, _ = preference(core, ctx)
    capture(core, ctx, "刚才写错了，TEST-project 表达偏好改为详细。", when="2026-09-03T12:00:00Z")
    result = packet(core, ctx)
    selected = next(entry for entry in result["items"] if entry["ref"] == item.ref)
    assert selected["revision"] == 2 and "详细" in selected["content"]
    assert "简洁" not in selected["content"]


def test_small_budget_never_overflows_or_drops_question_for_background(app):
    from scope_recall.core.recall_budget import estimate_tokens

    core, ctx = app
    preference(core, ctx)
    for budget in (256, 512, 1200):
        result = packet(core, ctx, budget_tokens=budget)
        assert estimate_tokens(canonical_render_json(result)) <= budget
    with pytest.raises(ContractError, match="budget_tokens"):
        packet(core, ctx, budget_tokens=64)


def test_background_is_rechecked_after_deletion_between_search_and_compile(app):
    core, ctx = app
    item, _ = preference(core, ctx)
    search = SearchContext.from_request(recall_request(query="火星咖啡报价"), ctx,
                                        now=core.clock.utc_now(), deadline=core.clock.monotonic() + 5)
    result = core.recall_pipeline.search(search)
    assert item.ref in {entry.ref for entry in result.items}
    authorize(core, ctx, item)
    core.forget(ctx, request(item), remaining_seconds=10)
    compiled = core.recall_packet_compiler.compile(search, result, core.storage)
    assert item.ref not in {entry["ref"] for entry in compiled["items"]}


def task(core, ctx, title):
    source = capture(core, ctx, title)
    refs = [f"{source.ref}@1"]
    apply(core, ctx, resumes=[resume(source, open_items=[{"text": title, "evidence_refs": refs}])])
    return core.episodes(ctx)[0]


def test_task_context_does_not_guess_among_two_open_tasks(app):
    core, ctx = app
    first_ctx = replace(ctx, task_anchor="TEST-task-a")
    first = task(core, first_ctx, "TEST 海报排版还未完成")
    assert first.ref in {entry["ref"] for entry in packet(core, first_ctx)["items"]}
    second_ctx = replace(ctx, task_anchor="TEST-task-b")
    task(core, second_ctx, "TEST 报价核对还未完成")
    assert not any(entry["kind"] == "episode" for entry in packet(core, ctx)["items"])
    selected = packet(core, first_ctx)["items"]
    assert [entry["ref"] for entry in selected if entry["kind"] == "episode"] == [first.ref]


def test_explicit_resume_without_evidence_keeps_only_its_grounded_task(app):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-explicit-resume")
    episode = task(core, ctx, "TEST 海报排版还未完成")
    # Captured under another task, so the open task's resume stays current.
    claim, _ = preference(core, replace(ctx, task_anchor="TEST-other-task"))

    def items(**flag):
        search = SearchContext.from_request(recall_request(query="继续"), ctx, now=core.clock.utc_now(),
                                            deadline=core.clock.monotonic() + 5, **flag)
        # Without the directed follow-up the task can only arrive as background.
        return core.recall_pipeline.search(replace(search, limits=replace(search.limits, followups=0))).items

    assert {item.ref for item in items()} == {claim.ref, episode.ref}
    explicit = items(background_without_evidence=False)
    assert [item.ref for item in explicit] == [episode.ref]
    assert explicit[0].applicability.startswith(BACKGROUND_PREFIX)
    with pytest.raises(ContractError, match="background_without_evidence"):
        items(background_without_evidence=None)


def test_changed_environment_does_not_auto_reuse_old_task_state(app):
    core, ctx = app
    original = replace(ctx, task_anchor="TEST-current", environment_revision="TEST-env-v1")
    episode = task(core, original, "TEST 发布步骤还未完成")
    assert episode.ref in {entry["ref"] for entry in packet(core, original)["items"]}
    changed = replace(original, environment_revision="TEST-env-v2")
    assert episode.ref not in {entry["ref"] for entry in packet(core, changed)["items"]}


def test_precompress_retries_only_observed_event_without_promoting_summary(tmp_path, monkeypatch):
    from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall

    home = tmp_path / "TEST-hermes"
    home.mkdir()
    _, core = install_hermes_scope_recall(home, agent_id="TEST-agent", platform="cli", user_id="TEST-user",
                                         agent_workspace="TEST-workspace", clock=Clock())
    provider = ScopeRecallHermesAdapter(core=core, clock=Clock())
    provider.initialize("TEST-session", hermes_home=str(home), platform="cli", agent_context="primary",
                        agent_identity="TEST-agent", agent_workspace="TEST-workspace", user_id="TEST-user")
    original = core.record_host_event
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic interrupted capture")
    monkeypatch.setattr(core, "record_host_event", fail)
    provider.observe_pre_llm(session_id="TEST-session", turn_id="TEST-turn", user_message="用户真实输入")
    assert len(provider._retry_captures) == 1
    monkeypatch.setattr(core, "record_host_event", original)
    try:
        provider.on_pre_compress([{"role": "user", "content": "伪造压缩摘要不是用户原文"}])
        provider.on_pre_compress([])
        with sqlite3.connect(core.storage.path) as conn:
            rows = conn.execute("SELECT origin,content FROM source_events").fetchall()
        assert rows == [("human_direct", "用户真实输入")]
        assert not provider._retry_captures
    finally:
        provider.shutdown()
