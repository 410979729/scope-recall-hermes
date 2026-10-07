"""A candidate still collecting evidence when a pass ends wakes its worker when it settles.

A candidate inside its quiet window is in no queue, so the wake plan names the moment it becomes ready; without it
the supervisor of a quiet conversation stands down before the window closes, and the candidate waits for the
channel's next session.  A question holds the evidence there was when it was queued, and evidence that came later
is asked about once it settles.  A candidate ready, and last changed, before the last pass that swept began was
seen by that sweep and is not waited for again, so a candidate the sweep has nothing to ask about cannot wake the
worker after every pass.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import itertools
import json
import sqlite3

import pytest

from scope_recall.core.candidate_debounce import MAX_DEFERRAL_SECONDS, QUIET_SECONDS, settle_reason, settles_at
from scope_recall.maintenance import doctor
from scope_recall.runtime import scheduling
from scope_recall.runtime.instance import RuntimeInstanceConfig
from scope_recall.runtime.scheduling import SupervisorControl, next_wake, supervise
from scope_recall.runtime.worker_entry import _receipt_payload
from scope_recall.core import worker as worker_module
from scope_recall.core.claims import Qualification
from test_r1_candidate_lifecycle import Evaluator, ModelRefusal, _candidate, _finish_source_work
from test_v11_claims import app, capture  # noqa: F401  (app is a fixture)

EVIDENCE = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)  # the claims fixture's clock
EVERY_TYPE = {"purge", "rebuild_projection", "consolidate", "evaluate_candidate", "embed"}


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("evaluated", [None, -3500, -600])
def test_settles_at_is_when_the_debounce_rule_first_gives_a_reason(evaluated):
    created = EVIDENCE - timedelta(hours=5)
    last_evaluated = None if evaluated is None else _stamp(EVIDENCE + timedelta(seconds=evaluated))
    moment = settles_at(last_evidence_at=_stamp(EVIDENCE), last_evaluated_at=last_evaluated,
                        created_at=_stamp(created))
    for offset in range(0, QUIET_SECONDS + 120, 5):
        now = EVIDENCE + timedelta(seconds=offset)
        waiting = settle_reason(now=_stamp(now), last_evidence_at=_stamp(EVIDENCE), last_evaluated_at=last_evaluated,
                                created_at=_stamp(created)) is None
        assert waiting == (now < moment), offset
    assert settles_at(last_evidence_at=None) is None
    # A deferral limit already past when the evidence came: ready as it comes, not before.
    assert settles_at(last_evidence_at=_stamp(EVIDENCE),
                      last_evaluated_at=_stamp(EVIDENCE - timedelta(seconds=MAX_DEFERRAL_SECONDS + 60))) == EVIDENCE


def _collecting(core, ctx):
    """A candidate judged half an hour before the person's last message brought it new first-hand evidence at
    ``EVIDENCE``, with no evaluation queued: what the last pass of a conversation leaves behind."""
    core.clock.now = _stamp(EVIDENCE - timedelta(minutes=30))
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    core.clock.now = _stamp(EVIDENCE)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-214/new-evidence")
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM candidate_evaluations WHERE state='queued'").fetchone()[0] == 0
        assert conn.execute("SELECT processing_state,reason,last_evidence_at FROM candidate_lifecycle").fetchone() == (
            "pending_evaluation", "new_evidence", _stamp(EVIDENCE))


def _config(ctx, **settings):
    raw = dict(binding=dict(agent_id=ctx.binding.agent_id, installation_id=ctx.binding.installation_id,
                            data_directory=str(ctx.binding.data_directory), scope_ids=sorted(ctx.binding.scope_ids),
                            test_mode=True),
               session_id="TEST-session", allowed_scope_ids=["TEST-scope"], actor_origin="human_direct",
               project_id="TEST-project", branch_id="TEST-main", supervisor_seconds=3600, supervisor_max_drains=8,
               worker_min_interval_seconds=1, auxiliary=dict(external_embedding=False, external_consolidation=False))
    raw.update(settings)
    path = ctx.binding.data_directory / "runtime-config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return RuntimeInstanceConfig.from_mapping(raw), path


def _passed(config, moment):
    SupervisorControl(config).update(installation_id=config.binding.installation_id, last_pass_at=_stamp(moment))


def test_the_plan_waits_for_a_collecting_candidate_and_never_again_once_a_pass_has_seen_it(app, monkeypatch):
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    ready = EVIDENCE + timedelta(seconds=QUIET_SECONDS)
    # Without a consolidation route nothing evaluates, so nothing is worth waking for.
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=60)).reason == "idle"
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))

    plan = next_wake(config, now=EVIDENCE + timedelta(seconds=60))
    assert (plan.due_at, plan.reason) == (_stamp(ready), "candidate_settle_window")
    # Woken at the moment, it drains: the last pass began before the candidate was ready.
    _passed(config, EVIDENCE)
    late = next_wake(config, now=ready + timedelta(seconds=30))
    assert (late.due_at, late.reason) == (_stamp(ready + timedelta(seconds=30)), "candidate_settle_window")
    # A pass that began once it was ready swept it, scheduling it or not: no wake for it again.
    _passed(config, ready + timedelta(seconds=1))
    assert next_wake(config, now=ready + timedelta(seconds=60)).reason == "idle"
    # A provider cooling down and a spent day's budget push the wake as they push any candidate work.
    _passed(config, EVIDENCE)
    for work_type in ("evaluate_candidate", "consolidate"):
        cooled = next_wake(config, now=EVIDENCE + timedelta(seconds=60),
                           unavailable_until={work_type: ready + timedelta(minutes=5)})
        assert (cooled.due_at, cooled.reason) == (_stamp(ready + timedelta(minutes=5)), "capability_cooldown")
    capped = replace(config, daily_work_limit=256)
    day = capped.binding.data_directory / "runtime-worker-day.json"
    day.write_text(json.dumps({"installation_id": capped.binding.installation_id, "day": "2026-09-06",
                               "used": capped.daily_work_limit}), encoding="utf-8")
    spent = next_wake(capped, now=EVIDENCE + timedelta(seconds=60))
    assert (spent.due_at, spent.reason) == ("2026-09-07T00:00:00Z", "daily_queue_budget")
    day.unlink()
    # A queued evaluation is a look already taken.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_evaluations SET state='queued' WHERE evaluation_id=(SELECT max(evaluation_id) "
                     "FROM candidate_evaluations)")
        conn.commit()
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=60)).reason != "candidate_settle_window"


@pytest.mark.parametrize("change", [
    "UPDATE candidate_lifecycle SET reason='authority_revoked'",
    "UPDATE claims SET read_blocked=1",
    "UPDATE claims SET suppressed=1",
    "UPDATE claim_versions SET state='active'",
])
def test_a_candidate_no_sweep_would_ask_about_wakes_no_worker(app, monkeypatch, change):
    """Revoked authority, a blocked or muted claim, and a claim no longer proposed or disputed: the sweep leaves
    each alone, and so does the plan."""
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=60)).reason == "candidate_settle_window"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(change)
        conn.commit()
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=60)).reason == "idle"


def test_a_pass_on_record_without_a_fraction_still_sees_a_later_readiness(app, monkeypatch):
    """The read compares stamps as text, where ``12:00:00Z`` sorts after ``12:00:00.500000Z``: a second's margin keeps
    evidence from just inside the window before the last pass in it."""
    core, ctx = app
    core.clock.now = _stamp(EVIDENCE - timedelta(minutes=30))
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    evidence = EVIDENCE + timedelta(microseconds=500000)
    core.clock.now = _stamp(evidence)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-214/fraction")
    _finish_source_work(core)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    ready = evidence + timedelta(seconds=QUIET_SECONDS)
    _passed(config, ready - timedelta(microseconds=500000))  # written as 12:15:00Z
    plan = next_wake(config, now=ready + timedelta(seconds=30))
    assert (plan.due_at, plan.reason) == (_stamp(ready + timedelta(seconds=30)), "candidate_settle_window")


def test_another_partition_s_candidate_wakes_no_worker_here(app, monkeypatch):
    core, ctx = app
    _collecting(core, ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    other, _path = _config(ctx, project_id="TEST-other-project")
    assert next_wake(other, now=EVIDENCE + timedelta(seconds=60)).reason == "idle"


def test_the_supervisor_waits_for_the_window_and_drains_when_it_closes(app, monkeypatch):
    """The pass after the person's last message finds nothing due; the supervisor waits for the window."""
    core, ctx = app
    _collecting(core, ctx)
    config, path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    elapsed = [60.0]   # the last message came a minute before the worker woke
    drains: list[float] = []

    def utc_now():
        return EVIDENCE + timedelta(seconds=elapsed[0])

    def sleep(seconds):
        elapsed[0] += max(0.0, float(seconds))

    def drain_once(remaining):
        drains.append(elapsed[0])
        elapsed[0] += 2.0
        return 0, {"completed": 0, "settle_swept": True}

    assert supervise(path, drain_once, clock=lambda: elapsed[0], sleep=sleep, utc_now=utc_now) == 0
    assert drains[0] == 60.0 and len(drains) == 2, drains
    assert QUIET_SECONDS <= drains[1] < QUIET_SECONDS + 60, "drained once the window closed, not before"
    state = SupervisorControl(config).read()
    assert state["state"] == "idle" and state["last_pass_at"] == _stamp(EVIDENCE + timedelta(seconds=drains[1]))


