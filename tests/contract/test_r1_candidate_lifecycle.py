"""R1 candidate lifecycle, bounded scheduling and migration contracts."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core.candidate_lifecycle import (
    candidate_evaluation_messages,
    candidate_subject_matches,
)
from scope_recall.core.claims import Qualification
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.maintenance import doctor
from scope_recall.runtime.scheduling import next_wake
from test_finite_supervisor import NOW, fixture as supervisor_fixture, queue as queue_work
from test_v11_claims import app, capture, draft
from test_v11_deletion import authorize, request


def _settle_evidence(core, *, seconds=None):
    """Back-date evidence arrival so the quiet window has elapsed.

    The test clock does not advance on its own and waiting fifteen real minutes
    is not a test.  Only the timestamps the debounce policy reads are moved;
    nothing else about the candidate changes.
    """
    from datetime import datetime, timedelta, timezone

    from scope_recall.core.candidate_debounce import QUIET_SECONDS

    elapsed = QUIET_SECONDS + 60 if seconds is None else seconds
    # Relative to the fixture clock, which is frozen: wall-clock time is days
    # away from it and would read as evidence arriving in the future.
    frozen = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00"))
    moment = (frozen - timedelta(seconds=elapsed)).isoformat()
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET last_evidence_at=?", (moment,))
        conn.commit()


def _sweep(core, ctx, *, limit=16):
    with core.storage.write(ctx) as tx:
        return tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=limit)


def _snapshot(core, ref, revision):
    """Rebuild the snapshot the repair routes pass to ``_schedule``.

    Read straight from the two tables production reads, so the test exercises
    the real path rather than a convenience accessor that only tests use.
    """
    from scope_recall.core.candidate_lifecycle import CandidateSnapshot

    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT l.scope_id,l.project_id,l.branch_id,l.processing_state,l.reason,l.rule_version,
                      v.state AS fact_state,v.payload_json
               FROM candidate_lifecycle l
               JOIN claim_versions v ON v.claim_id=l.candidate_ref AND v.revision=l.candidate_revision
               WHERE l.candidate_ref=? AND l.candidate_revision=?""",
            (ref, revision),
        ).fetchone()
    return CandidateSnapshot(ref, revision, row["scope_id"], row["project_id"], row["branch_id"],
                             row["fact_state"], json.loads(row["payload_json"]),
                             row["processing_state"], row["reason"], row["rule_version"])


def _candidate(core, ctx, *, value="蓝色", key=None):
    slug = key.rsplit("/", 1)[-1] if key else "blue"
    subject = f"entity-{slug}"
    predicate = f"property-{slug}"
    source = capture(core, ctx, f"{subject} {predicate} {value}。", key=key)
    proposal = draft(source, value, subject=subject, predicate=predicate)
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        registration = tx.candidates.register(
            saved.ref, saved.revision, observed_at=core.clock.utc_now(),
        )
    return saved, source, proposal, registration


def _finish_source_work(core):
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('consolidate','embed')")
        conn.commit()


def _candidate_rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        lifecycle = [dict(row) for row in conn.execute(
            "SELECT * FROM candidate_lifecycle ORDER BY candidate_ref,candidate_revision"
        )]
        evaluations = [dict(row) for row in conn.execute(
            "SELECT * FROM candidate_evaluations ORDER BY evaluation_id"
        )]
        work = [dict(row) for row in conn.execute(
            "SELECT * FROM work_items WHERE work_type='evaluate_candidate' ORDER BY work_id"
        )]
    return lifecycle, evaluations, work


class Evaluator:
    def __init__(self, proposal=None, callback=None):
        self.proposal = proposal
        self.callback = callback
        self.calls = 0

    def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
        self.calls += 1
        if self.callback is not None:
            self.callback()
        return json.dumps({
            "protocol_version": "1.1",
            "source_refs": [f"{source.ref}@{source.revision}" for source in sources],
            "claim_proposals": [] if self.proposal is None else [self.proposal],
            "resume_proposals": [],
            "reference_proposals": [],
        }, ensure_ascii=False)


class ModelRefusal(Exception):
    def __init__(self, error_type):
        super().__init__(error_type)
        self.error_type = error_type


def test_r1_candidate_registration_separates_fact_state_and_dedupes(app):
    core, ctx = app
    saved, _source, _proposal, registration = _candidate(core, ctx)
    assert saved.state == "proposed"
    assert registration.processing_state == "pending_evaluation"
    assert registration.work_queued is True
    with core.storage.write(ctx) as tx:
        repeated = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
        summary = tx.candidates.summary()
    lifecycle, evaluations, work = _candidate_rows(core)
    assert repeated.work_queued is False
    assert summary.pending_evaluation == 1
    assert lifecycle[0]["processing_state"] == "pending_evaluation"
    assert len(evaluations) == len(work) == 1
    assert evaluations[0]["work_id"] == work[0]["work_id"]
    assert work[0]["subject_ref"] == "candidate:" + saved.ref
    assert work[0]["subject_revision"] == evaluations[0]["evaluation_id"]


