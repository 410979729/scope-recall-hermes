"""Cross-component regressions using the installed runtime boundary and durable work."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from scope_recall.core.claims import Qualification
from scope_recall.runtime.instance import RuntimeInstance, RuntimeInstanceConfig
from test_r1_candidate_lifecycle import Evaluator, _candidate, _finish_source_work, _candidate_rows
from test_v11_claims import app as app, capture, draft, accept


def test_runtime_drain_preserves_candidate_capability_and_call_deadline(app):
    core, ctx = app
    saved, _, proposal, _ = _candidate(core, ctx)
    _finish_source_work(core)

    class BoundedEvaluator(Evaluator):
        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            assert 0 < remaining_seconds <= 3.0
            return super().evaluate_candidate(candidate, sources, remaining_seconds=remaining_seconds)

    evaluator = BoundedEvaluator(proposal)
    config = RuntimeInstanceConfig(
        binding=ctx.binding, session_id=ctx.session_id,
        allowed_scope_ids=ctx.allowed_scope_ids,
        project_id=ctx.project_id, branch_id=ctx.branch_id,
        request_seconds=3.0,
    )
    runtime = RuntimeInstance(config=config, core=core, auxiliary=None)
    result = runtime.drain(consolidation=evaluator, max_items=8, remaining_seconds=10)
    assert evaluator.calls == 1, result
    assert core.current_claim(ctx, saved.ref).state == "active"


def test_overflow_evidence_is_resumed_after_reopen_without_new_user_input(app):
    core, ctx = app
    sources = [
        capture(core, ctx, f"entity{i} property{i} sharedtoken value{i}。", key=f"TEST-integration/source/{i}")
        for i in range(20)
    ]
    with core.storage.write(ctx) as tx:
        for index, source in enumerate(sources):
            proposal = draft(source, f"sharedtoken value{index}", subject=f"entity{index}", predicate=f"property{index}")
            saved = tx.claims.append(
                "TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                recorded_at=core.clock.utc_now(),
            )
            tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    trigger = capture(core, ctx, "sharedtoken 提供了统一的新证据。", key="TEST-integration/trigger")
    _finish_source_work(core)
    core.initialize()
    evaluator = Evaluator()
    for _ in range(8):
        core.drain_worker(ctx, max_items=32, remaining_seconds=10, consolidation=evaluator)
    with sqlite3.connect(core.storage.path) as db:
        matched = db.execute(
            "SELECT count(*) FROM candidate_evidence WHERE source_ref=? AND source_revision=1", (trigger.ref,)
        ).fetchone()[0]
    assert matched == 20, f"Only {matched}/20 candidates received the already-persisted new evidence"


def test_visible_other_partition_candidates_cannot_starve_matching_source(app):
    core, ctx = app
    global_ctx = replace(ctx, project_id=None, branch_id=None)
    global_source = capture(core, global_ctx, "global sharedtoken。")
    with core.storage.write(global_ctx) as tx:
        for index in range(20):
            proposal = draft(global_source, "sharedtoken", subject=f"global{index}", predicate="property")
            saved = tx.claims.append(
                "TEST-scope", proposal, Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                recorded_at=core.clock.utc_now(),
            )
            tx.candidates.register(saved.ref, saved.revision, observed_at="2026-09-01T12:00:00Z")
    local_source = capture(core, ctx, "local property sharedtoken。")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope", draft(local_source, "sharedtoken", subject="local", predicate="property"),
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"), recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    trigger = capture(core, ctx, "sharedtoken 后续证据。")
    with core.storage.read(ctx) as tx:
        rows = tx._check().execute(
            "SELECT candidate_ref FROM candidate_evidence WHERE source_ref=?", (trigger.ref,),
        ).fetchall()
        assert [row[0] for row in rows] == [saved.ref]
        assert tx.candidates.pending_source_pages() == 0


@pytest.mark.parametrize(
    "text,value,expected",
    [
        ("TEST-project 状态由草案更正为定稿。", "定稿", "active"),
        ("TEST-project 状态从草案调整为定稿。", "定稿", "active"),
        ("TEST-project 状态由草案更正为定稿。", "草案", "proposed"),
        ("TEST-project 状态尚未由草案更正为定稿。", "定稿", "proposed"),
        ("TEST-project 状态可能由草案更正为定稿。", "定稿", "proposed"),
        ("TEST-project 状态由草案更正为定稿了吗？", "定稿", "proposed"),
        ("TEST-project 状态为草案。另一个项目更正为定稿。", "定稿", "proposed"),
    ],
)
def test_explicit_transition_preserves_new_value_and_rejects_unproven_frames(app, text, value, expected):
    core, ctx = app
    source = capture(core, ctx, text)
    proposal = draft(source, value, kind="fact", statement_kind="assertion", subject="TEST-project", predicate="状态")
    receipt = accept(core, ctx, proposal)
    assert receipt.items[0].state == expected


@pytest.mark.parametrize("always_rejected", [False, True])
def test_explicit_http_rejection_recovers_without_new_evidence_and_stops_at_limit(app, always_rejected):
    from scope_recall.adapters.models import AuxiliaryModelError

    core, ctx = app
    saved, _, proposal, _ = _candidate(core, ctx)
    _finish_source_work(core)

    class Recovering(Evaluator):
        attempts = 0

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            self.attempts += 1
            if always_rejected or self.attempts == 1:
                raise AuxiliaryModelError("http_status", detail="503")
            return super().evaluate_candidate(candidate, sources, remaining_seconds=remaining_seconds)

    evaluator = Recovering(proposal)
    first = core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert first.retried == 1 and first.failed == 0
    # Simulate passage of time and reopening, without adding evidence.
    for _ in range(4):
        core.clock.now = (datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        core.initialize()
        core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    _, evaluations, work = _candidate_rows(core)
    assert evaluator.attempts == (3 if always_rejected else 2)
    assert work[0]["state"] == ("failed" if always_rejected else "done")
    assert evaluations[0]["state"] == ("failed" if always_rejected else "resolved")
    if not always_rejected:
        assert core.current_claim(ctx, saved.ref).state == "active"
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT count(*) FROM work_error_details WHERE error_code='http_503'").fetchone()[0] == (3 if always_rejected else 1)
