"""The processing loop survives one bad pass, says when it did not, and can be handed credentials.

Two ways the queue stopped moving without anyone being told.

A supervisor that met any worker exit outside (0, 75, 124) marked itself non-accepting and
returned, so the loop stopped until the next five-minute wake.  Found by reading a control file
by hand on a live instance: ``state=failed reason=worker_failed drains=217``, 180 items still
queued, and nothing in any report mentioned it.

And a pass started by hand reached every provider with no credentials -- the scheduled wake
carries them in its environment and this entry point had no way to be given them -- so it
refused each item with ``credential_missing``, stood the work types down, and exited 0.  An
operator watching that receipt saw a success that had done nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scope_recall.runtime.scheduling import MAX_CONSECUTIVE_WORKER_FAILURES, WakePlan, supervise


class Control:
    """Whatever the supervisor wrote about itself, last write wins."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    @property
    def last(self) -> dict:
        return self.writes[-1] if self.writes else {}

    def state_at(self, index: int) -> str:
        return str(self.writes[index].get("state") or "")


def _supervise(tmp_path, codes, *, monkeypatch, max_drains=16):
    """Run a real supervisor whose passes return ``codes``, and capture its control writes."""
    from scope_recall.runtime import scheduling

    control = Control()
    clock = [0.0]

    class Recorder:
        def __init__(self, config) -> None:
            self.config = config
            self.value: dict = {"wake_revision": 1}

        def request(self) -> None:
            pass

        def read(self) -> dict:
            return dict(self.value)

        def update(self, **fields):
            self.value.update(fields)
            control.writes.append(dict(self.value))
            return dict(self.value)

        def close_if_unchanged(self, revision, **fields):
            self.update(accepting=False, **fields)
            return True

    class Config:
        supervisor_seconds = 600.0
        supervisor_max_drains = max_drains
        worker_min_interval_seconds = 0
        drain_seconds = 5.0
        auto_retry_cooldown_seconds = 60.0

    config = Config()
    monkeypatch.setattr(scheduling, "load_config", lambda path: config)
    monkeypatch.setattr(scheduling, "SupervisorControl", Recorder)
    monkeypatch.setattr(scheduling, "_acquire_ownership", lambda ctl: _Owner())
    monkeypatch.setattr(scheduling, "read_control", lambda cfg: {"enabled": True}, raising=False)
    import scope_recall.runtime.resume_entry as resume_entry
    monkeypatch.setattr(resume_entry, "read_control", lambda cfg: {"enabled": True})

    passes = iter(codes)
    made: list[int] = []

    def drain_once(remaining):
        clock[0] += 1.0
        code = next(passes, None)
        if code is None:
            # Past the script: an empty pass, so the supervisor's own
            # progress-followup drain does not extend the run.
            return 0, {"completed": 0}
        made.append(code)
        return code, {"completed": 1 if code == 0 else 0}

    def planner(cfg, *, now, unavailable_until):
        # Due in the past, so the loop drains rather than waiting out its window.
        return WakePlan("2000-01-01T00:00:00+00:00", "work_available", 1, 0, 0) if len(made) < len(codes) \
            else WakePlan(None, "idle", 0, 0, 0)

    def sleep(seconds):
        clock[0] += max(0.0, float(seconds))

    exit_code = supervise(tmp_path / "config.json", drain_once, clock=lambda: clock[0],
                          sleep=sleep, planner=planner)
    return exit_code, control, made


class _Owner:
    def __exit__(self, *args):
        return False


def test_one_failed_pass_does_not_stop_the_loop(tmp_path, monkeypatch):
    exit_code, control, made = _supervise(tmp_path, [1, 0, 0], monkeypatch=monkeypatch)
    assert made == [1, 0, 0], "the loop stopped at the first failure"
    assert control.last.get("accepting") is not False or control.last.get("reason") != "worker_failed"
    degraded = [w for w in control.writes if w.get("reason") == "worker_failed"]
    assert degraded and degraded[0].get("state") == "degraded", "the failure was not reported"
    assert degraded[0].get("worker_failures") == 1
    assert exit_code == 0


def test_a_worker_that_keeps_failing_is_stood_down(tmp_path, monkeypatch):
    codes = [1] * (MAX_CONSECUTIVE_WORKER_FAILURES + 2)
    exit_code, control, made = _supervise(tmp_path, codes, monkeypatch=monkeypatch)
    assert len(made) == MAX_CONSECUTIVE_WORKER_FAILURES, made
    assert control.last.get("state") == "failed" and control.last.get("accepting") is False
    assert control.last.get("worker_failures") == MAX_CONSECUTIVE_WORKER_FAILURES
    assert exit_code == 1