@pytest.mark.parametrize("passes,recorded", [
    # A busy pass (75) never ran, a failed one (1) may have stopped before its sweep, and one that finished without
    # looking at the candidates (a provider hold, no evaluator, a purge-only pass, a full evaluation queue) saw none
    # of them ready: none of them is the last pass.
    ([(75, {}), (1, {}), (0, {"settle_swept": False})], False),
    ([(124, {"settle_swept": True})], False),
    ([(0, {"settle_swept": False}), (0, {"settle_swept": True})], True),
])
def test_only_a_pass_that_swept_the_candidates_is_recorded(app, passes, recorded):
    core, ctx = app
    config, path = _config(ctx, supervisor_max_drains=len(passes))
    outcomes = iter(passes)
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += max(0.0, float(seconds))

    supervise(path, lambda remaining: next(outcomes), clock=lambda: elapsed[0], sleep=sleep,
              planner=lambda cfg, *, now, unavailable_until: scheduling.WakePlan("2000-01-01T00:00:00Z",
                                                                                 "work_available"))
    state = SupervisorControl(config).read()
    assert state["drains"] == len(passes) and ("last_pass_at" in state) == recorded


def _line(ctx, receipt):
    """What the worker prints for the supervisor to read."""
    config, _path = _config(ctx)
    payload = _receipt_payload(config, receipt, [])
    return payload["settle_swept"], payload["settle_partial"]


