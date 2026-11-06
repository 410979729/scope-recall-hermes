"""A worker pass that can do nothing is not started again at once.

Passes that can make no progress must not run back to back: one woken for a failed
candidate evaluation the planner counts as recoverable and the worker never recovers,
or one parking an embed item for want of a credential with the next pass starting at
once for the item after it.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from scope_recall.core.work_storage import AUTO_RECOVERABLE_WORK_TYPES
from scope_recall.runtime import scheduling
from scope_recall.runtime.models import AuxiliaryModelError
from scope_recall.runtime.scheduling import next_wake, supervise

from tests.contract.test_claims import app, capture  # noqa: F401  (fixture)
from tests.contract.test_finite_supervisor import NOW, fixture, queue
from tests.contract.test_worker import worker_app  # noqa: F401  (fixture)

EVERY_TYPE = {"purge", "rebuild_projection", "consolidate", "embed", "evaluate_candidate"}


def test_the_planner_wakes_only_for_failures_the_worker_recovers(tmp_path, monkeypatch):
    core, cfg, _path = fixture(tmp_path)
    monkeypatch.setattr(scheduling, "capable_work_types", lambda config: set(EVERY_TYPE))
    queue(
        core,
        cfg,
        ref="TEST-candidate",
        kind="evaluate_candidate",
        state="failed",
        error="lease_exhausted",
        due=NOW - timedelta(hours=10),
    )
    plan = next_wake(cfg, now=NOW)
    assert plan.due_at is None and plan.reason == "failed_terminal" and plan.failed == 1
    queue(
        core,
        cfg,
        ref="TEST-source",
        kind="consolidate",
        state="failed",
        error="lease_exhausted",
        due=NOW - timedelta(hours=10),
    )
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

    receipt = drain_worker(
        core.storage, clock, ctx, embed=MissingCredential(), remaining_seconds=5, config=WorkerConfig("TEST-paused")
    )
    assert MissingCredential.calls == 1, "the type stands down after the first refusal"
    assert receipt.deferred == 1 and receipt.unavailable_work_types == ("embed",)
    with sqlite3.connect(core.storage.path) as conn:
        states = conn.execute(
            "SELECT state,attempt FROM work_items WHERE work_type='embed' ORDER BY work_id"
        ).fetchall()
    assert states == [("pending", 0), ("pending", 0)]


def test_the_supervisor_sleeps_a_candidate_type_its_port_refused(tmp_path, monkeypatch):
    core, cfg, path = fixture(tmp_path, supervisor_seconds=600)
    monkeypatch.setattr(scheduling, "capable_work_types", lambda config: set(EVERY_TYPE))
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


def _credential_runtime(tmp_path):
    from dataclasses import replace

    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    from scope_recall.runtime.instance import RuntimeInstance

    from tests.contract.test_runtime_auxiliary import _runtime_config

    core, cfg, _ = fixture(tmp_path)
    auxiliary, _, _ = _runtime_config(tmp_path)
    cfg = replace(cfg, auxiliary=auxiliary)
    for kind in ("consolidate", "evaluate_candidate", "embed", "rebuild_projection"):
        queue(core, cfg, ref=f"TEST-missing-{kind}", kind=kind)
    ports = build_auxiliary_runtime(auxiliary)
    core.consolidation = ports.consolidation
    return RuntimeInstance(config=cfg, core=core, auxiliary=ports, _owned_resources=[ports])


def _leases(runtime):
    with sqlite3.connect(runtime.core.storage.path) as conn:
        return conn.execute(
            "SELECT work_type,lease_token,attempt,last_error_code FROM work_items ORDER BY work_type"
        ).fetchall()


def test_missing_credentials_do_not_claim_work_and_recover_after_recheck(tmp_path, monkeypatch):
    from scope_recall.runtime import auxiliary

    elapsed = [0.0]
    monkeypatch.setattr(auxiliary, "monotonic", lambda: elapsed[0], raising=False)
    monkeypatch.delenv("SCOPE_RECALL_TEST_CHAT_KEY", raising=False)
    monkeypatch.delenv("SCOPE_RECALL_TEST_EMBED_KEY", raising=False)
    runtime = _credential_runtime(tmp_path)
    try:
        before = _leases(runtime)
        receipt = runtime.drain(embed=object())
        assert _leases(runtime) == before
        assert set(receipt.unavailable_work_types) == EVERY_TYPE - {"purge"}
        assert len(runtime.background_gaps) == 2
        monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "TEST-chat-value")
        monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "TEST-embed-value")
        elapsed[0] = 59.0
        runtime.drain(embed=object())
        assert _leases(runtime) == before
        elapsed[0] = 60.0
        runtime.drain(embed=object())
        assert all(row[1] > 0 for row in _leases(runtime)), _leases(runtime)
        assert runtime.background_gaps == ()
    finally:
        runtime.close()


def test_missing_credentials_are_logged_once_across_runtime_instances(tmp_path, monkeypatch, caplog):
    from scope_recall.runtime import auxiliary

    monkeypatch.setattr(auxiliary, "_REPORTED_CREDENTIAL_GAPS", set(), raising=False)
    monkeypatch.delenv("SCOPE_RECALL_TEST_CHAT_KEY", raising=False)
    monkeypatch.delenv("SCOPE_RECALL_TEST_EMBED_KEY", raising=False)
    for index in range(2):
        root = tmp_path / str(index)
        root.mkdir()
        runtime = _credential_runtime(root)
        try:
            runtime.drain(embed=object())
            runtime.drain(embed=object())
        finally:
            runtime.close()
    messages = [record.message for record in caplog.records if "credential_missing:" in record.message]
    assert sorted(messages) == [
        "credential_missing:SCOPE_RECALL_TEST_CHAT_KEY",
        "credential_missing:SCOPE_RECALL_TEST_EMBED_KEY",
    ]


def test_a_missing_embedding_key_keeps_the_consolidation_lane_available(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "TEST-chat-value")
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "")
    runtime = _credential_runtime(tmp_path)
    try:
        runtime.drain(embed=object())
        assert [row[0] for row in _leases(runtime) if row[1] == 0] == ["embed"]
        assert runtime.background_gaps == ("credential_missing:SCOPE_RECALL_TEST_EMBED_KEY",)
    finally:
        runtime.close()


def test_the_supervisor_waits_a_minute_for_missing_projection_credentials(tmp_path):
    import json

    core, cfg, path = fixture(tmp_path, supervisor_seconds=180)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["auxiliary"] = {
        "external_embedding": False,
        "external_consolidation": True,
        "ledger_path": str(tmp_path / "TEST-budget.sqlite3"),
        "consolidation": {
            "model": "deepseek-v4-flash",
            "endpoint": "https://example.test/v1/chat/completions",
            "credential_env": "SCOPE_RECALL_TEST_CHAT_KEY",
            "output_limit_field": "max_tokens",
            "max_output_tokens": 512,
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    queue(core, cfg, kind="rebuild_projection")
    elapsed = [0.0]
    calls = []

    def sleep(seconds):
        elapsed[0] += seconds

    def drain(_remaining):
        calls.append(elapsed[0])
        return 0, {
            "completed": 0,
            "unavailable_work_types": ["rebuild_projection"],
            "capability_gaps": ["credential_missing:SCOPE_RECALL_TEST_CHAT_KEY"],
        }

    supervise(path, drain, clock=lambda: elapsed[0], sleep=sleep, utc_now=lambda: NOW + timedelta(seconds=elapsed[0]))
    assert calls[:2] == [0, 60]
