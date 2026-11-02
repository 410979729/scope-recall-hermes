"""Recall history preserves summary, adoption and work-bounded safety decisions."""

from dataclasses import replace
from datetime import UTC

import pytest

from tests.contract.test_claims import app, capture
from tests.contract.test_importance_summaries import build
from tests.v11_support import recall_request


def latest(core, request_id="TEST-request"):
    """Read the synthetic writer receipt without hiding rejected identities."""
    assert core.recall_diagnostics.flush()
    rows = core.recall_diagnostics.store.list(request_id=request_id, limit=1)
    assert rows
    return rows[0]


@pytest.mark.parametrize("kind", ["session", "task"])
@pytest.mark.parametrize("entry", ["recall", "recall_packet"])
def test_summary_log_preserves_kind_and_retrieval_channel(app, kind, entry):
    core, ctx = app
    actor = replace(ctx, task_anchor="TEST-summary-task") if kind == "task" else ctx
    capture(core, actor, "Implement TEST-project and run its checks.")
    if kind == "task":
        capture(core, actor, "The task is completed.")
    build(core, actor)
    with core.storage.read(ctx) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT p.claim_ref,p.claim_revision FROM summary_parts p JOIN summary_groups g "
                "ON g.group_key=p.group_key WHERE g.kind=? LIMIT 1",
                (kind,),
            )
            .fetchone()
        )
        assert row is not None
    getattr(core, entry)(
        replace(ctx, session_id="TEST-summary-reader"),
        recall_request(query="TEST-project", mode="current", focus_refs=[f"{row[0]}@{row[1]}"]),
        deadline_seconds=10,
    )
    item = next(i for i in latest(core)["items"] if i["ref"] == row[0] and i["revision"] == row[1])
    assert item["reason"] == ("delivered" if entry == "recall" else "summary_covered")
    assert item["source_type"] == kind + "_summary"
    assert "exact_ref" in item["channels"]


def test_log_explains_summary_omitted_after_original_fits(app):
    core, ctx = app
    source = capture(core, ctx, "If approved, deploy TEST-project.", when="2026-09-05T10:00:00Z")
    build(core, ctx)
    packet = core.recall_packet(
        replace(ctx, session_id="TEST-summary-reader"),
        recall_request(query="昨天做了什么"),
        deadline_seconds=10,
        zone=UTC,
    )
    assert any(i["ref"] == source.ref for i in packet["items"])
    summaries = [i for i in latest(core)["items"] if i.get("source_type") == "session_summary"]
    assert summaries and all(i["reason"] == "summary_covered" for i in summaries)
    assert all(i["rank"] is None and i["source_type"] == "session_summary" for i in summaries)


@pytest.mark.parametrize("entry", ["recall", "recall_packet"])
def test_adopted_decision_log_retains_type_and_not_proposal_text(app, entry):
    import json

    from tests.contract.test_adoption_merge import adopted

    core, ctx = app
    plan, _owner, claim = adopted(core, ctx)
    getattr(core, entry)(
        ctx,
        recall_request(query="配置重启策略", mode="current", focus_refs=[f"{claim.ref}@{claim.revision}"]),
        deadline_seconds=10,
    )
    record = latest(core)
    item = next(i for i in record["items"] if i["ref"] == claim.ref)
    assert item["reason"] == "delivered" and item["source_type"] == "adopted_decision"
    assert "exact_ref" in item["channels"]
    assert plan.event["content"] not in json.dumps(record, ensure_ascii=False)