def test_a_pass_says_whether_it_swept_the_candidates(app, monkeypatch):
    core, ctx = app
    _collecting(core, ctx)
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    assert (receipt.settle_swept, receipt.settle_partial) == _line(ctx, receipt) == (True, False)
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10)
    assert (receipt.settle_swept, receipt.settle_partial) == _line(ctx, receipt) == (False, False), "no evaluator"
    monkeypatch.setattr(worker_module, "CANDIDATE_QUEUE_CEILING", 0)
    receipt = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    assert (receipt.settle_swept, receipt.settle_partial) == (False, False), "a full evaluation queue"


def test_the_doctor_names_work_and_candidates_that_waited_a_day(app, monkeypatch):
    """Read for the store, every partition: a project's queue is worked only by a worker of that project."""
    core, ctx = app
    _collecting(core, ctx)
    _config(ctx)
    old = _stamp(datetime.now(timezone.utc) - timedelta(days=2))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET updated_at=?,last_evidence_at=?", (old, old))
        conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,
                        state,available_at) VALUES ('rebuild_projection','TEST-elsewhere',1,'TEST-scope',
                        'TEST-other-project','TEST-main','pending',?)""", (old,))
        conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,
                        state,available_at) VALUES ('embed','TEST-no-route',1,'TEST-scope','TEST-project','TEST-main',
                        'pending',?)""", (old,))
        conn.commit()
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    # Without an evaluator route, waiting candidates wait by design.
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: {"purge", "rebuild_projection"})
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert {row["project_id"] for row in result.unreached} == {"TEST-other-project"}
    # A candidate pending for another reason than new evidence is no finding either.
    monkeypatch.setattr(scheduling, "_capable_work_types",
                        lambda config: {"purge", "rebuild_projection", "consolidate", "evaluate_candidate"})
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET reason='evidence_settled'")
        conn.commit()
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert {row["project_id"] for row in result.unreached} == {"TEST-other-project"}
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET reason='new_evidence'")
        conn.commit()
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert "due_work_unreached" in result.capability_gaps
    found = {row["project_id"]: (row["work"], row["candidates"]) for row in result.unreached}
    # The embed waits by design (no embedding route) and is left out.
    assert found == {"TEST-other-project": (1, 0), "TEST-project": (0, 1)}
    line = next(check for check in result.checks if check["name"] == "due_work_unreached")
    assert line["detail"].startswith("1 work items and 1 candidates with new evidence have waited more than 24 h")
    # Scope ids carry chat and account ids: the line counts, ``unreached`` names them.
    assert "TEST-scope" not in line["detail"] and "2 partition(s)" in line["detail"]
    # It asks for attention: the store records no time a pass looked at an item, so a backlog is named too.
    assert "due_work_unreached" in doctor._NON_ACTIONABLE_GAPS
    # A lease that ran out a day ago is released only by a pass of its own partition; work a provider holds is
    # reported by its own checks.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,
                        state,available_at,lease_owner,lease_until) VALUES ('rebuild_projection','TEST-leased',1,
                        'TEST-scope','TEST-third-project','TEST-main','leased',?,'TEST-owner',?)""", (old, old))
        conn.commit()
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert {row["project_id"]: row["work"] for row in result.unreached}.get("TEST-third-project") == 1
    monkeypatch.setattr(doctor, "provider_holds", lambda auxiliary, now=None: {"rebuild_projection": ("TEST", 0.0)})
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert {row["project_id"] for row in result.unreached} == {"TEST-project"}
    monkeypatch.undo()
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    monkeypatch.setattr(scheduling, "_capable_work_types",
                        lambda config: {"purge", "rebuild_projection", "consolidate", "evaluate_candidate"})
    # Within the day it is no finding.
    with sqlite3.connect(core.storage.path) as conn:
        recent = _stamp(datetime.now(timezone.utc) - timedelta(hours=2))
        conn.execute("UPDATE candidate_lifecycle SET updated_at=?,last_evidence_at=?", (recent, recent))
        conn.execute("UPDATE work_items SET available_at=? WHERE state='pending'", (recent,))
        conn.execute("UPDATE work_items SET lease_until=? WHERE state='leased'", (recent,))
        conn.commit()
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert "due_work_unreached" not in result.capability_gaps and result.unreached == []


def _doctor(core, ctx, monkeypatch):
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    return doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)


def _days_ago(*days):
    """Whole-second stamps that many days before now, all from one reading of the clock."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return [_stamp(now - timedelta(days=count)) for count in days]


