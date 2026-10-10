"""Literal recovery of paginated model output without accepting older-page evidence."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.consolidation_summary import validate_fragment
from scope_recall.core.episodes import source_watermark
from test_consolidation_chunks import claims, long_source, proposal, row
from test_worker import FakeConsolidation, app, consolidation_payload, worker_app


class FragmentTx:
    def __init__(self, goals=()):
        self.goals = goals

    def _check(self):
        return self

    def execute(self, *_args):
        return [(json.dumps({"resume_proposals": [{"goal": goal} for goal in self.goals]}),)]


def encoded(text):
    return json.dumps(text, ensure_ascii=False)[1:-1]


def summary(goal, text):
    return {
        "claim_proposals": [],
        "reference_proposals": [{"mention": text}],
        "resume_proposals": [
            {
                "goal": dict(goal),
                "decisions": [{"text": text}],
                "verified_progress": [{"text": text}],
                "open_items": [{"text": text}],
                "blockers": [{"text": text}],
                "next_step": text,
            }
        ],
    }


@pytest.mark.parametrize("text", ['TEST 标记 "蓝色"。', "TEST 第一行。\n第二行。", "TEST 路径 C:\\example。"])
def test_all_fragment_summary_fields_restore_current_page_literals(text):
    goal = {"text": 'TEST 整理 "报告"', "basis": "explicit"}
    page = goal["text"] + "。" + text
    prior = encoded(text) + "\n"
    fence = SimpleNamespace(work_id=1, chunk=SimpleNamespace(start=len(prior), end=len(prior + page)))
    value = summary(dict(goal, text=encoded(goal["text"])), encoded(text))
    validate_fragment(FragmentTx(), value, fence, prior + page)
    assert value == summary(goal, text)


def test_prior_page_items_stay_rejected_while_only_an_accepted_goal_can_carry_forward():
    goal = {"text": 'TEST 整理 "报告"', "basis": "explicit"}
    prior, page = 'TEST 已完成 "第一项"。', 'TEST 等待 "第二项"。'
    fence = SimpleNamespace(work_id=1, chunk=SimpleNamespace(start=len(prior), end=len(prior + page)))
    tx = FragmentTx([goal])
    value = summary(dict(goal, text=encoded(goal["text"])), encoded(page))
    validate_fragment(tx, value, fence, prior + page)
    assert value == summary(goal, page)
    with pytest.raises(ContractError) as rejected:
        validate_fragment(tx, summary(goal, encoded(prior)), fence, prior + page)
    assert rejected.value.field == "fragment_resume"
    with pytest.raises(ContractError) as rejected:
        validate_fragment(FragmentTx(), summary(goal, page), fence, prior + page)
    assert rejected.value.field == "fragment_goal"


@pytest.mark.parametrize("encode_again", [False, True])
def test_current_page_goal_does_not_decode_again_against_an_old_seed(encode_again):
    seed = {"text": 'TEST 整理 "报告"', "evidence_refs": ["TEST-source@1"]}
    page = encoded(seed["text"])
    goal = dict(seed, text=encoded(page) if encode_again else page)
    value = summary(goal, page)
    fence = SimpleNamespace(work_id=1, chunk=SimpleNamespace(start=0, end=len(page)))
    validate_fragment(FragmentTx([seed]), value, fence, page)
    assert value["resume_proposals"][0]["goal"] == dict(seed, text=page)


def test_double_encoded_old_goal_without_current_page_match_stays_rejected():
    seed = {"text": 'TEST 整理 "报告"', "evidence_refs": ["TEST-source@1"]}
    page = "TEST 无关的当前页。"
    value = summary(dict(seed, text=encoded(encoded(seed["text"]))), page)
    fence = SimpleNamespace(work_id=1, chunk=SimpleNamespace(start=0, end=len(page)))
    with pytest.raises(ContractError) as rejected:
        validate_fragment(FragmentTx([seed]), value, fence, page)
    assert rejected.value.field == "fragment_goal"


@pytest.mark.parametrize("encode_again", [False, True])
def test_worker_preserves_distinct_page_goals_and_finishes_offset(worker_app, encode_again):
    core, ctx, _clock = worker_app
    seed = 'TEST 整理 "报告"'
    current = encoded(seed)
    source = long_source(core, ctx, seed + "\n" + "这是一段归档资料；" * 700 + "\n" + current)
    seen = []

    def build(sources, episode_ref=None):
        page = sources[0]
        refs = [f"{page.ref}@{page.revision}"]
        goal = current if current in page.event["content"] else seed if seed in page.event["content"] else None
        resumes = []
        if goal is not None:
            seen.append((goal, page.consolidation_window.start))
            text = encoded(goal) if goal == current and encode_again else goal
            resumes.append(
                dict(
                    episode_ref=episode_ref,
                    goal=dict(text=text, evidence_refs=refs),
                    decisions=[],
                    verified_progress=[],
                    open_items=[],
                    blockers=[],
                    next_step=None,
                    next_step_basis="unknown",
                    artifact_refs=[],
                    source_watermark=source_watermark(refs),
                    evidence_refs=refs,
                )
            )
        return consolidation_payload(page, resume_proposals=resumes)

    model = FakeConsolidation(build)
    for _ in range(30):
        if row(core, source)[0] == "done":
            break
        result = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
        assert result.failed == result.retried == 0
    assert row(core, source)[0:2] == ("done", len(source.event["content"]))
    assert seen[0] == (seed, 0) and seen[-1][0] == current and seen[-1][1] > len(seed)
    with closing(sqlite3.connect(core.storage.path)) as db:
        outcomes = db.execute("SELECT disposition,detail FROM consolidation_outcomes").fetchall()
        assert any(outcome == "partial" and "multiple_goals" in detail.split(",") for outcome, detail in outcomes)
        # Capture creates an empty episode; conflicting goals must not populate its resume.
        assert db.execute("SELECT resume_json,processed_sequence FROM episode_versions").fetchall() == [(None, 0)]


def test_chunk_claim_recovery_uses_the_current_page_even_when_an_older_literal_matches(worker_app):
    core, ctx, _clock = worker_app
    current = "TEST-project 配色 蓝色。\n尾注。"
    quoted = encoded(current)
    source = long_source(core, ctx, quoted + "\n" + "这是一段归档资料；" * 700 + "\n" + current)
    quoted_windows = []

    def build(sources, **_kwargs):
        page = sources[0]
        found = []
        if current in page.event["content"]:
            quoted_windows.append(page.consolidation_window)
            found.append(proposal(page, quoted))
        return consolidation_payload(page, claims=found)

    model = FakeConsolidation(build)
    for _ in range(30):
        if row(core, source)[0] == "done":
            break
        result = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
        assert result.failed == result.retried == 0
    assert row(core, source)[0:2] == ("done", len(source.event["content"]))
    assert quoted_windows and quoted_windows[0].start > len(quoted)
    saved = claims(core)
    assert len(saved) == 1 and saved[0][1] == "active"
    assert saved[0][0]["evidence_spans"][0]["quote"] == current