def test_r1_candidate_new_evidence_wakes_once_and_duplicate_capture_does_not_expand_work(app):
    """Arriving evidence is recorded; it no longer mints an evaluation each time.

    Scheduling per arrival is what produced 7,802 retired-before-judged
    evaluations on alpha.  The evidence is still collected the moment it
    arrives -- nothing is lost by waiting -- and one evaluation is scheduled
    once the candidate settles.
    """
    core, ctx = app
    _candidate(core, ctx)
    first = capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-r1/new-evidence")
    replay = capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-r1/new-evidence")
    assert replay.ref == first.ref
    lifecycle, evaluations, work = _candidate_rows(core)
    assert len(evaluations) == len(work) == 1, "the arriving source did not queue a second evaluation"
    assert lifecycle[0]["reason"] == "new_evidence"
    with sqlite3.connect(core.storage.path) as conn:
        recorded = conn.execute("SELECT count(*) FROM candidate_evidence").fetchone()[0]
    assert recorded == 2, "but the evidence itself was recorded"

    # The evidence that arrived while the first evaluation was queued is judged
    # in the next round, not by displacing the one already waiting.
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    _settle_evidence(core)
    assert _sweep(core, ctx) == 1
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert len(evaluations) == len(work) == 2
    assert evaluations[0]["evidence_fingerprint"] != evaluations[1]["evidence_fingerprint"]
    assert len(json.loads(evaluations[1]["evidence_refs_json"])) == 2
    assert _sweep(core, ctx) == 0, "and an unchanged evidence set schedules nothing further"


def test_r1_candidate_worker_applies_through_fact_path_without_manual_approval(app):
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator(proposal)
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    current = core.current_claim(ctx, saved.ref)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 1
    candidate_items = [item for item in receipt.items if item.work_type == "evaluate_candidate"]
    assert len(candidate_items) == 1 and candidate_items[0].disposition == "completed"
    assert current.state == "active" and current.revision == 2
    assert evaluations[0]["state"] == "resolved"
    assert work[0]["state"] == "done"
    assert any(row["candidate_revision"] == 2 and row["processing_state"] == "resolved" for row in lifecycle)


def test_candidate_queued_before_unrelated_write_uses_current_attempt_epoch(app):
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)
    capture(core, ctx, "无关项目今天开会。", key="TEST-r1/unrelated")
    _finish_source_work(core)
    with core.storage.read(ctx) as tx:
        epoch = tx.status().memory_epoch
    evaluator = Evaluator(proposal)
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert result.completed == 1
    assert core.current_claim(ctx, saved.ref).state == "active"
    _, evaluations, _ = _candidate_rows(core)
    assert evaluations[0]["memory_epoch"] == epoch


def test_candidate_unrelated_capture_during_attempt_still_applies_verdict(app):
    """An unrelated capture during the attempt no longer voids the paid verdict.

    This was ``test_candidate_epoch_change_during_attempt_still_blocks_publication``:
    the capture moved ``memory_epoch`` and the evaluation was made obsolete
    with ``memory_epoch_changed`` although neither its evidence nor its
    candidate head changed.  The verdict is now applied.
    """
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator(proposal, callback=lambda: capture(
        core, ctx, "另一个项目刚更新。", key="TEST-r1/during-model"))
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert result.completed == 1 and result.obsolete == 0
    assert result.items[0].error_code is None
    assert core.current_claim(ctx, saved.ref).state == "active"
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert evaluations[0]["state"] == "resolved"


def test_candidate_evidence_suppressed_during_attempt_blocks_publication(app):
    core, ctx = app
    saved, source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)

    def suppress_evidence():
        authorize(core, ctx, source, mode="suppress")
        core.forget(ctx, request(source, mode="suppress"), remaining_seconds=10)

    evaluator = Evaluator(proposal, callback=suppress_evidence)
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert result.completed == 0 and result.stale == 1
    assert (result.items[0].state, result.items[0].error_code) == ("obsolete", None)
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert (evaluations[0]["state"], evaluations[0]["reason"]) == ("obsolete", "authority_revoked")
    with core.storage.read(ctx) as tx:
        assert [version.state for version in tx.claims.versions(saved.ref)] == ["proposed"]


def test_candidate_slot_written_during_attempt_blocks_publication(app):
    """A version recorded in the candidate's claim during the attempt voids the verdict.

    The head is untouched -- the version is historical -- so the evaluation
    itself is still current; only the claim-slot check sees the change.
    """
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)

    def write_history():
        with core.storage.write(ctx) as tx:
            head = tx.claims.version(saved.ref, saved.revision)
            tx.claims.append("TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_history"),
                             recorded_at=core.clock.utc_now(), previous=head, advance_head=False)

    evaluator = Evaluator(proposal, callback=write_history)
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert result.completed == 0 and result.obsolete == 1
    assert result.items[0].error_code == "memory_epoch_changed"
    with core.storage.read(ctx) as tx:
        versions = tx.claims.versions(saved.ref)
    assert [(version.state, version.current_revision) for version in versions] == [("proposed", 1), ("proposed", 1)]