@pytest.mark.parametrize("change", [
    "UPDATE candidate_evaluations SET state='queued' WHERE evaluation_id=(SELECT max(evaluation_id) "
    "FROM candidate_evaluations)",
    "UPDATE claims SET read_blocked=1",
    "UPDATE claims SET suppressed=1",
])
def test_the_doctor_leaves_out_a_candidate_queued_blocked_or_muted(app, monkeypatch, change):
    core, ctx = app
    _collecting(core, ctx)
    _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    [old] = _days_ago(2)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET updated_at=?,last_evidence_at=?", (old, old))
        conn.commit()
    assert [row["candidates"] for row in _doctor(core, ctx, monkeypatch).unreached] == [1]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(change)
        conn.commit()
    assert _doctor(core, ctx, monkeypatch).unreached == []


def test_the_doctor_names_each_partition_from_its_oldest_wait(app, monkeypatch):
    """A partition's oldest wait is the earliest of its work and candidates; the longest-waiting partition comes
    first.  Without a runtime config only the work every installation can do counts."""
    core, ctx = app
    _collecting(core, ctx)
    _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    two, three, four = _days_ago(2, 3, 4)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET updated_at=?,last_evidence_at=?", (two, two))
        # The store lists partitions by name, so the longer wait goes to the partition named last.
        for project, since in (("TEST-project", four), ("TEST-other-project", three)):
            conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,
                            branch_id,state,available_at) VALUES ('rebuild_projection',?,1,'TEST-scope',?,'TEST-main',
                            'pending',?)""", (f"TEST-{project}", project, since))
        conn.commit()
    found = [(row["project_id"], row["work"], row["candidates"], row["oldest"])
             for row in _doctor(core, ctx, monkeypatch).unreached]
    assert found == [("TEST-project", 1, 1, four), ("TEST-other-project", 1, 0, three)]
    (ctx.binding.data_directory / "runtime-config.json").unlink()
    found = [(row["project_id"], row["work"], row["candidates"])
             for row in _doctor(core, ctx, monkeypatch).unreached]
    assert found == [("TEST-project", 1, 0), ("TEST-other-project", 1, 0)]


def test_without_a_pass_on_record_a_ready_candidate_wakes_the_worker_once(app, monkeypatch):
    """No control file has the record right after an upgrade, and a session's worker records into its own audience's
    file: with no pass on record, every ready candidate counts, once."""
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    later = EVIDENCE + timedelta(seconds=QUIET_SECONDS, hours=3)
    plan = next_wake(config, now=later)
    assert (plan.due_at, plan.reason) == (_stamp(later), "candidate_settle_window")
    _passed(config, later)
    assert next_wake(config, now=later + timedelta(seconds=30)).reason == "idle"


def _unswept(config, moment):
    SupervisorControl(config).update(installation_id=config.binding.installation_id, unswept_pass_at=_stamp(moment))


def test_a_pass_that_did_not_sweep_is_not_repeated_for_the_candidates_at_once(app, monkeypatch):
    """A full evaluation queue, a pass kept out or cut short: the next pass would find the same, so the settle wake
    waits ``SETTLE_RETRY_SECONDS``."""
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    ready = EVIDENCE + timedelta(seconds=QUIET_SECONDS)
    _passed(config, EVIDENCE)
    _unswept(config, ready + timedelta(seconds=5))
    plan = next_wake(config, now=ready + timedelta(seconds=40))
    retry = ready + timedelta(seconds=5 + scheduling.SETTLE_RETRY_SECONDS)
    assert (plan.due_at, plan.reason) == (_stamp(retry), "candidate_settle_window")
    # A later pass that swept wins over the one that did not.
    _passed(config, ready + timedelta(seconds=10))
    assert next_wake(config, now=ready + timedelta(seconds=40)).reason == "idle"


def _run(path, outcomes, *, start, planner=None):
    """Supervise with a clock that sleeps as asked; the times passes began, from ``EVIDENCE``."""
    elapsed = [float(start)]
    drains: list[float] = []

    def sleep(seconds):
        elapsed[0] += max(0.0, float(seconds))

    def drain_once(remaining):
        drains.append(elapsed[0])
        elapsed[0] += 2.0
        return next(outcomes)

    extra = {} if planner is None else {"planner": planner}
    supervise(path, drain_once, clock=lambda: elapsed[0], sleep=sleep,
              utc_now=lambda: EVIDENCE + timedelta(seconds=elapsed[0]), **extra)
    return drains


def test_the_supervisor_does_not_spin_on_passes_that_cannot_sweep(app, monkeypatch):
    core, ctx = app
    _collecting(core, ctx)
    config, path = _config(ctx, supervisor_seconds=3600, supervisor_max_drains=64)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    drains = _run(path, itertools.repeat((0, {"completed": 0, "settle_swept": False})), start=QUIET_SECONDS + 60)
    assert 2 <= len(drains) <= 5, drains
    assert all(later - earlier >= scheduling.SETTLE_RETRY_SECONDS for earlier, later in zip(drains, drains[1:])), drains


def test_a_sweep_that_stopped_at_its_page_goes_on_with_the_rest(app, monkeypatch):
    """One sweep takes a page of candidates.  One that filled its page is not the last pass, so the next pass
    sweeps the rest of a burst."""
    core, ctx = app
    _collecting(core, ctx)
    config, path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    _passed(config, EVIDENCE - timedelta(minutes=1))
    passes = [(0, {"completed": 0, "settle_partial": True}), (0, {"completed": 0, "settle_swept": True})]
    drains = _run(path, iter(passes), start=QUIET_SECONDS + 60)
    assert len(drains) == 2 and drains[1] - drains[0] < 60, drains
    assert SupervisorControl(config).read()["last_pass_at"] == _stamp(EVIDENCE + timedelta(seconds=drains[1]))


def test_a_pass_says_when_its_sweep_stopped_at_its_page(app, monkeypatch):
    from scope_recall.core.candidate_sweeps import CandidateSweeps

    core, ctx = app
    _collecting(core, ctx)
    monkeypatch.setattr(CandidateSweeps, "settled_to_schedule",
                        lambda self, *, now, limit=16, rule_version=None: tuple(("TEST-ref", 1) for _ in range(limit)))
    receipt = core.drain_worker(ctx, max_items=2, remaining_seconds=10, consolidation=Evaluator())
    assert (receipt.settle_swept, receipt.settle_partial) == _line(ctx, receipt) == (False, True)


@pytest.mark.parametrize("field,value", [("project_id", "TEST-other-project"),
                                         ("allowed_scope_ids", ["TEST-scope", "TEST-scope-2"])])
def test_a_supervisor_whose_audience_changes_stands_down(app, field, value):
    """Its control file is the old audience's: planning from the new one, it would keep writing the old file."""
    _core, ctx = app
    binding = dict(agent_id=ctx.binding.agent_id, installation_id=ctx.binding.installation_id,
                   data_directory=str(ctx.binding.data_directory), scope_ids=["TEST-scope", "TEST-scope-2"],
                   test_mode=True)
    config, path = _config(ctx, supervisor_max_drains=4, binding=binding)

    def outcomes():
        while True:
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw[field] = value
            path.write_text(json.dumps(raw), encoding="utf-8")
            yield 0, {"completed": 0, "settle_swept": True}

    now = scheduling.WakePlan(_stamp(EVIDENCE), "work_available")
    drains = _run(path, outcomes(), start=0, planner=lambda cfg, *, now_=None, **kwargs: now)
    state = SupervisorControl(config).read()
    assert len(drains) == 1 and (state["state"], state["reason"]) == ("suspended", "config_changed"), state


