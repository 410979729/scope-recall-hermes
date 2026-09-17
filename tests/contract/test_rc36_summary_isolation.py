"""A worker's summary that does not qualify no longer takes the claims beside it down.

On 2026-09-17 beta recorded 208 violations of the resume rules in one day, nearly all goal_authority:
the model named as the user's goal something the user had not asked for. A paged consolidation already
dropped such a summary and kept its claims; a single page rolled the whole result back, paid for a second
model call and, when that answer failed too, lost every claim in the page.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.episodes import source_watermark
from tests.contract.test_v11_claims import app, capture, draft  # noqa: F401  (fixture)
from tests.contract.test_v11_worker import (  # noqa: F401  (fixture)
    FakeConsolidation,
    _mark_embed_done,
    consolidation_payload,
    worker_app,
)


def _goal_from(source):
    refs = [f"{source.ref}@{source.revision}"]
    return dict(episode_ref=None, goal=dict(text=source.event["content"], evidence_refs=refs),
                decisions=[], verified_progress=[], open_items=[], blockers=[], next_step=None,
                next_step_basis="unknown", artifact_refs=[], source_watermark=source_watermark(refs), evidence_refs=refs)


def _stored_values(core):
    with sqlite3.connect(core.storage.path) as db:
        return {json.loads(payload)["value_text"] for (payload,) in db.execute("SELECT payload_json FROM claim_versions")}


def test_an_unqualified_goal_is_dropped_and_the_claims_beside_it_are_kept(worker_app):
    core, ctx, _clock = worker_app
    said = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-rc36/said")
    # A question is never a goal (goal_authority), and both messages share one page.
    asked = capture(core, ctx, "TEST 要不要先把海报整理好？", key="TEST-rc36/asked")
    _mark_embed_done(core)

    def builder(sources, episode_ref=None):
        by_ref = {source.ref: source for source in sources}
        return consolidation_payload(*sources, claims=[draft(by_ref[said.ref], "蓝色")],
                                     resume_proposals=[_goal_from(by_ref[asked.ref])])

    model = FakeConsolidation(builder)
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=10)

    assert receipt.completed == 1 and model.calls == 1
    assert "蓝色" in _stored_values(core)
    with sqlite3.connect(core.storage.path) as db:
        errors = db.execute("SELECT stage,error_code,error_field FROM work_error_details").fetchall()
        outcomes = db.execute("SELECT disposition,detail FROM consolidation_outcomes").fetchall()
    assert errors == [("summary", "DERIVATION_INVALID", "goal_authority")]
    assert outcomes == [("partial", "resume_qualification_failed")]


def test_a_result_submitted_outside_the_worker_is_still_rejected_whole(app):
    core, ctx = app
    said = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-rc36/direct-said")
    asked = capture(core, ctx, "TEST 要不要先把海报整理好？", key="TEST-rc36/direct-asked")
    value = consolidation_payload(said, asked, claims=[draft(said, "蓝色")], resume_proposals=[_goal_from(asked)])

    with pytest.raises(ContractError, match="goal_authority"):
        core.accept_consolidation(ctx, value, scope_id="TEST-scope", remaining_seconds=10)

    assert "蓝色" not in _stored_values(core)
