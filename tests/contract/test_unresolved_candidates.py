"""Unresolved speakers retain their claims and sources without spending evaluation attempts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import Qualification
from scope_recall.core.worker_candidates import process_candidate_evaluation
from scope_recall.maintenance.cli import main

from tests.contract.test_candidate_lifecycle import (
    Evaluator,
    _candidate,
    _candidate_rows,
    _finish_source_work,
    app,
)
from tests.contract.test_claims import capture, draft
from tests.contract.test_deletion import authorize, request

SUBJECT = "unresolved-source:" + "a" * 64
REASON = "unresolved_subject"


def _legacy_candidate(core, ctx, *, failed=False, key=None):
    """A synthetic unresolved claim with unfinished evaluation metadata."""
    saved, source, proposal, _registration = _candidate(core, ctx, key=key)
    proposal = {**proposal, "subject": SUBJECT}
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE claim_versions SET payload_json=? WHERE claim_id=?", (json.dumps(proposal), saved.ref))
        if failed:
            conn.execute(
                "UPDATE candidate_evaluations SET state='failed',reason='evaluation_failed' WHERE candidate_ref=?",
                (saved.ref,),
            )
            conn.execute(
                "UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason='evaluation_failed' "
                "WHERE candidate_ref=?",
                (saved.ref,),
            )
            conn.execute(
                "UPDATE work_items SET state='failed',last_error_code='input_invalid' WHERE subject_ref=?",
                ("candidate:" + saved.ref,),
            )
    _finish_source_work(core)
    return saved, source, proposal


def test_unresolved_claim_is_not_queued_or_woken_by_new_evidence(app):
    core, ctx = app
    source = capture(core, ctx, "TEST speaker prefers blue paint.")
    proposal = draft(source, "blue paint", subject=SUBJECT, predicate="prefers")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "TEST"),
            recorded_at=core.clock.utc_now(),
        )
        registration = tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    assert (registration.processing_state, registration.reason, registration.work_queued) == ("archived", REASON, False)
    capture(core, ctx, "TEST speaker confirms blue paint.")
    _finish_source_work(core)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0 and evaluations == work == []
    assert lifecycle[0]["reason"] == REASON
    with core.storage.read(ctx) as tx:
        claim = tx.claims.version(saved.ref, saved.revision)
        assert claim.state == "proposed" and claim.payload == proposal
    assert core.source(ctx, source.ref, source.revision).event == source.event
    authorize(core, ctx, saved)
    assert core.forget(ctx, request(saved), remaining_seconds=10)["read_blocked"]
    assert core.claim_history(ctx, saved.ref) == ()


@pytest.mark.parametrize("failed", [False, True])
def test_existing_unresolved_evaluations_are_obsoleted_idempotently(app, failed):
    core, ctx = app
    saved, _source, proposal = _legacy_candidate(core, ctx, failed=failed)
    with core.storage.write(ctx) as tx:
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=1) == 1
    first = _candidate_rows(core)
    with core.storage.write(ctx) as tx:
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=1) == 0
        assert tx.claims.version(saved.ref, saved.revision).payload == proposal
    assert _candidate_rows(core) == first
    lifecycle, evaluations, work = first
    assert (lifecycle[0]["processing_state"], lifecycle[0]["reason"]) == ("archived", REASON)
    assert (evaluations[0]["state"], evaluations[0]["reason"], evaluations[0]["failure_code"]) == (
        "obsolete",
        REASON,
        REASON,
    )
    assert (work[0]["state"], work[0]["last_error_code"]) == ("obsolete", REASON)
    with core.storage.read(ctx) as tx:
        assert tx.candidates.summary().archived_other == 1
        assert tx.candidates.summary().failed == 0


@pytest.mark.parametrize("reviewed", [False, True])
@pytest.mark.parametrize("processing_state", ["pending_evaluation", "waiting_evidence"])
def test_worker_closes_a_leased_unresolved_evaluation_without_a_model(app, reviewed, processing_state):
    core, ctx = app
    _legacy_candidate(core, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET processing_state=?", (processing_state,))
        if reviewed:
            conn.execute("UPDATE candidate_evaluations SET model_attempted_at=?", (core.clock.utc_now(),))
    with core.storage.write(ctx) as tx:
        item = tx.work.claim_next("TEST-worker", core.clock.utc_now(), lease_seconds=60, limit=1)[0]
    evaluator = Evaluator()
    result = process_candidate_evaluation(
        core.storage,
        core.clock,
        ctx,
        item,
        evaluator=evaluator,
        started=core.clock.monotonic(),
        budget=10,
    )
    assert result == ("obsolete", REASON, "obsolete") and evaluator.calls == 0
    lifecycle, evaluations, work = _candidate_rows(core)
    assert lifecycle[0]["processing_state"] == "archived"
    assert (evaluations[0]["model_attempted_at"] is not None) == reviewed
    assert evaluations[0]["reason"] == REASON
    assert work[0]["state"] == "obsolete" and work[0]["lease_token"] > item.lease_token


def test_background_pass_retires_failed_unresolved_evaluations(app):
    core, ctx = app
    _legacy_candidate(core, ctx, failed=True)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert evaluator.calls == 0
    assert lifecycle[0]["reason"] == evaluations[0]["reason"] == work[0]["last_error_code"] == REASON
    assert evaluations[0]["state"] == work[0]["state"] == "obsolete"


def test_resolved_subject_keeps_its_evaluation(app):
    core, ctx = app
    saved, _source, proposal, registration = _candidate(core, ctx)
    _finish_source_work(core)
    evaluator = Evaluator(proposal)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert registration.work_queued and evaluator.calls == 1
    assert core.current_claim(ctx, saved.ref).state == "active"


def test_one_pass_retires_a_small_backlog_before_any_evaluation_is_claimed(app):
    core, ctx = app
    for index in range(2):
        _legacy_candidate(core, ctx, key=f"TEST-subject/{index}")
    evaluator = Evaluator()
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 0 and result.obsolete == 0
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert [row["reason"] for row in evaluations] == [REASON, REASON]
    assert [(row["state"], row["last_error_code"]) for row in work] == [("obsolete", REASON)] * 2


def test_a_candidate_behind_a_finished_walk_is_counted_in_the_worker_receipt(app):
    """A row the backlog walk has already passed, as from an older backup, closes when its evaluation is claimed."""
    core, ctx = app
    core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=Evaluator())
    for index in range(2):
        _legacy_candidate(core, ctx, key=f"TEST-subject/{index}")
    evaluator = Evaluator()
    result = core.drain_worker(ctx, max_items=1, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 0 and result.obsolete == 1
    assert result.items[0].error_code == REASON and result.items[0].state == "obsolete"


def test_operator_retry_declines_an_unresolved_failure_without_writing(app):
    core, ctx = app
    _legacy_candidate(core, ctx, failed=True)
    before = _candidate_rows(core)
    work = before[2]
    with core.storage.write(ctx) as tx:
        assert not tx.candidates.reopen_evaluation(work[0]["work_id"], now=core.clock.utc_now())
    assert _candidate_rows(core) == before


def test_retry_command_preview_and_apply_agree_and_respect_limit(app, tmp_path, capsys):
    core, ctx = app
    for index in range(3):
        _legacy_candidate(core, ctx, failed=True, key=f"TEST-declined/{index}")
    for index in range(2):
        _candidate(core, ctx, key=f"TEST-retryable/known-{index}")
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_evaluations SET state='failed'")
        conn.execute(
            "UPDATE work_items SET state='failed',last_error_code='input_invalid' WHERE work_type='evaluate_candidate'"
        )
    before = _candidate_rows(core)
    path = tmp_path / "TEST-runtime.json"
    path.write_text(
        json.dumps(
            {
                "binding": {
                    "agent_id": ctx.binding.agent_id,
                    "installation_id": ctx.binding.installation_id,
                    "data_directory": str(ctx.binding.data_directory),
                    "scope_ids": sorted(ctx.binding.scope_ids),
                    "test_mode": True,
                },
                "session_id": ctx.session_id,
                "allowed_scope_ids": sorted(ctx.allowed_scope_ids),
                "project_id": ctx.project_id,
                "branch_id": ctx.branch_id,
            }
        ),
        encoding="utf-8",
    )
    args = ["retry-failures", "--config", str(path), "--limit", "1", "--include-terminal"]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert _candidate_rows(core) == before
    assert main([*args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied == {**preview, "applied": True}
    assert preview["retried"] == 1 and preview["declined"] == 3
    assert preview["declined_work_items"] == [{"work_id": row["work_id"], "reason": REASON} for row in before[2][:3]]
    after = _candidate_rows(core)
    declined_refs = {row["candidate_ref"] for row in before[1][:3]}
    assert [row for row in before[0] if row["candidate_ref"] in declined_refs] == [
        row for row in after[0] if row["candidate_ref"] in declined_refs
    ]
    assert before[1][:3] == after[1][:3] and before[2][:3] == after[2][:3]
    assert [row["state"] for row in after[2]] == ["failed"] * 3 + ["pending", "failed"]


def test_backlog_walk_bounds_scanned_rows_and_stops_after_completion(app):
    core, ctx = app
    for index in range(7):
        if index % 2:
            _candidate(core, ctx, key=f"TEST-known/{index}")
        else:
            _legacy_candidate(core, ctx, failed=True, key=f"TEST-unknown/{index}")
    keys = [(row["candidate_ref"], row["candidate_revision"]) for row in _candidate_rows(core)[0]]
    retired = 0
    for processed in (2, 4, 6, 7):
        with core.storage.write(ctx) as tx:
            statements = []
            tx._check().set_trace_callback(statements.append)
            retired += tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=2)
            tx._check().set_trace_callback(None)
            cursor = (
                tx._check()
                .execute("SELECT * FROM candidate_scan_cursors WHERE cursor_name LIKE 'unresolved_subjects:%'")
                .fetchone()
            )
            assert cursor["processed_count"] == processed
            assert (cursor["position_ref"], cursor["position_revision"]) == keys[processed - 1]
            assert cursor["completed"] == int(processed == 7)
            assert sum("json_extract" in sql for sql in statements) <= 2
    assert retired == 4
    with core.storage.write(ctx) as tx:
        statements = []
        tx._check().set_trace_callback(statements.append)
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=2) == 0
        tx._check().set_trace_callback(None)
    assert len(statements) == 1 and "candidate_scan_cursors" in statements[0]
    lifecycle, evaluations, work = _candidate_rows(core)
    assert sum(row["reason"] == REASON for row in lifecycle) == 4
    assert sum(row["state"] == "obsolete" for row in evaluations) == 4
    assert sum(row["state"] == "obsolete" for row in work) == 4


def test_backlog_cursor_isolated_by_context(app):
    core, ctx = app
    other = replace(ctx, project_id="TEST-other-project")
    _legacy_candidate(core, ctx, failed=True, key="TEST-first")
    _legacy_candidate(core, other, failed=True, key="TEST-second")
    for context in (ctx, other):
        with core.storage.write(context) as tx:
            assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=8) == 1
    assert all(row["reason"] == REASON for row in _candidate_rows(core)[0])


@pytest.mark.parametrize("complete", [False, True])
def test_backlog_cursor_survives_reopen_and_snapshot_restore(app, tmp_path, complete):
    core, ctx = app
    for index in range(3):
        _legacy_candidate(core, ctx, failed=True, key=f"TEST-snapshot/{index}")
    with core.storage.write(ctx) as tx:
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=8 if complete else 1) == (
            3 if complete else 1
        )
    backup = tmp_path / "TEST-snapshot.sqlite3"
    with sqlite3.connect(core.storage.path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    reopened = MemoryCore(CoreConfig(ctx.binding), clock=core.clock)
    reopened.initialize()
    with reopened.storage.write(ctx) as tx:
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=8) == (0 if complete else 2)
    with sqlite3.connect(backup) as source, sqlite3.connect(core.storage.path) as target:
        source.backup(target)
    with reopened.storage.write(ctx) as tx:
        assert tx.candidates.retire_unresolved_backlog(now=core.clock.utc_now(), limit=8) == (0 if complete else 2)
    assert all(row["reason"] == REASON for row in _candidate_rows(core)[0])