def test_a_route_without_a_budget_ledger_is_not_planned_for():
    """A pass does not use it (``runtime/auxiliary``): planned for, its work woke a pass after every pass."""
    from types import SimpleNamespace

    def config(ledger):
        return SimpleNamespace(vector=object(), auxiliary=SimpleNamespace(
            ledger_path=ledger, external_consolidation=True, consolidation=object(), external_embedding=True,
            embedding=object()))

    assert scheduling._capable_work_types(config(None)) == {"purge", "rebuild_projection"}
    assert scheduling._capable_work_types(config("TEST-ledger")) == EVERY_TYPE


def test_the_scheduled_wake_launches_for_a_candidate_ready_with_no_pass_on_record(app, monkeypatch):
    """Right after an upgrade no control file has the record: the scheduled wake launches a worker, once, for a
    candidate that became ready meanwhile."""
    from pathlib import Path
    import sys
    from types import SimpleNamespace

    from scope_recall.maintenance import autostart
    from scope_recall.runtime.resume_entry import resume_once

    core, ctx = app
    _collecting(core, ctx)
    config, path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    monkeypatch.setattr(autostart, "_windows", lambda: False)
    autostart.apply(autostart.plan(path, Path(sys.executable), user_id=None))
    ready = EVIDENCE + timedelta(seconds=QUIET_SECONDS)
    launched = []

    def launcher(*args, **kwargs):
        launched.append(args)
        return SimpleNamespace(pid=7)

    assert resume_once(path, launcher=launcher, now=ready - timedelta(minutes=2))["launched"] is False
    assert resume_once(path, launcher=launcher, now=ready + timedelta(minutes=3))["launched"] is True
    # Once a pass that swept is on record, the same candidate wakes nothing again.
    _passed(config, ready + timedelta(minutes=3))
    assert resume_once(path, launcher=launcher, now=ready + timedelta(minutes=8))["status"] == "idle"