def test_candidate_deletion_in_scope_during_attempt_blocks_publication(app):
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)
    other = capture(core, ctx, "TEST 与候选无关的记录。", key="TEST-r1/delete-other")
    _finish_source_work(core)

    def delete_other():
        authorize(core, ctx, other)
        core.forget(ctx, request(other), remaining_seconds=10)

    evaluator = Evaluator(proposal, callback=delete_other)
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1
    assert result.completed == 0 and result.obsolete == 1
    assert result.items[0].error_code == "memory_epoch_changed"
    with core.storage.read(ctx) as tx:
        assert [version.state for version in tx.claims.versions(saved.ref)] == ["proposed"]


def test_r1_candidate_hides_c1_principal_from_model_and_rebinds_via_c2(app):
    core, ctx = app
    principal_ref = "principal:TEST-private-alice"
    actor = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref=principal_ref, display_name="Alice",
        ),
    )
    source = capture(core, actor, "我喜欢蓝色。", key="TEST-r1/private-principal")
    stored = {
        "kind": "preference",
        "subject": principal_ref,
        "predicate": "喜欢",
        "value_text": "蓝色",
        "conditions": [],
        "statement_kind": "assertion",
        "valid_from": source.event["occurred_at"],
        "valid_to": None,
        "evidence_spans": [{
            "source_ref": source.ref,
            "source_revision": source.revision,
            "quote": source.event["content"],
        }],
    }
    with core.storage.write(actor) as tx:
        candidate = tx.claims.append(
            "TEST-scope", stored,
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        registration = tx.candidates.register(
            candidate.ref, candidate.revision, observed_at=core.clock.utc_now(),
        )
        evaluation = tx.candidates.evaluation(registration.evaluation_id)
    assert evaluation is not None
    messages = candidate_evaluation_messages(evaluation.candidate, (source,))
    model_input = json.dumps(messages, ensure_ascii=False)
    assert principal_ref not in model_input
    assert json.loads(messages[-1]["content"].removeprefix("candidate="))["subject"] == "current_user"
    assert candidate_subject_matches(evaluation.candidate, (source,), "current_user")
    assert not candidate_subject_matches(evaluation.candidate, (source,), principal_ref)

    proposal = dict(stored, subject="current_user")
    _finish_source_work(core)
    evaluator = Evaluator(proposal)
    receipt = core.drain_worker(
        actor, max_items=8, remaining_seconds=10, consolidation=evaluator,
    )
    current = core.current_claim(ctx, candidate.ref)
    candidate_items = [item for item in receipt.items if item.work_type == "evaluate_candidate"]
    assert len(candidate_items) == 1 and candidate_items[0].disposition == "completed"
    assert evaluator.calls == 1
    assert current is not None and current.state == "active"
    assert current.payload["subject"] == principal_ref


def test_r1_candidate_no_new_evidence_means_no_second_model_attempt(app):
    core, ctx = app
    saved, _source, _proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator()
    first = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    second = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    summary = None
    with core.storage.write(ctx) as tx:
        replay = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
        summary = tx.candidates.summary()
    assert first.completed == 1 and second.processed == 0
    assert evaluator.calls == 1
    assert summary.waiting_evidence == 1
    assert replay.disposition == "unchanged" and replay.work_queued is False


def test_r1_candidate_batch_stops_at_the_pass_bound_and_persists_remainder(app):
    """A pass takes what it is allowed and leaves the rest standing.

    The eight-per-pass batch limit still holds candidates behind conversation
    work, and now lifts once nothing else is waiting, because a limit meant to
    protect that work was also keeping a backlog alive: see
    tests/contract/test_rc38_queue_backpressure.py.  What a pass does not reach
    is still exactly where it was.
    """
    core, ctx = app
    for index in range(10):
        _candidate(core, ctx, value=f"value{index}", key=f"TEST-r1/batch/{index}")
    _finish_source_work(core)
    evaluator = Evaluator()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=20, consolidation=evaluator)
    _lifecycle, _evaluations, work = _candidate_rows(core)
    assert evaluator.calls == receipt.processed == 8
    assert sum(row["state"] == "pending" for row in work) == 2


def test_r1_candidate_source_matching_is_capped_at_sixteen(app):
    core, ctx = app
    sources = [
        capture(core, ctx, f"entity{i} property{i} sharedtoken value{i}。", key=f"TEST-r1/match/{i}")
        for i in range(20)
    ]
    with core.storage.write(ctx) as tx:
        for index, source in enumerate(sources):
            proposal = draft(
                source, f"sharedtoken value{index}", subject=f"entity{index}", predicate=f"property{index}",
            )
            saved = tx.claims.append(
                "TEST-scope", proposal,
                Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                recorded_at=core.clock.utc_now(),
            )
            tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-r1/match-trigger")
    with sqlite3.connect(core.storage.path) as conn:
        row = conn.execute(
            """SELECT matched_count,scheduled_count,truncated FROM candidate_source_triggers
               WHERE source_ref=? AND source_revision=1""", (trigger.ref,),
        ).fetchone()
    # matched and truncated are the cap under test.  scheduled is now zero for
    # two independent reasons -- the evidence has only just arrived, and each of
    # these candidates already carries the evaluation it was registered with --
    # and either one alone is enough to keep the queue from multiplying.
    assert row == (16, 0, 1)
    _settle_evidence(core)
    assert _sweep(core, ctx, limit=16) == 0, "a live evaluation is never displaced"


