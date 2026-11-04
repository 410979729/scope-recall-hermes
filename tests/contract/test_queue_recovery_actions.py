"""Recovery guidance is checked by executing the corresponding storage and CLI actions."""

import json
from datetime import datetime

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import capture_inbox, suppression
from scope_recall.core.claims import Qualification
from scope_recall.core.inbox_rules import deferral
from scope_recall.maintenance import cli, doctor_store
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.runtime.scheduling import next_wake
from test_candidate_lifecycle import (
    Evaluator,
    _candidate,
    _candidate_rows,
    _finish_source_work,
    _settle_evidence,
    _sweep,
)
from test_claims import app, capture, draft
from test_deletion import authorize, request
from v11_support import source_event

from tests.queue_state_support import advance, check_transitions, planning_config, state_table


def _inbox(core, ctx, code):
    token, _ = capture_inbox.enqueue(
        core.storage,
        core.clock,
        ctx,
        source_event(source_event_key="TEST-recovery"),
        scope_id="TEST-scope",
        host_scope={},
    )
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
    return token


def _command(core, ctx, tmp_path, monkeypatch):
    import scope_recall.core as core_module

    config = tmp_path / "TEST-runtime.json"
    config.write_text(
        json.dumps(
            dict(
                binding=dict(
                    agent_id=ctx.binding.agent_id,
                    installation_id=ctx.binding.installation_id,
                    data_directory=str(ctx.binding.data_directory),
                    scope_ids=sorted(ctx.binding.scope_ids),
                    test_mode=True,
                ),
                session_id=ctx.session_id,
                allowed_scope_ids=sorted(ctx.allowed_scope_ids),
                actor_origin=ctx.actor_origin,
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                auxiliary=dict(external_embedding=False, external_consolidation=False),
            )
        )
    )
    monkeypatch.setattr(core_module, "MemoryCore", lambda _cfg: core)
    arguments = [
        str(config) if part == "<runtime-config.json>" else part for part in doctor_store._RETRY_COMMAND.split()[1:]
    ]
    assert cli.main(arguments) == 0