def test_a_success_forgets_the_earlier_failures(tmp_path, monkeypatch):
    """Two failures, a success, two more: a flaky pass must never accumulate into a stand-down."""
    _exit, control, made = _supervise(tmp_path, [1, 1, 0, 1, 1, 0], monkeypatch=monkeypatch)
    assert made == [1, 1, 0, 1, 1, 0], made
    assert control.last.get("state") != "failed"


# -- the doctor says so --------------------------------------------------------

def test_the_doctor_reports_a_loop_that_stood_down(tmp_path):
    from scope_recall.maintenance.doctor import DoctorReport, _check_supervisor

    (tmp_path / "runtime-supervisor-aaa.json").write_text(json.dumps(
        {"state": "failed", "reason": "worker_failed", "exit_code": 1, "drains": 217,
         "started_at": "2026-09-17T07:41:26Z", "worker_failures": 3}), encoding="utf-8")
    report = DoctorReport(host="hermes", status="ok")
    _check_supervisor(report, tmp_path)
    assert any(gap.startswith("supervisor_stood_down:1") for gap in report.capability_gaps), report.capability_gaps


def test_the_doctor_reports_a_loop_that_is_limping(tmp_path):
    from scope_recall.maintenance.doctor import DoctorReport, _check_supervisor

    (tmp_path / "runtime-supervisor-aaa.json").write_text(json.dumps(
        {"state": "degraded", "reason": "worker_failed", "drains": 9,
         "started_at": "2026-09-18T07:00:00Z", "worker_failures": 2}), encoding="utf-8")
    report = DoctorReport(host="hermes", status="ok")
    _check_supervisor(report, tmp_path)
    assert "worker_failures:2" in report.capability_gaps, report.capability_gaps


def test_an_operator_pause_is_not_a_failure(tmp_path):
    from scope_recall.maintenance.doctor import DoctorReport, _check_supervisor

    (tmp_path / "runtime-supervisor-aaa.json").write_text(json.dumps(
        {"state": "paused", "reason": "operator_pause", "drains": 0,
         "started_at": "2026-09-18T07:00:00Z", "finished_at": "2026-09-18T07:00:01Z"}), encoding="utf-8")
    report = DoctorReport(host="hermes", status="ok")
    _check_supervisor(report, tmp_path)
    assert not report.capability_gaps, report.capability_gaps


# -- a pass may be handed its credentials --------------------------------------

def test_a_pass_can_be_handed_the_credentials_a_wake_would_have_carried(tmp_path, monkeypatch):
    from scope_recall.runtime import worker_entry

    class Route:
        credential_env = "TEST_SCOPE_RECALL_KEY"

    class Auxiliary:
        embedding = Route()
        consolidation = None

    class Config:
        auxiliary = Auxiliary()

    env_file = tmp_path / "TEST-credentials.env"
    env_file.write_text("TEST_SCOPE_RECALL_KEY=TEST-value\nTEST_OTHER=ignored\n", encoding="utf-8")
    monkeypatch.setattr(worker_entry, "load_config", lambda path: Config())
    monkeypatch.delenv("TEST_SCOPE_RECALL_KEY", raising=False)
    monkeypatch.delenv("TEST_OTHER", raising=False)

    assert worker_entry.load_credential_environment(tmp_path / "config.json", env_file) == 1
    assert os.environ["TEST_SCOPE_RECALL_KEY"] == "TEST-value"
    assert "TEST_OTHER" not in os.environ, "only the names the config declares are read"


def test_the_environment_a_pass_was_started_with_wins(tmp_path, monkeypatch):
    """A scheduled wake's own environment is authoritative; the file never overrides it."""
    from scope_recall.runtime import worker_entry

    class Route:
        credential_env = "TEST_SCOPE_RECALL_KEY"

    class Auxiliary:
        embedding = Route()
        consolidation = None

    class Config:
        auxiliary = Auxiliary()

    env_file = tmp_path / "TEST-credentials.env"
    env_file.write_text("TEST_SCOPE_RECALL_KEY=TEST-from-file\n", encoding="utf-8")
    monkeypatch.setattr(worker_entry, "load_config", lambda path: Config())
    monkeypatch.setenv("TEST_SCOPE_RECALL_KEY", "TEST-from-the-wake")
    assert worker_entry.load_credential_environment(tmp_path / "config.json", env_file) == 0
    assert os.environ["TEST_SCOPE_RECALL_KEY"] == "TEST-from-the-wake"


def test_an_unusable_env_file_stops_the_pass_instead_of_starting_it_blind(tmp_path, monkeypatch):
    from scope_recall.runtime import worker_entry

    monkeypatch.setattr(worker_entry, "run_worker", lambda *a, **k: pytest.fail("the pass ran blind"))
    code = worker_entry.main(["--config", str(tmp_path / "config.json"),
                              "--env-file", str(tmp_path / "TEST-missing.env")])
    assert code == 2