def test_r1_candidate_budget_pause_is_explicit_and_does_not_spend_attempt(app):
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)

    class Refusing:
        calls = 0

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            self.calls += 1
            raise ModelRefusal("budget_exhausted")

    evaluator = Refusing()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    with core.storage.read(ctx) as tx:
        summary = tx.candidates.summary()
    assert receipt.deferred == 1 and evaluator.calls == 1
    assert lifecycle[0]["reason"] == "budget_paused"
    assert evaluations[0]["model_attempted_at"] is None
    assert work[0]["state"] == "pending" and work[0]["attempt"] == 0
    assert summary.budget_paused == 1


@pytest.mark.parametrize("status", ["401", "402", "403"])
def test_an_account_refusal_parks_candidates_instead_of_failing_them(app, status):
    """A DeepSeek balance that ran out answered 402 for fifteen minutes and
    failed 100 evaluations outright on alpha.  The provider refused the
    account, not the question: park it, hand the attempt back, and stop asking
    for the rest of the pass."""
    from scope_recall.adapters.models import AuxiliaryModelError

    core, ctx = app
    _candidate(core, ctx)
    _candidate(core, ctx, value="绿色", key="TEST-r1/green")
    _finish_source_work(core)

    class Refusing:
        calls = 0

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            self.calls += 1
            raise AuxiliaryModelError("http_status", detail=status)

    evaluator = Refusing()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert len(work) == 2, "both candidates should have been queued for the pass"
    assert evaluator.calls == 1, "a refused account was asked again in the same pass"
    assert receipt.deferred == 1 and receipt.failed == 0
    assert not any(row["state"] == "failed" for row in work)
    refused = [row for row in work if row["attempt"] == 0 and row["last_error_code"] == f"http_{status}"]
    assert len(refused) == 1 and refused[0]["state"] == "pending"
    assert all(row["model_attempted_at"] is None for row in evaluations)
    assert {row["reason"] for row in lifecycle} >= {"budget_paused"}


def test_r1_candidate_failed_combination_rejects_operator_retry_and_needs_new_evidence(app):
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)

    class Failing:
        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            raise ModelRefusal("network_error")

    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Failing())
    _lifecycle, _evaluations, work = _candidate_rows(core)
    assert receipt.failed == 1 and work[0]["state"] == "failed"
    with core.storage.write(ctx) as tx:
        retried = tx.work.operator_retry_failed(
            [work[0]["work_id"]], now=core.clock.utc_now(), operation_id="TEST-r1-retry",
        )
    assert retried[0].disposition == "rejected"
    assert retried[0].reason == "new_evidence_required"
    capture(core, ctx, "entity-blue property-blue 带来真正的新证据。", key="TEST-r1/recover-evidence")
    _settle_evidence(core)
    assert _sweep(core, ctx) == 1
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert len(evaluations) == len(work) == 2
    assert work[0]["state"] == "failed" and work[1]["state"] == "pending"


def test_r1_candidate_interrupted_attempt_never_calls_model_again(app):
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    with core.storage.write(ctx) as tx:
        item = tx.work.claim_next(
            "TEST-crashed", core.clock.utc_now(), lease_seconds=60, limit=1,
            allowed_work_types=frozenset({"evaluate_candidate"}),
        )[0]
        assert tx.candidates.begin_model_attempt(
            item.subject_revision, item.work_id, item.lease_token, item.lease_owner,
            now=core.clock.utc_now(),
        )
        tx._check(write=True).execute(
            "UPDATE work_items SET lease_until='2026-09-06T11:00:00Z' WHERE work_id=?", (item.work_id,),
        )
    evaluator = Evaluator()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0
    assert receipt.failed == 1
    assert evaluations[0]["failure_code"] == "candidate_attempt_interrupted"
    assert work[0]["state"] == "failed"


def test_r1_candidate_dormancy_is_recoverable_but_active_fact_is_untouched(app):
    core, ctx = app
    candidate, _source, _proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    core.clock.now = "2026-10-07T12:00:00Z"
    core.drain_worker(ctx, max_items=8, remaining_seconds=10)
    lifecycle, _evaluations, _work = _candidate_rows(core)
    dormant = next(row for row in lifecycle if row["candidate_ref"] == candidate.ref)
    assert dormant["processing_state"] == "archived"
    assert dormant["reason"] == "dormant_no_evidence"
    capture(core, ctx, "entity-blue property-blue 有一条新的相关证据。", key="TEST-r1/wake")
    with core.storage.read(ctx) as tx:
        assert tx.candidates.summary().pending_evaluation == 1


