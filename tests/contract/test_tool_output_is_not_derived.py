"""A tool output is kept and embedded, but never a derivation root (3.2.0rc6).

One 2.5-hour task on the pilot left 787 claims derived from what the agent read or ran -- file
sizes, paths, ports -- and 93% of the store's claims rested on tool output alone, while not one
of the owner's 30 real questions was answered by one.  These tests pin where the rule lives:
admission queues an embedding only, consolidation shows the model no tool output, and work
queued before the change finishes without a model call.  ``test_retire_rootless_claims.py``
covers the claims derived before it.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.core.admission import AdmissionDecision, store_decision
from test_r1_candidate_lifecycle import Evaluator, _candidate_rows, _finish_source_work
from test_v11_claims import app, capture, draft  # noqa: F401 - app is a fixture
from test_v11_worker import Clock, FakeConsolidation, consolidation_payload, procedure_proposal


@pytest.fixture
def worker_app(app):
    core, ctx = app
    core.clock = Clock()
    return core, ctx


def _queued(core, ref):
    with sqlite3.connect(core.storage.path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT work_type FROM work_items WHERE subject_ref=? ORDER BY work_id", (ref,))]


def test_a_tool_output_is_embedded_and_never_shown_to_the_consolidation_model(worker_app):
    core, ctx = worker_app
    said = capture(core, ctx, "TEST 以后导出前先检查透明背景，这是我的决定。")
    # "失败" raises a tool output's priority; it still earns an embedding only.
    read = capture(core, ctx, "TEST 导出失败：透明背景未检查，exit=1", origin="tool_observation")
    assert _queued(core, read.ref) == ["embed"]
    assert _queued(core, said.ref) == ["consolidate", "embed"]
    shown = []

    def builder(sources, episode_ref=None):
        shown.extend(f"{s.ref}@{s.revision}" for s in sources)
        return consolidation_payload(*sources)

    model = FakeConsolidation(builder)
    core.drain_worker(ctx, consolidation=model, max_items=8, remaining_seconds=10)
    assert model.calls >= 1
    assert f"{read.ref}@{read.revision}" not in shown and f"{said.ref}@{said.revision}" in shown


def test_a_consolidation_queued_before_the_change_finishes_without_a_model_call(worker_app):
    core, ctx = worker_app
    read = capture(core, ctx, "TEST 目录里有 42 个文件。", origin="tool_observation")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.enqueue_source(read.ref, read.revision, work_type="consolidate", available_at=core.clock.utc_now())
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        *sources, claims=[procedure_proposal(sources[0])]))
    core.drain_worker(ctx, consolidation=model, max_items=8, remaining_seconds=10)
    with sqlite3.connect(core.storage.path) as conn:
        state = conn.execute("SELECT state FROM work_items WHERE subject_ref=? AND work_type='consolidate'",
                             (read.ref,)).fetchone()[0]
    assert (state, model.calls) == ("done", 0)
    with core.storage.read(ctx) as tx:
        assert not tx.claims.list_refs(predicate="导出方法")


def test_a_deferred_tool_output_settles_on_refill_without_a_consolidation(worker_app):
    core, ctx = worker_app
    read = capture(core, ctx, "TEST 构建日志第 42 行。", origin="tool_observation")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        store_decision(tx, read.ref, read.revision, AdmissionDecision("deferred", "queue_capacity", False))
    resumed = core.resume_deferred(ctx, remaining_seconds=10)
    assert [(item.ref, item.disposition) for item in resumed] == [(read.ref, "unchanged")]
    assert core.resume_deferred(ctx, remaining_seconds=10) == ()
    assert _queued(core, read.ref) == ["embed"]
    receipt = core.schedule_source(ctx, read.ref, read.revision, remaining_seconds=10)
    assert receipt.queued_work == 0 and _queued(core, read.ref) == ["embed"]


# --- the evaluator: the other path by which a claim version is written automatically ---------

def _proposal_from(core, ctx, *sources, value="42GB"):
    """A proposal citing ``sources``, registered with its first evaluation queued."""
    from scope_recall.core.claims import Qualification

    proposal = draft(sources[0], value, kind="fact", subject="entity-disk", predicate="property-disk")
    proposal["evidence_spans"] = [dict(source_ref=s.ref, source_revision=s.revision, quote=s.event["content"])
                                  for s in sources]
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append("TEST-scope", proposal,
                                 Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                 recorded_at=core.clock.utc_now())
        assert tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now()).work_queued
    _finish_source_work(core)
    return saved


def test_an_evaluation_of_a_proposal_tool_output_alone_supports_asks_no_model(app):
    """Queued before 3.2.0 for a proposal derived from tool output, such an evaluation still reached
    the model after the upgrade, and a verdict could make the proposal an active claim."""
    core, ctx = app
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    saved = _proposal_from(core, ctx, read)
    evaluator = Evaluator(proposal=draft(read, "42GB", kind="fact", subject="entity-disk", predicate="property-disk"))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "no_derivation_root")]
    assert [row["state"] for row in work] == ["done"]
    assert core.claim_history(ctx, saved.ref)[-1].state == "proposed"


def test_a_verdict_that_quotes_only_tool_output_writes_no_version(app):
    """A person's proposal whose evaluation also carried a tool output: a verdict quoting only the
    tool output would have written a claim version resting on tool output alone."""
    core, ctx = app
    said = capture(core, ctx, "entity-disk property-disk 42GB。")
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    saved = _proposal_from(core, ctx, said, read)
    evaluator = Evaluator(proposal=draft(read, "42GB", kind="fact", subject="entity-disk", predicate="property-disk"))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert evaluator.calls == 1
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "insufficient_evidence")]
    assert [version.state for version in core.claim_history(ctx, saved.ref)] == ["proposed"]