def test_a_candidate_another_worker_put_its_question_to_wakes_no_worker(app, monkeypatch):
    """Another audience's worker (a session's) put the question: the scheduled wake, which reads only its own record,
    does not repeat that pass."""
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    ready = EVIDENCE + timedelta(seconds=QUIET_SECONDS)
    later = ready + timedelta(hours=1)
    assert next_wake(config, now=later).reason == "candidate_settle_window", "no pass on record: it counts"
    core.clock.now = _stamp(ready + timedelta(minutes=1))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    assert next_wake(config, now=later).reason == "idle"


def test_a_candidate_asked_about_as_its_evidence_came_wakes_no_worker(app, monkeypatch):
    """Last judged over an hour before, a candidate is asked about in the write that links its new evidence (the
    deferral limit), so the question holds that evidence, and nothing wakes at its readiness a window later, where a
    pass would ask nothing.  Most candidates of a live store are asked this way."""
    core, ctx = app
    core.clock.now = _stamp(EVIDENCE - timedelta(hours=2))
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    core.clock.now = _stamp(EVIDENCE)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-214/asked-at-once")
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_evidence_at FROM candidate_lifecycle").fetchone()[0] == _stamp(EVIDENCE)
        assert conn.execute("SELECT state,created_at FROM candidate_evaluations ORDER BY evaluation_id DESC"
                            ).fetchone() == ("queued", _stamp(EVIDENCE))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=60)).reason == "idle"
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=QUIET_SECONDS + 60)).reason == "idle"
    # Stores written by earlier releases hold ``+00:00`` stamps too: the same moment, written otherwise.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_evaluations SET created_at='2026-09-06T12:00:00.000000+00:00' "
                     "WHERE evaluation_id=(SELECT max(evaluation_id) FROM candidate_evaluations)")
        conn.commit()
    assert next_wake(config, now=EVIDENCE + timedelta(seconds=QUIET_SECONDS + 60)).reason == "idle"