def test_r1_candidate_delete_during_model_call_blocks_publish(app):
    core, ctx = app
    candidate, source, proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)

    def delete_source():
        authorize(core, ctx, source)
        core.forget(ctx, request(source), remaining_seconds=10)

    evaluator = Evaluator(proposal, callback=delete_source)
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 1
    assert receipt.completed == 0
    assert core.current_claim(ctx, candidate.ref) is None
    assert lifecycle[0]["processing_state"] == "blocked"
    assert all(row["state"] == "obsolete" for row in evaluations)
    assert all(row["state"] == "obsolete" for row in work)


def test_r1_candidate_doctor_reports_waiting_capability_and_failures(app, monkeypatch):
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10)
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert result.candidate_pending_evaluation == 1
    assert result.candidate_capability_unavailable == 1
    assert result.candidate_oldest_waiting_at is not None
    assert "candidate_capability_unavailable" in result.capability_gaps
    payload = result.to_dict()
    assert payload["candidate_pending_evaluation"] == 1


def test_r1_candidate_scheduler_distinguishes_missing_capability_and_daily_budget(tmp_path):
    core, config, _path = supervisor_fixture(tmp_path)
    queue_work(core, config, ref="candidate:TEST", kind="evaluate_candidate", due=NOW)
    blocked = next_wake(config, now=NOW)
    assert blocked.due_at is None and blocked.reason == "capability_unavailable" and blocked.blocked == 1
    capable = replace(
        config,
        # daily_work_limit defaults to 0, meaning uncapped; a daily pause can only
        # be observed against a cap that was actually asked for.
        daily_work_limit=256,
        auxiliary=replace(config.auxiliary, external_consolidation=True, consolidation=object()),
    )
    ready = next_wake(capable, now=NOW)
    assert ready.due_at == "2026-09-12T00:00:00Z" and ready.reason == "work_available"
    budget = capable.binding.data_directory / "runtime-worker-day.json"
    budget.write_text(json.dumps({
        "installation_id": capable.binding.installation_id,
        "day": "2026-09-12",
        "used": capable.daily_work_limit,
    }), encoding="utf-8")
    paused = next_wake(capable, now=NOW)
    assert paused.due_at == "2026-09-13T00:00:00Z" and paused.reason == "daily_queue_budget"
    # The same spent counter, uncapped: candidate work stays due instead of
    # being pushed to tomorrow.
    uncapped = next_wake(replace(capable, daily_work_limit=0), now=NOW)
    assert uncapped.reason != "daily_queue_budget"


