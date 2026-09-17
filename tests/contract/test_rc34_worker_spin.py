"""A worker pass that can do nothing is not started again at once.

On 2026-09-17 two instances ran passes that could make no progress, back to back:
alpha every ~20 seconds, woken for one failed candidate evaluation the planner
counted as recoverable and the worker never recovers; delta every ~7.5 seconds,
each pass parking one of 2,548 embed items for want of a credential and the next
pass starting at once for the item after it.
"""
from __future__ import annotations

from datetime import timedelta
import sqlite3

from scope_recall.adapters.models import AuxiliaryModelError
from scope_recall.core.work_storage import AUTO_RECOVERABLE_WORK_TYPES
from scope_recall.runtime import scheduling
from scope_recall.runtime.scheduling import next_wake, supervise
from tests.contract.test_finite_supervisor import NOW, fixture, queue
from tests.contract.test_v11_claims import app, capture  # noqa: F401  (fixture)
from tests.contract.test_v11_worker import worker_app  # noqa: F401  (fixture)

EVERY_TYPE = {"purge", "rebuild_projection", "consolidate", "embed", "evaluate_candidate"}


def test_the_planner_wakes_only_for_failures_the_worker_recovers(tmp_path, monkeypatch):
    core, cfg, _path = fixture(tmp_path)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    queue(core, cfg, ref="TEST-candidate", kind="evaluate_candidate", state="failed",
          error="lease_exhausted", due=NOW - timedelta(hours=10))
    plan = next_wake(cfg, now=NOW)
    assert plan.due_at is None and plan.reason == "failed_terminal" and plan.failed == 1
    queue(core, cfg, ref="TEST-source", kind="consolidate", state="failed",
          error="lease_exhausted", due=NOW - timedelta(hours=10))
    assert next_wake(cfg, now=NOW).reason == "failure_cooldown"
    assert "evaluate_candidate" not in AUTO_RECOVERABLE_WORK_TYPES
    assert {"consolidate", "embed"} <= AUTO_RECOVERABLE_WORK_TYPES


def test_a_type_its_port_refused_before_any_attempt_is_reported_unavailable(worker_app):
    from scope_recall.core.worker import WorkerConfig, drain_worker

    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 向量缺少凭据一。")
    capture(core, ctx, "TEST 向量缺少凭据二。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()

    class MissingCredential:
        calls = 0

        def prepare_source(self, source, *, remaining_seconds=1.0):
            MissingCredential.calls += 1
            raise AuxiliaryModelError("credential_missing")

        def publish_source(self, prepared, **kwargs):
            raise AssertionError("nothing to publish")

    receipt = drain_worker(core.storage, clock, ctx, embed=MissingCredential(), remaining_seconds=5,
                           config=WorkerConfig("TEST-paused"))
    assert MissingCredential.calls == 1, "the type stands down after the first refusal"
    assert receipt.deferred == 1 and receipt.unavailable_work_types == ("embed",)
    with sqlite3.connect(core.storage.path) as conn:
        states = conn.execute("SELECT state,attempt FROM work_items WHERE work_type='embed' ORDER BY work_id").fetchall()
    assert states == [("pending", 0), ("pending", 0)]


def test_the_supervisor_sleeps_a_candidate_type_its_port_refused(tmp_path, monkeypatch):
    core, cfg, path = fixture(tmp_path, supervisor_seconds=600)
    monkeypatch.setattr(scheduling, "_capable_work_types", lambda config: set(EVERY_TYPE))
    queue(core, cfg, ref="TEST-candidate", kind="evaluate_candidate")
    elapsed = [0.0]
    calls = []

    def sleep(seconds):
        elapsed[0] += seconds

    def drain(_remaining):
        calls.append(elapsed[0])
        return 0, {"completed": 0, "unavailable_work_types": ["evaluate_candidate"]}

    supervise(path, drain, clock=lambda: elapsed[0], sleep=sleep, utc_now=lambda: NOW + timedelta(seconds=elapsed[0]))
    assert calls[:2] == [0, 300]