def _questions(core):
    with sqlite3.connect(core.storage.path) as conn:
        return [row[0] for row in conn.execute("SELECT state FROM candidate_evaluations ORDER BY evaluation_id")]


class _Failing:
    def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
        raise ModelRefusal("network_error")


@pytest.mark.parametrize("closed,later", [("answered", timedelta(minutes=1)), ("failed", timedelta(minutes=1)),
                                          ("obsolete", timedelta(minutes=1)),
                                          ("answered", timedelta(microseconds=100))])
def test_evidence_that_came_while_a_question_waited_wakes_the_worker_once_it_settles(app, monkeypatch, closed, later):
    """An evaluation holds the evidence there was when it was queued.  However it ends (answered, failed or
    retired), evidence that came while it waited is asked about once it settles, also when the pass that ended it
    is on record as having swept, and also when it came within the question's millisecond."""
    core, ctx = app
    saved, _source, proposal, _registration = _candidate(core, ctx)  # its question is queued at EVIDENCE
    _finish_source_work(core)
    core.clock.now = _stamp(EVIDENCE + later)
    capture(core, ctx, "又发现 entity-blue property-blue 的相关证据。", key="TEST-214/while-queued")
    _finish_source_work(core)

    def write_history():
        with core.storage.write(ctx) as tx:
            head = tx.claims.version(saved.ref, saved.revision)
            tx.claims.append("TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_history"),
                             recorded_at=core.clock.utc_now(), previous=head, advance_head=False)

    closer = {"answered": Evaluator(), "failed": _Failing(),
              "obsolete": Evaluator(proposal, callback=write_history)}[closed]
    answered = EVIDENCE + timedelta(minutes=20)  # the new evidence settled at +16 min
    core.clock.now = _stamp(answered)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=closer)
    assert _questions(core) == [{"answered": "waiting_evidence", "failed": "failed", "obsolete": "obsolete"}[closed]]
    with core.storage.read(ctx) as tx:
        assert tx.candidates.settled_to_schedule(now=core.clock.now) == ((saved.ref, saved.revision),)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    # With no pass on record, and with the pass that closed it on record: it began before it closed, here a second
    # before or at the same moment on this clock.
    for record in (None, answered - timedelta(seconds=1), answered):
        if record is not None:
            _passed(config, record)
        plan = next_wake(config, now=answered + timedelta(seconds=30))
        assert (plan.due_at, plan.reason) == (_stamp(answered + timedelta(seconds=30)), "candidate_settle_window")
    # The next pass puts the new question, and once it is answered nothing is left to wake for.
    core.clock.now = _stamp(answered + timedelta(minutes=1))
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    assert _questions(core)[1:] == ["waiting_evidence"]
    # The failed question's work stays failed for good, which the plan reports without waking for it.
    plan = next_wake(config, now=answered + timedelta(minutes=2))
    assert (plan.due_at, plan.reason) == (None, "failed_terminal" if closed == "failed" else "idle")


