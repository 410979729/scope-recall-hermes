"""A tool output is kept and embedded, but never a derivation root.

Claims derived from what the agent read or ran -- file sizes, paths, ports -- rest on tool output
alone and answer none of the owner's real questions.  These tests pin where the rule lives:
admission queues an embedding only, consolidation shows the model no tool output, and work
queued by an earlier release finishes without a model call.  ``test_retire_rootless_claims.py``
covers the claims derived before the rule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from scope_recall.core.admission import AdmissionDecision, store_decision, wanted_work_types
from scope_recall.core.memory_import import ImportRecord, import_record
from test_autonomous_admission import app_at, complete_work
from test_candidate_lifecycle import Evaluator, _candidate_rows, _finish_source_work
from test_claims import app, capture, draft  # noqa: F401 - app is a fixture
from test_worker import Clock, FakeConsolidation, consolidation_payload, procedure_proposal
from v11_support import source_event


@pytest.fixture
def worker_app(app):
    core, ctx = app
    core.clock = Clock()
    return core, ctx


def _queued(core, ref):
    with sqlite3.connect(core.storage.path) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT work_type FROM work_items WHERE subject_ref=? ORDER BY work_id", (ref,))
        ]


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible", "host_generated"])
def test_a_non_root_source_is_embedded_and_never_shown_to_the_consolidation_model(worker_app, origin):
    core, ctx = worker_app
    said = capture(core, ctx, "TEST 以后导出前先检查透明背景，这是我的决定。")
    # Priority changes queue access, never whether an agent source can derive a claim.
    read = capture(core, ctx, "TEST 导出失败：透明背景未检查，exit=1", origin=origin)
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


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible", "host_generated"])
def test_a_consolidation_queued_before_the_change_is_obsolete_without_a_model_call(worker_app, origin):
    core, ctx = worker_app
    read = capture(core, ctx, "TEST 目录里有 42 个文件。", origin=origin)
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.sources.enqueue_source(read.ref, read.revision, work_type="consolidate", available_at=core.clock.utc_now())
    model = FakeConsolidation(
        lambda sources, episode_ref=None: consolidation_payload(*sources, claims=[procedure_proposal(sources[0])])
    )
    core.drain_worker(ctx, consolidation=model, max_items=8, remaining_seconds=10)
    with sqlite3.connect(core.storage.path) as conn:
        state, error, work_id = conn.execute(
            "SELECT state,last_error_code,work_id FROM work_items WHERE subject_ref=? AND work_type='consolidate'",
            (read.ref,),
        ).fetchone()
        reason = conn.execute("SELECT error_code FROM work_error_details WHERE work_id=?", (work_id,)).fetchone()[0]
    # The terminal work code retains its compatibility contract; the detail says
    # why the source is ineligible without pretending it was deleted or suppressed.
    assert (state, error, model.calls) == ("obsolete", "authority_revoked", 0)
    assert reason == "no_derivation_root"
    assert core.source(ctx, read.ref, read.revision).event["content"] == read.event["content"]
    with core.storage.read(ctx) as tx:
        assert not tx.claims.list_refs(predicate="导出方法")


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible", "host_generated"])
def test_a_deferred_non_root_source_settles_on_refill_without_a_consolidation(worker_app, origin):
    core, ctx = worker_app
    read = capture(core, ctx, "TEST 构建日志第 42 行。", origin=origin)
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        store_decision(tx, read.ref, read.revision, AdmissionDecision("deferred", "queue_capacity", False))
    resumed = core.records.resume_deferred(ctx, remaining_seconds=10)
    assert [(item.ref, item.disposition) for item in resumed] == [(read.ref, "unchanged")]
    assert core.records.resume_deferred(ctx, remaining_seconds=10) == ()
    assert _queued(core, read.ref) == ["embed"]
    receipt = core.records.schedule_source(ctx, read.ref, read.revision, remaining_seconds=10)
    assert receipt.queued_work == 0 and _queued(core, read.ref) == ["embed"]


@pytest.mark.parametrize("origin", ["human_direct", "external_document"])
def test_derivation_roots_keep_their_derived_work(origin):
    assert wanted_work_types({"origin": origin, "role": "document"}) == {"consolidate", "embed"}


@pytest.mark.parametrize(
    ("role", "original", "expected"),
    [
        ("tool", "tool_observation", {"embed"}),
        ("assistant", "assistant_visible", {"embed"}),
        ("user", "human_direct", {"consolidate", "embed"}),
    ],
)
def test_imported_sources_are_scheduled_by_original_origin(worker_app, role, original, expected):
    core, ctx = worker_app
    event = source_event(
        source_event_key="TEST-imported",
        origin="imported",
        role=role,
        source_original_origin=original,
        content="TEST substantive imported message",
    )
    assert wanted_work_types(event) == expected
    saved = core.record_event(replace(ctx, actor_origin="imported"), event, scope_id="TEST-scope", remaining_seconds=10)
    assert set(_queued(core, saved.event_refs[0].ref)) == expected


def test_import_record_preserves_human_derived_work(worker_app):
    core, ctx = worker_app
    item = ImportRecord("TEST-human", "TEST substantive human message", "user", original_origin="human_direct")
    saved = import_record(core, ctx, item, source="chatgpt", source_id="TEST-account", scope_id="TEST-scope")
    assert set(_queued(core, saved.event_refs[0].ref)) == {"consolidate", "embed"}


@pytest.mark.parametrize("original", ["tool_observation", "assistant_visible"])
def test_deferred_imported_non_root_does_not_hold_refill_page(tmp_path, original):
    from scope_recall.core.admission import AdmissionPolicy
    from test_autonomous_admission import capture as admission_capture

    core, ctx = app_at(tmp_path, AdmissionPolicy(max_pending_work=2, important_reserve=0))
    admission_capture(core, ctx, "TEST-fill", "TEST fills both work queues")
    event = source_event(
        source_event_key="TEST-import",
        origin="imported",
        role="tool" if original == "tool_observation" else "assistant",
        source_original_origin=original,
        content="TEST imported substantive source",
    )
    saved = core.record_event(replace(ctx, actor_origin="imported"), event, scope_id="TEST-scope", remaining_seconds=10)
    assert saved.admission == ("admission_deferred:queue_capacity",)
    complete_work(core, ctx, core.clock, allowed_work_types=frozenset({"consolidate"}))
    assert core.records.resume_deferred(ctx, limit=1, remaining_seconds=10) == ()
    assert _queued(core, saved.event_refs[0].ref) == []
    complete_work(core, ctx, core.clock, allowed_work_types=frozenset({"embed"}))
    resumed = core.records.resume_deferred(ctx, limit=1, remaining_seconds=10)
    assert [(item.ref, item.disposition) for item in resumed] == [(saved.event_refs[0].ref, "scheduled")]
    assert _queued(core, saved.event_refs[0].ref) == ["embed"]
    later = admission_capture(core, ctx, "TEST-later", "TEST another human message")
    assert _queued(core, later.event_refs[0].ref) == ["consolidate"]


# --- the evaluator: the other path by which a claim version is written automatically ---------

DISK = dict(kind="fact", subject="entity-disk", predicate="property-disk")


def _proposal_from(core, ctx, *sources, value="42GB"):
    """A proposal citing ``sources``, registered as the worker would register it."""
    from scope_recall.core.claims import Qualification

    proposal = draft(sources[0], value, **DISK)
    proposal["evidence_spans"] = [
        dict(source_ref=s.ref, source_revision=s.revision, quote=s.event["content"]) for s in sources
    ]
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        registration = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)
    return saved, registration


def _span(source, quote):
    assert quote in source.event["content"]
    return dict(source_ref=source.ref, source_revision=source.revision, quote=quote)


def test_a_proposal_tool_output_alone_supports_is_never_put_to_the_model(app):
    """Such a proposal is not queued for evaluation, so no verdict can make it active."""
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
    """Queued under an earlier release's rule, it must not reach the model after the upgrade, where it could promote
    the proposal."""
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
    evaluator = Evaluator(
        proposal=dict(draft(said, "42GB", **DISK), evidence_spans=[_span(said, said.event["content"])])
    )
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert core.claim_history(ctx, saved.ref)[-1].state == "active"


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible"])
def test_positioned_evaluation_cannot_borrow_a_value_from_an_agent_source(app, origin):
    core, ctx = app
    said = capture(core, ctx, "entity-disk property-disk 42GB。另外 entity-disk 该清理了。")
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin=origin)
    saved, registration = _proposal_from(core, ctx, said, read)
    assert registration.work_queued
    spans = [_span(read, read.event["content"]), _span(said, "另外 entity-disk 该清理了")]
    evaluator = Evaluator(proposal=dict(draft(read, "42GB", **DISK), evidence_spans=spans))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert [(v.revision, v.state) for v in core.claim_history(ctx, saved.ref)] == [(1, "proposed")]
    _, evaluations, _ = _candidate_rows(core)
    assert [(row["state"], row["reason"]) for row in evaluations] == [("waiting_evidence", "insufficient_evidence")]


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible"])
def test_consolidation_cannot_publish_an_agent_quote_beside_a_person(worker_app, origin):
    core, ctx = worker_app
    said = capture(core, ctx, "entity-disk property-disk 42GB。另外 entity-disk 该清理了。")
    read = capture(core, ctx, "entity-disk property-disk 42GB。", origin=origin)
    claim = dict(
        draft(read, "42GB", **DISK),
        evidence_spans=[_span(read, read.event["content"]), _span(said, "另外 entity-disk 该清理了")],
    )
    model = FakeConsolidation(lambda sources, **kw: consolidation_payload(*sources, claims=[claim]))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=model)
    assert model.calls >= 1
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM claim_versions").fetchone()[0] == 0
        assert conn.execute("SELECT error_field FROM work_error_details WHERE stage='claim'").fetchall() == [
            ("claim_proposals[0].evidence_spans[0].source_ref",)
        ]
