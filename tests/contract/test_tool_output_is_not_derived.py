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

DISK = dict(kind="fact", subject="entity-disk", predicate="property-disk")


def _proposal_from(core, ctx, *sources, value="42GB"):
    """A proposal citing ``sources``, registered as the worker would register it."""
    from scope_recall.core.claims import Qualification

    proposal = draft(sources[0], value, **DISK)
    proposal["evidence_spans"] = [dict(source_ref=s.ref, source_revision=s.revision, quote=s.event["content"])
                                  for s in sources]
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append("TEST-scope", proposal,
                                 Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                 recorded_at=core.clock.utc_now())
        registration = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)
    return saved, registration


def _span(source, quote):
    assert quote in source.event["content"]
    return dict(source_ref=source.ref, source_revision=source.revision, quote=quote)


def test_a_proposal_tool_output_alone_supports_is_never_put_to_the_model(app):
    """Until 3.2.0 such a proposal was queued for evaluation, and a verdict could make it active."""
    core, ctx = app
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    saved, registration = _proposal_from(core, ctx, read)
    assert not registration.work_queued
    evaluator = Evaluator(proposal=draft(read, "42GB", **DISK))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert (evaluator.calls, work) == (0, [])
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "no_derivation_root")]
    assert core.claim_history(ctx, saved.ref)[-1].state == "proposed"


def test_an_evaluation_queued_before_the_upgrade_settles_without_a_model_call(app, monkeypatch):
    """Queued under the old rule, it reached the model after the upgrade and could promote the proposal."""
    import scope_recall.core.candidate_intake as intake

    core, ctx = app
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    with monkeypatch.context() as old_rule:
        old_rule.setattr(intake, "unanswerable_reason", lambda payload, evidence: None)
        old_rule.setattr(intake, "rootless", lambda cited, evidence: None)
        saved, registration = _proposal_from(core, ctx, read)
    assert registration.work_queued
    evaluator = Evaluator(proposal=draft(read, "42GB", **DISK))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0 and [row["state"] for row in work] == ["done"]
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "no_derivation_root")]
    assert core.claim_history(ctx, saved.ref)[-1].state == "proposed"


@pytest.mark.parametrize("beside_a_person", [False, True])
def test_a_verdict_whose_value_only_tool_output_carries_writes_no_version(app, beside_a_person):
    """A person's message beside a tool output: a verdict quoting the value from the tool output, alone
    or with any fragment of the person's message, would have been written as the person's own report."""
    core, ctx = app
    said = capture(core, ctx, "entity-disk property-disk 42GB。另外 entity-disk 该清理了。")
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    saved, registration = _proposal_from(core, ctx, said, read)
    assert registration.work_queued, "a person's words carry the value: the question is worth asking"
    spans = [_span(read, read.event["content"])]
    if beside_a_person:
        spans.append(_span(said, "另外 entity-disk 该清理了"))
    evaluator = Evaluator(proposal=dict(draft(read, "42GB", **DISK), evidence_spans=spans))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert evaluator.calls == 1
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "insufficient_evidence")]
    assert [version.state for version in core.claim_history(ctx, saved.ref)] == ["proposed"]


def test_a_verdict_on_a_persons_own_words_still_promotes(app):
    core, ctx = app
    said = capture(core, ctx, "entity-disk property-disk 42GB。")
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin="tool_observation")
    saved, _registration = _proposal_from(core, ctx, said, read)
    evaluator = Evaluator(proposal=dict(draft(said, "42GB", **DISK), evidence_spans=[_span(said, said.event["content"])]))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert core.claim_history(ctx, saved.ref)[-1].state == "active"