def test_r1_candidate_1107_migration_preserves_work_ids_leases_and_error_history(app, monkeypatch):
    core, ctx = app
    source = capture(core, ctx, "TEST migration durable work。", key="TEST-r1/migration")
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        work_id = conn.execute(
            "SELECT work_id FROM work_items WHERE subject_ref=? AND work_type='consolidate'", (source.ref,)
        ).fetchone()[0]
        conn.execute(
            """UPDATE work_items SET state='leased',attempt=2,lease_token=7,lease_owner='TEST-owner',
               lease_until='2099-01-01T00:00:00Z',last_error_code='held' WHERE work_id=?""", (work_id,)
        )
        conn.execute(
            """INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
               VALUES (?,7,'TEST','held','field','2026-09-06T12:00:00Z')""", (work_id,)
        )
        conn.execute(
            """INSERT INTO capture_inbox(token,scope_id,project_id,branch_id,created_at,payload_json,last_error_code)
               VALUES ('TEST-inbox','TEST-scope','TEST-project','TEST-main','2026-09-06T12:00:00Z','{}','held')"""
        )
        for table in (
            "candidate_source_triggers", "candidate_evaluations", "candidate_trigger_terms",
            "candidate_evidence", "candidate_lifecycle", "candidate_scan_cursors", "expired_vectors",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE instance_meta SET schema_version=1107 WHERE singleton=1")
        conn.execute("PRAGMA user_version=1107")
        conn.commit()

    import scope_recall.core.schema as schema_module
    import scope_recall.core.storage as storage_module
    original = schema_module.upgrade_1107

    def fail_after_upgrade(connection):
        original(connection)
        raise RuntimeError("TEST migration rollback")

    monkeypatch.setattr(storage_module, "upgrade_1107", fail_after_upgrade)
    with pytest.raises(RuntimeError, match="migration rollback"):
        core.initialize()
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1107
        assert conn.execute("SELECT state,attempt,lease_token,lease_owner,last_error_code FROM work_items WHERE work_id=?", (work_id,)).fetchone() == (
            "leased", 2, 7, "TEST-owner", "held",
        )
    monkeypatch.setattr(storage_module, "upgrade_1107", original)
    status = core.initialize()
    with sqlite3.connect(core.storage.path) as conn:
        assert status.schema_version == SCHEMA_VERSION == 1109
        assert conn.execute("SELECT state,attempt,lease_token,lease_owner,last_error_code FROM work_items WHERE work_id=?", (work_id,)).fetchone() == (
            "leased", 2, 7, "TEST-owner", "held",
        )
        assert conn.execute("SELECT lease_token,stage,error_code,error_field FROM work_error_details WHERE work_id=?", (work_id,)).fetchone() == (
            7, "TEST", "held", "field",
        )
        assert conn.execute("SELECT payload_json,last_error_code FROM capture_inbox WHERE token='TEST-inbox'").fetchone() == (
            "{}", "held",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='work_items'").fetchone()[0]
        assert "evaluate_candidate" in ddl



def test_r1_one_candidate_never_accumulates_a_queue_of_stale_evaluations(app):
    """The invariant is unchanged; the way it is kept is stronger.

    The dedup key is (candidate, revision, evidence_fingerprint, rule_version)
    and the fingerprint covers the evidence set, so scheduling on every arrival
    minted a fresh evaluation and retired the ones still waiting -- on alpha
    7,802 of 11,158 were retired before anyone judged them, and exactly two ever
    reached a verdict.

    Now a second one is never created while the first is queued, so there is
    nothing to retire.  The supersede path stays for the repair routes that
    schedule a candidate directly; the next test covers those.
    """
    core, ctx = app
    _candidate(core, ctx)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-r1/supersede")
    _settle_evidence(core)
    assert _sweep(core, ctx) == 0, "a candidate that already has a queued evaluation is left alone"

    _lifecycle, evaluations, work = _candidate_rows(core)
    queued = [row for row in evaluations if row["state"] == "queued"]
    assert len(queued) == 1, "exactly one evaluation stays live per candidate"
    assert [row for row in evaluations if row["state"] == "obsolete"] == [], "and none had to be retired"
    assert {row["work_id"]: row["state"] for row in work}[queued[0]["work_id"]] == "pending"


def test_r1_a_directly_scheduled_candidate_still_retires_what_it_replaces(app):
    """The supersede path is the safety net for the repair routes.

    Those schedule without the debounce, because a rule change or a revision
    change has to take effect now rather than after the next quiet window.  When
    they do, an unstarted evaluation must still step aside.
    """
    core, ctx = app
    saved, _source, _proposal, _registration = _candidate(core, ctx)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-r1/direct")
    snapshot = _snapshot(core, saved.ref, saved.revision)
    with core.storage.write(ctx) as tx:
        tx.candidates._schedule(snapshot, now=core.clock.utc_now(), rule_version=snapshot.rule_version)

    _lifecycle, evaluations, work = _candidate_rows(core)
    queued = [row for row in evaluations if row["state"] == "queued"]
    retired = [row for row in evaluations if row["state"] == "obsolete"]
    assert len(queued) == 1 and len(retired) == 1
    assert retired[0]["reason"] == "superseded_by_new_evidence"
    assert retired[0]["evaluation_id"] < queued[0]["evaluation_id"], "the newest evidence wins"
    assert len(json.loads(queued[0]["evidence_refs_json"])) == 2, "and it carries the larger set"
    states = {row["work_id"]: row["state"] for row in work}
    assert states[retired[0]["work_id"]] == "obsolete", "its work item is retired with it"
    assert states[queued[0]["work_id"]] == "pending"


def test_r1_a_started_evaluation_is_never_retired_by_new_evidence(app):
    """The at-most-once fence outranks the tidy-up."""
    core, ctx = app
    _candidate(core, ctx)
    _lifecycle, evaluations, _work = _candidate_rows(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE candidate_evaluations SET model_attempted_at=? WHERE evaluation_id=?",
            (core.clock.utc_now(), evaluations[0]["evaluation_id"]),
        )
        conn.commit()
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-r1/started")
    _lifecycle, after, _work = _candidate_rows(core)
    started = next(row for row in after if row["evaluation_id"] == evaluations[0]["evaluation_id"])
    assert started["state"] == "queued", "a started evaluation keeps its fence"


# --------------------------------------------------------------------------
# Questions no verdict could settle are answered without the model
# --------------------------------------------------------------------------

def _register_candidate(core, ctx, source, value, *, slug):
    proposal = draft(source, value, subject=f"entity-{slug}", predicate=f"property-{slug}")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        registration = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    return saved, registration


def test_a_value_absent_from_every_source_is_not_sent_to_the_model(app):
    """On alpha 2,019 evaluations in a day promoted 7 facts; 46% of them asked
    about a value none of the supplied sources contained."""
    core, ctx = app
    source = capture(core, ctx, "entity-violet property-violet 还没有定下来。", key="TEST-r1/violet")
    _saved, registration = _register_candidate(core, ctx, source, "紫色", slug="violet")
    assert registration.work_queued is False
    assert (registration.processing_state, registration.reason) == ("waiting_evidence", "value_not_in_evidence")
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert work == []
    assert len(evaluations) == 1
    assert (evaluations[0]["state"], evaluations[0]["reason"]) == ("waiting_evidence", "value_not_in_evidence")
    assert evaluations[0]["work_id"] is None and evaluations[0]["model_attempted_at"] is None

    # Recorded like a verdict: the settle sweep never selects that question again.
    _finish_source_work(core)
    _settle_evidence(core)
    assert _sweep(core, ctx) == 0
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 0

    # A person stating the value is a new question, and that one is asked.
    said = capture(core, ctx, "entity-violet property-violet 定为紫色。", key="TEST-r1/violet-said")
    with core.storage.write(ctx) as tx:
        tx.candidates.observe_source(said.ref, said.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)
    _settle_evidence(core)
    _sweep(core, ctx)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1


def test_a_fact_without_any_authority_is_not_sent_to_the_model(app):
    core, ctx = app
    source = capture(core, ctx, "entity-said property-said 灰色", origin="assistant_visible", key="TEST-r1/said")
    proposal = draft(source, "灰色", subject="entity-said", predicate="property-said", kind="fact", statement_kind="fact")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append("TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                 recorded_at=core.clock.utc_now())
        registration = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    assert (registration.work_queued, registration.reason) == (False, "no_authoritative_evidence")
    _lifecycle, _evaluations, work = _candidate_rows(core)
    assert work == []


def test_a_person_only_kind_proposed_from_a_tool_is_not_a_candidate(app):
    """2,034 evaluations of such candidates on alpha promoted nothing: only the
    person's own words can, and their consolidation proposes it with that authority."""
    core, ctx = app
    source = capture(core, ctx, "entity-tool property-tool 灰色", origin="tool_observation", key="TEST-r1/tool")
    saved, registration = _register_candidate(core, ctx, source, "灰色", slug="tool")
    assert (registration.processing_state, registration.reason, registration.work_queued) == (
        "archived", "person_kind_without_person", False)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert work == [] and evaluations == []
    with sqlite3.connect(core.storage.path) as conn:
        terms = conn.execute("SELECT count(*) FROM candidate_trigger_terms WHERE candidate_ref=?", (saved.ref,)).fetchone()[0]
    assert terms == 0, "an archived candidate must not be woken by later sources"


def test_a_person_only_kind_queued_before_the_rule_is_archived_without_the_model(app):
    core, ctx = app
    source = capture(core, ctx, "entity-tool property-tool 灰色", origin="tool_observation", key="TEST-r1/legacy")
    saved, _registration = _register_candidate(core, ctx, source, "灰色", slug="tool")
    # What an older release left behind: the candidate pending with a queued evaluation.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence'")
        conn.commit()
    with core.storage.write(ctx) as tx:
        refs, digest = tx.candidates._question(saved.ref, saved.revision)
        tx._check(write=True).execute(
            """INSERT INTO candidate_evaluations(candidate_ref,candidate_revision,evidence_fingerprint,evidence_refs_json,
               rule_version,memory_epoch,state,reason,created_at) VALUES (?,?,?,?,?,0,'queued','new_evidence',?)""",
            (saved.ref, saved.revision, digest, json.dumps([f"{r}@{v}" for r, v in refs]), "r1-candidate-v1",
             core.clock.utc_now()))
        evaluation_id = tx._check().execute("SELECT max(evaluation_id) FROM candidate_evaluations").fetchone()[0]
        work_id = tx.candidates._enqueue(saved.ref, evaluation_id, core.clock.utc_now())
        tx._check(write=True).execute("UPDATE candidate_evaluations SET work_id=? WHERE evaluation_id=?", (work_id, evaluation_id))
    _finish_source_work(core)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0
    assert (lifecycle[0]["processing_state"], lifecycle[0]["reason"]) == ("archived", "person_kind_without_person")
    assert evaluations[0]["model_attempted_at"] is None and work[0]["state"] == "done"


def test_an_evaluation_queued_before_the_check_is_settled_without_the_model(app):
    """The live backlog was queued before this check existed; the worker applies
    it too, spends no attempt, and leaves the at-most-once fence untouched."""
    core, ctx = app
    _candidate(core, ctx)
    unrelated = capture(core, ctx, "entity-blue property-blue 今天先不讨论。", key="TEST-r1/unrelated")
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_evaluations SET evidence_refs_json=? WHERE state='queued'",
                     (json.dumps([f"{unrelated.ref}@{unrelated.revision}"]),))
        conn.commit()
    evaluator = Evaluator()
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0
    assert receipt.completed == 1 and receipt.failed == 0
    assert (evaluations[0]["state"], evaluations[0]["reason"]) == ("waiting_evidence", "value_not_in_evidence")
    assert evaluations[0]["model_attempted_at"] is None
    assert work[0]["state"] == "done"
    assert (lifecycle[0]["processing_state"], lifecycle[0]["reason"]) == ("waiting_evidence", "value_not_in_evidence")


def _long_evidence_messages(core, ctx, content):
    saved, _source, _proposal, _registration = _candidate(core, ctx)
    long_source = capture(core, ctx, content, origin="tool_observation", key="TEST-r1/long-output")
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(saved.ref, saved.revision)
    from scope_recall.core.candidate_lifecycle import CandidateSnapshot

    snapshot_ = CandidateSnapshot(saved.ref, saved.revision, "TEST-scope", None, None, "proposed", head.payload,
                                  "pending_evaluation", "new_evidence", "r1-candidate-v1")
    messages = candidate_evaluation_messages(snapshot_, (long_source,))
    record = json.loads(messages[2]["content"])["sources"][0]
    return record, long_source


def test_a_long_source_reaches_the_evaluation_as_a_window_around_the_value(app):
    core, ctx = app
    filler = "日志行：无关的构建输出。" * 400
    record, source = _long_evidence_messages(core, ctx, filler + "entity-blue property-blue 蓝 色 已确认。" + filler)
    total = len(source.event["content"])
    assert total > 9000
    assert "蓝 色" in record["content"]
    assert len(record["content"]) <= 3000 + len("蓝 色")
    window = record["source_window"]
    assert window["total"] == total and window["coverage"] == "fragment_only"
    assert source.event["content"][window["start"]:window["end"]] == record["content"]


def test_a_long_source_without_the_value_sends_its_head_and_a_short_one_is_whole(app):
    core, ctx = app
    filler = "日志行：无关的构建输出。" * 400
    record, source = _long_evidence_messages(core, ctx, filler)
    assert record["content"] == source.event["content"][:3000]
    saved, short_source, _proposal, _registration = _candidate(core, ctx, key="TEST-r1/short")
    with core.storage.read(ctx) as tx:
        head = tx.claims.version(saved.ref, saved.revision)
    from scope_recall.core.candidate_lifecycle import CandidateSnapshot

    snapshot_ = CandidateSnapshot(saved.ref, saved.revision, "TEST-scope", None, None, "proposed", head.payload,
                                  "pending_evaluation", "new_evidence", "r1-candidate-v1")
    whole = json.loads(candidate_evaluation_messages(snapshot_, (short_source,))[2]["content"])["sources"][0]
    assert whole["content"] == short_source.event["content"] and "source_window" not in whole


def test_captured_conversation_is_claimed_before_candidate_evaluation(app):
    """A busy candidate queue must never hold a new conversation back from recall."""
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    capture(core, ctx, "TEST 新的一轮对话。", key="TEST-r1/new-turn")
    with core.storage.write(ctx) as tx:
        order = []
        while True:
            leased = tx.work.claim_next("TEST-order", core.clock.utc_now(), lease_seconds=30, limit=1)
            if not leased:
                break
            order.append(leased[0].work_type)
    assert "evaluate_candidate" in order and len(order) >= 2
    assert order[-1] == "evaluate_candidate", order


def _new_first_hand_evidence(core, ctx, text, key):
    said = capture(core, ctx, text, key=key)
    with core.storage.write(ctx) as tx:
        tx.candidates.observe_source(said.ref, said.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)
    _settle_evidence(core)
    return _sweep(core, ctx)


def test_a_candidate_gets_two_verdicts_then_only_a_restatement_reopens_it(app):
    """On alpha 307 candidates were asked ten times or more.  Of 27 promotions,
    19 came from the first verdict and 4 from the second."""
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1

    assert _new_first_hand_evidence(core, ctx, "entity-blue property-blue 今天又讨论了一次。", "TEST-r1/second") == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 2

    # New testimony that does not restate the value: answered without the model.
    assert _new_first_hand_evidence(core, ctx, "entity-blue property-blue 还要再想想。", "TEST-r1/third") == 0
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 2
    lifecycle, evaluations, _work = _candidate_rows(core)
    assert (lifecycle[0]["processing_state"], lifecycle[0]["reason"]) == ("waiting_evidence", "repeat_without_restatement")
    assert evaluations[-1]["reason"] == "repeat_without_restatement" and evaluations[-1]["work_id"] is None

    # Someone says the value again: that is worth a third verdict.
    assert _new_first_hand_evidence(core, ctx, "entity-blue property-blue 最后确认用蓝色。", "TEST-r1/restated") == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 3


def test_a_repeat_queued_before_the_limit_is_answered_without_the_model(app):
    core, ctx = app
    saved, _source, _proposal, _registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _new_first_hand_evidence(core, ctx, "entity-blue property-blue 今天又讨论了一次。", "TEST-r1/second")
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 2
    said = capture(core, ctx, "entity-blue property-blue 还要再想想。", key="TEST-r1/legacy-third")
    with core.storage.write(ctx) as tx:
        tx.candidates.observe_source(said.ref, said.revision, observed_at=core.clock.utc_now())
        refs, digest = tx.candidates._question(saved.ref, saved.revision)
        conn = tx._check(write=True)
        # What an older release queued: a third evaluation, no restatement in it.
        conn.execute(
            """INSERT INTO candidate_evaluations(candidate_ref,candidate_revision,evidence_fingerprint,evidence_refs_json,
               rule_version,memory_epoch,state,reason,created_at) VALUES (?,?,?,?,?,0,'queued','new_evidence',?)""",
            (saved.ref, saved.revision, digest, json.dumps([f"{r}@{v}" for r, v in refs]), "r1-candidate-v1",
             core.clock.utc_now()))
        evaluation_id = conn.execute("SELECT max(evaluation_id) FROM candidate_evaluations").fetchone()[0]
        work_id = tx.candidates._enqueue(saved.ref, evaluation_id, core.clock.utc_now())
        conn.execute("UPDATE candidate_evaluations SET work_id=? WHERE evaluation_id=?", (work_id, evaluation_id))
        conn.execute("UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence'")
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert evaluator.calls == 2
    third = next(row for row in evaluations if row["evaluation_id"] == evaluation_id)
    assert third["reason"] == "repeat_without_restatement" and third["model_attempted_at"] is None
