"""Generated failures and recovery sequences exercise the durable queues, not a second implementation."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from scope_recall.contracts import ContractError
from scope_recall.core import capture_inbox, suppression
from scope_recall.core.candidate_debounce import settles_at
from scope_recall.core.inbox_rules import deferred_until, given_up, replayable, waiting
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import (
    _RETRY_COMMAND,
    _candidate_queue_checks,
    _capture_queue_checks,
    check_backlog,
)
from scope_recall.runtime.scheduling import next_wake
from test_candidate_lifecycle import _candidate, _candidate_rows, _sweep
from test_claims import capture
from test_deletion import authorize, request
from v11_support import source_event

from tests.queue_state_support import (
    QUEUE_SETTINGS,
    advance,
    check_transitions,
    planning_config,
    schema_states,
    state_table,
    store_factory,
)

ERRORS = st.sampled_from(["timeout", "http_429", "http_503", "input_invalid", "model_refused", "TEST_unknown"])


def test_redirect_is_an_endpoint_problem_and_unknown_recovery_stays_actionable():
    from scope_recall.core.failure_policy import failure_category, http_retryable, retryable
    from scope_recall.core.failure_retry import retry_class

    assert failure_category("http_301") == "configuration"
    assert not http_retryable(301)
    assert retry_class("TEST_future_failure") == "actionable"
    assert not retryable("TEST_future_failure", candidate_attempted=True)


def test_doctor_recovery_command_selects_required_runtime_configuration(tmp_path, monkeypatch):
    from scope_recall.maintenance import cli

    config = str(tmp_path / "TEST-worker.json")
    parsed = []
    monkeypatch.setattr(cli, "_run_core", lambda args, _action: parsed.append(args) or 0)
    arguments = [config if part == "<runtime-config.json>" else part for part in _RETRY_COMMAND.split()[1:]]
    assert cli.main(arguments) == 0
    assert parsed[0].config == config and parsed[0].apply
    report = DoctorReport("hermes", "ok", failed_work=1, work_error_counts={"TEST_failure": 1})
    check_backlog(report, wake_seconds=300)
    assert _RETRY_COMMAND in str(report.checks)


class WorkStates(RuleBasedStateMachine):
    """Durable work retains timers and lease fences through arbitrary failures and expired owners."""

    def __init__(self, factory):
        super().__init__()
        self.core, self.ctx = factory()
        self.capable = True
        self.leased = None
        self.table = state_table("Work items")
        self.previous = {}
        capture(self.core, self.ctx, "TEST durable queue")
        with self.core.storage.read(self.ctx) as tx:
            assert schema_states(tx._check(), "work_items", "state") == set(self.table)

    @rule(available=st.booleans())
    def capability(self, available):
        self.capable = available

    @rule()
    def claim(self):
        if not self.capable:
            return
        with self.core.storage.write(self.ctx) as tx:
            items = tx.work.claim_next("TEST-owner", self.core.clock.utc_now(), lease_seconds=60)
        if items:
            self.leased = items[0]

    @rule(code=ERRORS)
    def fail(self, code):
        if self.leased is None:
            return
        from scope_recall.core.failure_policy import retryable

        with self.core.storage.write(self.ctx) as tx:
            tx.work.fail(
                *self.leased.lease, now=self.core.clock.utc_now(), error_code=code, recoverable=retryable(code)
            )

    @rule(obsolete=st.booleans())
    def settle(self, obsolete):
        if self.leased is not None:
            with self.core.storage.write(self.ctx) as tx:
                action = tx.work.mark_obsolete if obsolete else tx.work.complete
                action(*self.leased.lease, now=self.core.clock.utc_now())

    @rule(seconds=st.integers(0, 7200))
    def expire(self, seconds):
        advance(self.core, seconds)
        with self.core.storage.write(self.ctx) as tx:
            tx.work.release_stale(self.core.clock.utc_now())

    @rule()
    def repaired(self):
        self.capable = True
        self.core.operations.retry_failed_work(self.ctx, limit=32, dry_run=False)

    @invariant()
    def every_item_has_a_timer_or_inspectable_stop(self):
        with self.core.storage.read(self.ctx) as tx:
            rows = list(tx._check().execute("SELECT * FROM work_items"))
            status = tx.status()
        for row in rows:
            assert row["state"] in self.table
            if row["state"] in {"pending", "leased"}:
                stamp = row["lease_until"] if row["state"] == "leased" else row["available_at"]
                assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo
            if row["state"] == "leased":
                assert row["lease_owner"] and row["lease_token"] > 0
            if row["state"] == "failed":
                assert row["last_error_code"]
        current = {row["work_id"]: row["state"] for row in rows}
        check_transitions(self.table, self.previous, current)
        self.previous = current
        if any(row["state"] in {"pending", "leased"} for row in rows):
            now = datetime.fromisoformat(self.core.clock.utc_now().replace("Z", "+00:00"))
            plan = next_wake(planning_config(self.ctx, capable=self.capable), now=now)
            assert plan.due_at is not None or plan.blocked > 0
        if status.failed_work:
            report = DoctorReport(
                "hermes", "ok", failed_work=status.failed_work, work_error_counts=dict(status.work_error_counts)
            )
            check_backlog(report, wake_seconds=300)
            assert "retry-failures --apply" in str(report.checks)


class CandidateStates(RuleBasedStateMachine):
    """Candidate debounce and request fences stay aligned with their actual work rows."""

    def __init__(self, factory):
        super().__init__()
        self.core, self.ctx = factory()
        self.candidate, *_ = _candidate(self.core, self.ctx)
        self.capable = True
        self.work = None
        self.began = False
        self.requests = 0
        self.sequence = 0
        self.previous = ({}, {})
        with self.core.storage.read(self.ctx) as tx:
            assert schema_states(tx._check(), "candidate_lifecycle", "processing_state") == set(
                state_table("Candidate debounce")
            )
            assert schema_states(tx._check(), "candidate_evaluations", "state") == set(
                state_table("Candidate evaluations")
            )

    @rule(available=st.booleans())
    def capability(self, available):
        self.capable = available

    @rule()
    def new_evidence(self):
        self.sequence += 1
        capture(
            self.core,
            self.ctx,
            f"entity-blue property-blue TEST evidence {self.sequence}",
            key=f"TEST-evidence/{self.sequence}",
        )

    @rule(seconds=st.integers(0, 90000))
    def sweep(self, seconds):
        advance(self.core, seconds)
        _sweep(self.core, self.ctx)
        with self.core.storage.write(self.ctx) as tx:
            tx.work.release_stale(self.core.clock.utc_now())

    @rule()
    def begin(self):
        if not self.capable:
            return
        with self.core.storage.write(self.ctx) as tx:
            items = tx.work.claim_next(
                "TEST-candidate",
                self.core.clock.utc_now(),
                lease_seconds=60,
                allowed_work_types=frozenset({"evaluate_candidate"}),
            )
            if items:
                self.work = items[0]
                row = (
                    tx._check()
                    .execute(
                        "SELECT state,model_attempted_at FROM candidate_evaluations WHERE evaluation_id=?",
                        (self.work.subject_revision,),
                    )
                    .fetchone()
                )
                self.began = tx.candidates.begin_model_attempt(
                    self.work.subject_revision, *self.work.lease, now=self.core.clock.utc_now()
                )
                assert self.began == (row[0] == "queued" and row[1] is None)
                self.requests += int(self.began)
                assert not tx.candidates.begin_model_attempt(
                    self.work.subject_revision, *self.work.lease, now=self.core.clock.utc_now()
                )

    @rule(code=ERRORS, budget_pause=st.booleans())
    def failure(self, code, budget_pause):
        if self.work is None or not self.began:
            return
        with self.core.storage.write(self.ctx) as tx:
            if not tx.work._verify_lease(*self.work.lease, now=self.core.clock.utc_now()):
                return
            if budget_pause:
                tx.candidates.defer_budget(
                    self.work.subject_revision, self.work, now=self.core.clock.utc_now(), code="credential_missing"
                )
            else:
                tx.candidates.fail(self.work.subject_revision, self.work, now=self.core.clock.utc_now(), code=code)

    @rule()
    def complete(self):
        if self.work is None or not self.began:
            return
        with self.core.storage.write(self.ctx) as tx:
            if tx.work._verify_lease(*self.work.lease, now=self.core.clock.utc_now()):
                tx.candidates.complete(
                    self.work.subject_revision,
                    self.work,
                    now=self.core.clock.utc_now(),
                    state="waiting_evidence",
                    reason="insufficient_evidence",
                    result_digest="TEST-digest",
                )

    @rule()
    def suppress(self):
        with self.core.storage.read(self.ctx) as tx:
            if tx.claims.version(self.candidate.ref, self.candidate.revision).suppressed:
                return
        authorize(self.core, self.ctx, self.candidate, mode="suppress")
        self.core.forget(self.ctx, request(self.candidate, mode="suppress"), remaining_seconds=10)

    @rule()
    def release_suppression(self):
        with self.core.storage.write(self.ctx) as tx:
            suppression.release(tx, (tx.deletions.target(self.candidate.ref),), now=self.core.clock.utc_now())

    @rule()
    def repair(self):
        self.capable = True
        self.core.operations.retry_failed_work(self.ctx, limit=32, include_terminal=True, dry_run=False)

    @invariant()
    def queued_questions_have_live_work_or_a_reclaimable_lease(self):
        lifecycle, evaluations, work = _candidate_rows(self.core)
        current = (
            {row["candidate_ref"]: row["processing_state"] for row in lifecycle},
            {row["evaluation_id"]: row["state"] for row in evaluations},
        )
        for heading, before, after in zip(
            ("Candidate debounce", "Candidate evaluations"), self.previous, current, strict=True
        ):
            check_transitions(state_table(heading), before, after)
        self.previous = current
        rows = {row["work_id"]: row for row in work}
        for evaluation in evaluations:
            assert evaluation["state"] in state_table("Candidate evaluations")
            if evaluation["state"] == "queued":
                row = rows[evaluation["work_id"]]
                assert row["state"] in {"pending", "leased", "failed"}
                if row["state"] == "failed":
                    with self.core.storage.read(self.ctx) as tx:
                        report = DoctorReport("hermes", "ok")
                        _candidate_queue_checks(report, tx)
                        preview = tx.work.retry_failed(now=self.core.clock.utc_now(), dry_run=True, limit=32)
                    assert preview["reconciled_evaluations"] > 0
                    assert f"work {row['work_id']}:" in str(report.checks)
                    assert row["last_error_code"] in str(report.checks)
                    assert "retry-failures --apply" in str(report.checks) or "restore the candidate" in str(
                        report.checks
                    )
                else:
                    now = datetime.fromisoformat(self.core.clock.utc_now().replace("Z", "+00:00"))
                    plan = next_wake(planning_config(self.ctx, capable=self.capable), now=now)
                    assert plan.due_at is not None or plan.blocked > 0
            elif evaluation["state"] == "failed":
                assert evaluation["failure_code"] and rows[evaluation["work_id"]]["state"] == "failed"
        for candidate in lifecycle:
            assert candidate["processing_state"] in state_table("Candidate debounce")
            if candidate["processing_state"] == "pending_evaluation":
                queued = any(e["state"] == "queued" for e in evaluations)
                assert (
                    queued
                    or settles_at(last_evidence_at=candidate["last_evidence_at"], created_at=candidate["created_at"])
                    is not None
                )


class InboxStates(RuleBasedStateMachine):
    """Replay, rekey and deferred authorization failures retain their durable next action."""

    def __init__(self, factory):
        super().__init__()
        self.core, self.ctx = factory()
        self.capable = True
        self.sequence = 0
        self.previous = {}

    @rule()
    def enqueue(self):
        self.sequence += 1
        capture_inbox.enqueue(
            self.core.storage,
            self.core.clock,
            self.ctx,
            source_event(source_event_key=f"TEST-inbox/{self.sequence}"),
            scope_id="TEST-scope",
            host_scope={},
        )

    @rule(available=st.booleans())
    def capability(self, available):
        self.capable = available

    @rule(rekey=st.booleans())
    def replay(self, rekey):
        def authorize(_scope):
            if not self.capable:
                raise OSError("TEST capability unavailable")
            return self.ctx.allowed_scope_ids

        action = capture_inbox.resolve_conflicted_ingress if rekey else capture_inbox.replay_inbox
        action(self.core.storage, self.core.clock, self.ctx, authorize=authorize, remaining_seconds=10)

    @rule(seconds=st.integers(0, 10000))
    def advance(self, seconds):
        advance(self.core, seconds)

    @rule(path=st.sampled_from(["replay", "rekey"]))
    def failed_revalidation(self, path):
        with self.core.storage.read(self.ctx) as tx:
            rows = list(tx._check().execute("SELECT * FROM capture_inbox LIMIT 1"))
        if rows:
            capture_inbox._defer(
                self.core.storage,
                self.core.clock,
                self.ctx,
                rows[0],
                ContractError("INPUT_INVALID", "ingress_context"),
                time.monotonic() + 10,
                path=path,
            )

    @rule()
    def repair(self):
        self.capable = True
        self.core.operations.retry_failed_work(self.ctx, limit=32, dry_run=False)

    @invariant()
    def every_retained_capture_is_due_deferred_or_diagnosed(self):
        with self.core.storage.read(self.ctx) as tx:
            rows = list(tx._check().execute("SELECT token,last_error_code FROM capture_inbox"))
            codes = [row[1] for row in rows]
            report = DoctorReport("hermes", "ok")
            _capture_queue_checks(report, self.ctx.binding, tx._check())
        current = {}
        for token, code in rows:
            current[token] = (
                "given_up"
                if given_up(code)
                else "deferred"
                if str(code).startswith("DEFERRED|")
                else "ready"
                if waiting(code)
                else "stopped"
            )
        removed = {token: "completed" for token in self.previous.keys() - current.keys()}
        check_transitions(state_table("Capture inbox"), self.previous, current | removed)
        self.previous = current
        now = datetime.fromisoformat(self.core.clock.utc_now().replace("Z", "+00:00"))
        for code in codes:
            assert replayable(code, now) or deferred_until(code, now) is not None or given_up(code) or not waiting(code)
        if any(waiting(code) for code in codes):
            plan = next_wake(planning_config(self.ctx, capable=False), now=now)
            assert plan.due_at is not None, "a replayable inbox entry has no automatic wake"
        if any(given_up(code) for code in codes):
            assert "retry-failures --apply" in str(report.checks)


@pytest.mark.parametrize("machine", [WorkStates, CandidateStates, InboxStates])
def test_durable_queue_state_sequences(tmp_path, machine):
    factory = store_factory(tmp_path)
    run_state_machine_as_test(lambda: machine(factory), settings=QUEUE_SETTINGS)


def test_candidate_state_actions_cover_suppression_release_and_failed_repair(tmp_path):
    machine = CandidateStates(store_factory(tmp_path))
    machine.sweep(1000)
    machine.begin()
    assert machine.began and machine.requests == 1
    machine.failure("input_invalid", False)
    assert _candidate_rows(machine.core)[1][0]["state"] == "failed"
    machine.queued_questions_have_live_work_or_a_reclaimable_lease()
    machine.repair()
    machine.begin()
    assert machine.began and machine.requests == 2
    machine.suppress()
    assert _candidate_rows(machine.core)[0][0]["processing_state"] == "blocked"
    machine.queued_questions_have_live_work_or_a_reclaimable_lease()
    machine.release_suppression()
    assert _candidate_rows(machine.core)[0][0]["processing_state"] != "blocked"
    machine.queued_questions_have_live_work_or_a_reclaimable_lease()