def _state_after(path, config, outcomes, **stamps):
    if stamps:
        SupervisorControl(config).update(installation_id=config.binding.installation_id,
                                         **{name: _stamp(moment) for name, moment in stamps.items()})
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += max(0.0, float(seconds))

    plan = scheduling.WakePlan("2000-01-01T00:00:00Z", "work_available")
    supervise(path, lambda remaining: next(outcomes), clock=lambda: elapsed[0], sleep=sleep,
              utc_now=lambda: EVIDENCE + timedelta(seconds=elapsed[0]), planner=lambda cfg, **kwargs: plan)
    return SupervisorControl(config).read()


@pytest.mark.parametrize("failures", [1, 3])
def test_a_failing_pass_holds_the_settle_wake(app, failures):
    """A failing pass holds the settle wake as a pass that could not sweep does, so a worker that keeps failing is
    not launched for the candidates at every scheduled wake.  The hold counts from the last failing pass, also the one
    that stops the supervisor."""
    _core, ctx = app
    config, path = _config(ctx, supervisor_max_drains=failures)
    elapsed = [0.0]
    began: list[str] = []

    def drain_once(remaining):
        began.append(_stamp(EVIDENCE + timedelta(seconds=elapsed[0])))
        elapsed[0] += 2.0
        return 1, {}

    def sleep(seconds):
        elapsed[0] += max(0.0, float(seconds))

    plan = scheduling.WakePlan("2000-01-01T00:00:00Z", "work_available")
    supervise(path, drain_once, clock=lambda: elapsed[0], sleep=sleep,
              utc_now=lambda: EVIDENCE + timedelta(seconds=elapsed[0]), planner=lambda cfg, **kwargs: plan)
    state = SupervisorControl(config).read()
    assert len(began) == failures and state["unswept_pass_at"] == began[-1] and "last_pass_at" not in state
    assert state["state"] == ("failed" if failures == 3 else "suspended")


def test_a_partial_sweep_clears_an_earlier_hold(app):
    _core, ctx = app
    config, path = _config(ctx, supervisor_max_drains=1)
    state = _state_after(path, config, iter([(0, {"settle_partial": True})]),
                         unswept_pass_at=EVIDENCE - timedelta(minutes=5))
    assert state["unswept_pass_at"] is None and "last_pass_at" not in state


def test_a_hold_older_than_the_last_sweep_holds_nothing(app, monkeypatch):
    core, ctx = app
    _collecting(core, ctx)
    config, _path = _config(ctx)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    _unswept(config, EVIDENCE + timedelta(seconds=800))
    _passed(config, EVIDENCE + timedelta(seconds=850))
    now = EVIDENCE + timedelta(seconds=QUIET_SECONDS + 40)
    assert next_wake(config, now=now).due_at == _stamp(now)


def test_the_doctor_reports_an_operator_timer(app, monkeypatch):
    from pathlib import Path
    import sys

    from scope_recall.maintenance import autostart

    _core, ctx = app
    _config_, path = _config(ctx)
    monkeypatch.setattr(autostart, "_windows", lambda: False)
    autostart.apply(autostart.plan(path, Path(sys.executable), user_id=None))
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert result.autostart_status == "operator_timer"