@pytest.mark.parametrize(
    "code", ["ACCESS_DENIED", "GAVE_UP|TEST|25|replay|INPUT_INVALID", "GAVE_UP|TEST|25|rekey|INPUT_INVALID"]
)
def test_doctor_recommended_retry_command_reopens_and_processes_the_inbox(app, tmp_path, monkeypatch, code):
    core, ctx = app
    token = _inbox(core, ctx, code)
    with core.storage.read(ctx) as tx:
        report = DoctorReport("hermes", "ok")
        doctor_store._capture_queue_checks(report, ctx.binding, tx._check())
    assert doctor_store._RETRY_COMMAND in str(report.checks)
    _command(core, ctx, tmp_path, monkeypatch)
    with core.storage.read(ctx) as tx:
        after = tx._check().execute("SELECT last_error_code FROM capture_inbox WHERE token=?", (token,)).fetchone()[0]
    expected = "VERSION_CONFLICT" if "|rekey|" in code else None
    assert after == expected
    action = capture_inbox.resolve_conflicted_ingress if expected else capture_inbox.replay_inbox
    action(core.storage, core.clock, ctx, authorize=lambda _scope: ctx.allowed_scope_ids, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
    before = "given_up" if code.startswith("GAVE_UP|") else "stopped"
    check_transitions(state_table("Capture inbox"), {token: before}, {token: "ready"})


@pytest.mark.parametrize("deferred", [False, True])
def test_doctor_does_not_recommend_retry_for_deferred_or_invalid_capture(app, monkeypatch, tmp_path, deferred):
    core, ctx = app
    now = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00"))
    code = deferral(None, ContractError("INPUT_INVALID"), now, path="replay") if deferred else "INPUT_INVALID"
    _inbox(core, ctx, code)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(doctor_store, "datetime", FixedDateTime)
    with core.storage.read(ctx) as tx:
        report = DoctorReport("hermes", "ok")
        doctor_store._capture_queue_checks(report, ctx.binding, tx._check())
    text = str(report.checks)
    assert "retry-failures" not in text
    assert ("until" if deferred else "corrected") in text
    _command(core, ctx, tmp_path, monkeypatch)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT last_error_code FROM capture_inbox").fetchone()[0] == code


@pytest.mark.parametrize("fact_state,expected", [("active", "resolved"), ("retracted", "archived")])
def test_released_suppression_restores_the_current_fact_state(app, fact_state, expected):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    with core.storage.write(ctx) as tx:
        candidate = tx.claims.append(
            "TEST-scope",
            draft(source),
            Qualification(fact_state, "inferred_suggestion", "TEST-fact"),
            recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(candidate.ref, candidate.revision, observed_at=core.clock.utc_now())
    authorize(core, ctx, candidate, mode="suppress")
    core.forget(ctx, request(candidate, mode="suppress"), remaining_seconds=10)
    assert _candidate_rows(core)[0][0]["processing_state"] == "blocked"
    with core.storage.write(ctx) as tx:
        suppression.release(tx, (tx.deletions.target(candidate.ref),), now=core.clock.utc_now())
    assert _candidate_rows(core)[0][0]["processing_state"] == expected
    check_transitions(state_table("Candidate debounce"), {candidate.ref: "blocked"}, {candidate.ref: expected})


@pytest.mark.parametrize("grant_used", [False, True])
def test_queued_evaluation_with_failed_work_reports_its_actual_recovery_fence(app, tmp_path, monkeypatch, grant_used):
    core, ctx = app
    _candidate(core, ctx)
    _settle_evidence(core)
    _sweep(core, ctx)
    _finish_source_work(core)
    for attempt in range(3):
        with core.storage.write(ctx) as tx:
            (work,) = tx.work.claim_next(
                "TEST-unfinished",
                core.clock.utc_now(),
                lease_seconds=60,
                allowed_work_types=frozenset({"evaluate_candidate"}),
            )
            began = tx.candidates.begin_model_attempt(work.subject_revision, *work.lease, now=core.clock.utc_now())
            assert began == (attempt == 0)
        advance(core, 61)
        with core.storage.write(ctx) as tx:
            tx.work.release_stale(core.clock.utc_now())
    lifecycle, evaluations, work_rows = _candidate_rows(core)
    assert evaluations[0]["state"] == "queued" and work_rows[0]["state"] == "failed"
    now = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00"))
    plan = next_wake(planning_config(ctx, capable=True), now=now)
    assert plan.reason == "failed_terminal" and plan.due_at is None
    if grant_used:
        from scope_recall.core.failure_retry import RETRY_GENERATION, marked

        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute(
                "UPDATE work_items SET last_error_code=? WHERE work_id=?",
                (marked("lease_exhausted", generation=RETRY_GENERATION), work.work_id),
            )
    before_preview = _candidate_rows(core)
    with core.storage.write(ctx) as tx:
        assert not tx.candidates.reopen_evaluation(work.work_id, now=core.clock.utc_now(), reason="interrupted_retry")
        assert not tx.candidates.reopen_evaluation(work.work_id, now=core.clock.utc_now(), reason="derivation_retry")
        preview = tx.work.retry_failed(now=core.clock.utc_now(), dry_run=True)
    assert _candidate_rows(core) == before_preview
    assert preview["retried"] == (0 if grant_used else 1) and preview["declined"] == 0
    if grant_used:
        assert preview["reconciled_evaluations"] == 1
    assert _candidate_rows(core)[1][0]["model_attempted_at"] is not None
    evaluator = Evaluator()
    report = DoctorReport("hermes", "ok")
    assert doctor_store.check_storage(report, ctx.binding, ctx.binding.data_directory)
    text = str(report.checks)
    assert "lease_exhausted" in text and doctor_store._RETRY_COMMAND in text

    core.drain_worker(ctx, consolidation=evaluator, remaining_seconds=10)
    assert evaluator.calls == 0
    assert _candidate_rows(core)[1][0]["state"] == "queued"
    _command(core, ctx, tmp_path, monkeypatch)
    if grant_used:
        assert _candidate_rows(core)[1][0]["state"] == "failed"
        assert _candidate_rows(core)[1][0]["model_attempted_at"] is not None
        core.drain_worker(ctx, consolidation=evaluator, remaining_seconds=20)
        assert evaluator.calls == 0
        capture(core, ctx, "entity-blue property-blue 蓝色，今天仍然这样。", key="TEST-recovery/new-evidence")
        _finish_source_work(core)
        _settle_evidence(core)
        _sweep(core, ctx)
    else:
        assert _candidate_rows(core)[1][0]["model_attempted_at"] is None
    core.drain_worker(ctx, consolidation=evaluator, remaining_seconds=20)
    assert evaluator.calls == 1


def test_recovery_preview_and_apply_share_the_bookkeeping_page_limit(app):
    from scope_recall.core.failure_retry import RETRY_GENERATION, marked

    core, ctx = app
    _candidate(core, ctx, key="TEST/page-a")
    _candidate(core, ctx, key="TEST/page-b")
    _settle_evidence(core)
    _sweep(core, ctx)
    _finish_source_work(core)
    for _ in range(3):
        with core.storage.write(ctx) as tx:
            work = tx.work.claim_next(
                "TEST-page",
                core.clock.utc_now(),
                lease_seconds=60,
                limit=2,
                allowed_work_types=frozenset({"evaluate_candidate"}),
            )
            assert len(work) == 2
        advance(core, 61)
        with core.storage.write(ctx) as tx:
            tx.work.release_stale(core.clock.utc_now())
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "UPDATE work_items SET last_error_code=? WHERE work_id=?",
            (marked("lease_exhausted", generation=RETRY_GENERATION), work[0].work_id),
        )
    preview = core.operations.retry_failed_work(ctx, dry_run=True, limit=1)
    assert preview["retried"] == 0 and preview["reconciled_evaluations"] == 1
    applied = core.operations.retry_failed_work(ctx, dry_run=False, limit=1)
    assert applied["retried"] == preview["retried"]
    assert applied["reconciled_evaluations"] == preview["reconciled_evaluations"]
    assert [row["state"] for row in _candidate_rows(core)[1]] == ["failed", "queued"]
    next_page = core.operations.retry_failed_work(ctx, dry_run=False, limit=1)
    assert next_page["retried"] == next_page["reconciled_evaluations"] == 1


@pytest.mark.parametrize("grant_used", [False, True])
def test_mixed_recovery_page_limits_distinct_work_and_matches_preview(app, grant_used):
    from datetime import timedelta

    from scope_recall.core.failure_retry import RETRY_GENERATION, marked

    core, ctx = app
    _candidate(core, ctx)
    _settle_evidence(core)
    _sweep(core, ctx)
    now = core.clock.utc_now()
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    with core.storage.write(ctx) as tx:
        (candidate,) = tx.work.claim_next(
            "TEST-mixed", now, lease_seconds=60, allowed_work_types=frozenset({"evaluate_candidate"})
        )
        assert tx.candidates.begin_model_attempt(candidate.subject_revision, *candidate.lease, now=now)
        tx.work.fail(*candidate.lease, now=now, error_code="timeout", recoverable=False)
        ordinary = tx.work.claim_next(
            "TEST-mixed", now, lease_seconds=60, limit=2, allowed_work_types=frozenset({"consolidate", "embed"})
        )
        assert len(ordinary) == 2
        for work in ordinary:
            tx.work.fail(*work.lease, now=now, error_code="timeout", recoverable=False)
        ids = [candidate.work_id, *(work.work_id for work in ordinary)]
        for index, work_id in enumerate(ids):
            tx._check(write=True).execute(
                "UPDATE work_items SET available_at=? WHERE work_id=?",
                ((moment - timedelta(seconds=3 - index)).isoformat().replace("+00:00", "Z"), work_id),
            )
        if grant_used:
            tx._check(write=True).execute(
                "UPDATE work_items SET last_error_code=? WHERE work_id=?",
                (marked("timeout", generation=RETRY_GENERATION), candidate.work_id),
            )

    def snapshot():
        with core.storage.read(ctx) as tx:
            return {
                table: [dict(row) for row in tx._check().execute(f"SELECT * FROM {table} ORDER BY {order}")]
                for table, order in (
                    ("work_items", "work_id"),
                    ("candidate_evaluations", "evaluation_id"),
                    ("candidate_lifecycle", "candidate_ref,candidate_revision"),
                )
            }

    before = snapshot()
    preview = core.operations.retry_failed_work(ctx, dry_run=True, limit=2)
    assert snapshot() == before
    expected = {ordinary[0].work_id} | (set() if grant_used else {candidate.work_id})
    assert set(preview["retried_work_ids"]) == expected
    assert preview["reconciled_evaluations"] == 1
    applied = core.operations.retry_failed_work(ctx, dry_run=False, limit=2)
    assert applied == {**preview, "applied": True}
    after = snapshot()
    changed = {
        row["work_id"] for row, prior in zip(after["work_items"], before["work_items"], strict=True) if row != prior
    }
    evaluation_changed = after["candidate_evaluations"] != before["candidate_evaluations"]
    handled = changed | ({candidate.work_id} if evaluation_changed else set())
    assert handled == {candidate.work_id, ordinary[0].work_id}
    assert len(handled) == 2
    rows = {row["work_id"]: row for row in after["work_items"]}
    assert rows[ordinary[1].work_id]["state"] == "failed"
    assert rows[ordinary[0].work_id]["state"] == "pending"
    assert rows[candidate.work_id]["state"] == ("failed" if grant_used else "pending")