def test_work_allowance_pending_review_has_its_own_log_reason(app, monkeypatch):
    from scope_recall.core import injection_guard

    core, ctx = app
    monkeypatch.setattr(injection_guard, "SCAN_WINDOWS", 0)
    from tests.v11_support import source_event

    receipt = core.record_event(
        ctx, source_event(content="TEST observatory compass material is copper."), scope_id="TEST-scope"
    )
    ref = receipt.event_refs[0].ref
    with core.storage.read(ctx) as tx:
        assert (
            tx._check().execute("SELECT state FROM source_safety WHERE source_ref=?", (ref,)).fetchone()[0]
            == "pending_review"
        )
    packet = core.recall_packet(ctx, recall_request(query="TEST observatory compass", focus_refs=[f"{ref}@1"]))
    assert not packet["items"]
    record = latest(core)
    assert "pending_review" in record["reasons"]
    assert any(i["ref"] == ref and i["reason"] == "pending_review" for i in record["items"])


def test_overflow_log_rows_keep_original_identity_and_channel(app):
    from tests.contract.test_recall_overflow import sources

    core, ctx = app
    captured = sources(app)
    packet = core.recall_packet(ctx, recall_request(query="Orchard deployment port"), deadline_seconds=10)
    rows = [i for i in packet["items"] if i.get("qualification_reason") == "Other-session matches"]
    assert rows
    record = latest(core)
    overflow_refs = {ref for i in rows for ref in i["evidence_refs"]}
    items = [i for i in record["items"] if i["reason"] == "overflow"]
    assert {f"{i['ref']}@{i['revision']}" for i in items} == overflow_refs
    assert all(i["source_type"] == "event" and i["channels"] for i in items)
    assert {i["ref"] for i in record["items"] if i["reason"] in {"delivered", "overflow"}} >= {s.ref for s in captured}


@pytest.mark.parametrize("state,reason", [("pending_review", "pending_review"), ("quarantined", "quarantine")])
def test_adopted_decision_logs_the_hold_of_its_assistant_dependency(app, state, reason):
    from tests.contract.test_adoption_merge import adopted

    core, ctx = app
    plan, _owner, claim = adopted(core, ctx)
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute(
            "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES(?,?,?,?,?)",
            (plan.ref, plan.revision, state, "[]", core.clock.utc_now()),
        )
    packet = core.recall_packet(
        ctx,
        recall_request(query="配置重启策略", mode="current", focus_refs=[f"{claim.ref}@{claim.revision}"]),
        deadline_seconds=10,
    )
    record = latest(core)
    assert all(i["ref"] != claim.ref for i in packet["items"])
    assert any(i["ref"] == claim.ref and i["reason"] == reason for i in record["items"])


def test_unshippable_original_trial_cannot_claim_a_summary_was_covered(app):
    from scope_recall.core.recall_trace import CURRENT_TRACE, RecallTrace

    from tests.contract.test_recall_packet_compiler import _compiled, _said
    from tests.contract.test_summary_repairs import _quoted_summary

    core, ctx = app
    summary = _quoted_summary("TEST-retained-summary", "TEST-project approved.")
    original = _said("TEST-unshippable-original", "TEST-project approved." + "x" * 13000)
    trace = RecallTrace(enabled=True)
    token = CURRENT_TRACE.set(trace)
    try:
        packet = _compiled(core, ctx, "TEST-project", [("scoped", summary), ("lexical", original)])
        assert any(i["ref"] == summary.ref for i in packet["items"])
        assert trace.candidates[("claim", summary.ref, 1)]["reason"] != "summary_covered"
    finally:
        CURRENT_TRACE.reset(token)


def test_setup_cleanup_retires_only_its_diagnostic_writers(tmp_path):
    from scope_recall.core.recall_log_storage import RecallLogStore
    from scope_recall.core.recall_log_writer import RecallLogWriter
    from scope_recall.maintenance.setup_verify import _clean_owned_check

    root = tmp_path / "TEST-owned-setup-check"
    root.mkdir()
    (root / "TEST-file").write_text("synthetic", encoding="utf-8")
    owned = RecallLogWriter(RecallLogStore(root / "recall.sqlite3"))
    other = RecallLogWriter(RecallLogStore(tmp_path / "TEST-other-log.sqlite3"))
    try:
        _clean_owned_check(root)
        assert owned.closed and not other.closed
        assert not root.exists()
    finally:
        owned.close()
        other.close()
