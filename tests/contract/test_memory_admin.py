"""Local owner management preserves the normal content and version boundaries."""

from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from test_candidate_lifecycle import Evaluator, _candidate, _candidate_rows, _finish_source_work
from test_claims import app, capture, initial
from test_deletion import sqlite_backup


def management(core, context):
    return MemoryManagement(core, InstallationMaintenance(context))


def test_owner_can_browse_source_and_fact_in_project(app):
    core, context = app
    item, source = initial(core, context)
    owner = management(core, replace(context, project_id=None, branch_id=None))
    page = owner.list(MemoryFilter(query="蓝色"))
    assert {item.ref, source.ref} <= {row["ref"] for row in page["items"]}
    detail = owner.show(item.ref)
    assert detail["value"]["payload"]["value_text"] == "蓝色"
    assert owner.history(item.ref)["items"][0]["revision"] == 1


def test_filters_and_stale_cursor(app):
    core, context = app
    _item, source = initial(core, context)
    owner = management(core, context)
    page = owner.list(MemoryFilter(kind="event", agent="TEST-agent", session="TEST-session"))
    assert page["items"][0]["ref"] == source.ref
    assert owner.list(MemoryFilter(agent="agent2"))["items"] == []
    assert owner.list(MemoryFilter(since="2026-09-02T00:00:00Z"))["items"] == []
    page = owner.list(limit=1)
    assert page["next_cursor"]
    capture(core, context, "NameOne uses green.")
    with pytest.raises(ContractError) as error:
        owner.list(limit=1, cursor=page["next_cursor"])
    assert error.value.code == "VERSION_CONFLICT"


def test_preview_then_delete_matches_closure_and_records_owner(app):
    core, context = app
    item, source = initial(core, context)
    owner = management(core, context)
    preview = owner.preview([item.ref], expected={item.ref: 1})
    assert {item.ref, source.ref} <= {row["ref"] for row in preview["affected"]}
    assert owner.show(source.ref)["value"]["event"]["content"]
    result = owner.apply(preview)
    assert sum(r["affected_objects"] for r in result["receipts"]) == len(preview["affected"])
    assert all(r["read_blocked"] for r in result["receipts"])
    assert owner.list()["items"] == []
    assert core.search_sources(context, "蓝色") == ()
    with core.storage.read(context) as tx:
        row = tx._check().execute("SELECT * FROM management_actions").fetchone()
        assert row["actor"] == "local_user_management"
        assert "蓝色" not in row["targets_json"]


def test_changed_inventory_refuses_deletion_and_keeps_everything(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    preview = owner.preview([item.ref])
    capture(core, context, "NameOne uses green.")
    with pytest.raises(ContractError) as error:
        owner.apply(preview)
    assert error.value.code == "VERSION_CONFLICT"
    assert owner.show(item.ref)["revision"] == 1


def test_session_preview_does_not_select_another_session(app):
    core, context = app
    _item, source = initial(core, context)
    other = capture(core, replace(context, session_id="TEST-other"), "NameOne uses green.")
    owner = management(core, context)
    preview = owner.forget_session(MemoryFilter(session=context.session_id))
    assert source.ref in {t["ref"] for t in preview["targets"]}
    assert other.ref not in {t["ref"] for t in preview["affected"]}


def test_ordinary_context_is_not_management_authority(app):
    core, context = app
    with pytest.raises(ContractError) as error:
        MemoryManagement(core, context)
    assert error.value.code == "ACCESS_DENIED"


def test_owner_revision_keeps_history_and_truthful_origin(app):
    core, context = app
    item, original = initial(core, context)
    owner = management(core, context)
    receipt = owner.revise(item.ref, expected_revision=1, value="绿色")
    assert receipt["item"]["revision"] == 2
    assert core.current_claim(context, item.ref).payload["value_text"] == "绿色"
    assert owner.history(item.ref)["items"][1]["value"]["payload"]["value_text"] == "蓝色"
    assert core.source(context, original.ref, 1).event["content"] == original.event["content"]
    source = core.source(context, receipt["source_ref"], 1)
    assert source.event["origin"] == "host_generated"
    assert source.event["source_context"]["platform"] == "local_management"
    with pytest.raises(ContractError) as error:
        owner.revise(item.ref, expected_revision=1, value="白色")
    assert error.value.code == "VERSION_CONFLICT"
    with pytest.raises(ContractError) as error:
        owner.revise(original.ref, expected_revision=1, value="白色")
    assert error.value.field == "source_text_immutable"


def test_manual_revision_survives_text_requalification(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.revise(item.ref, expected_revision=1, value="不在线")
    core.operations.requalify_claims(context, dry_run=False)
    assert core.current_claim(context, item.ref).payload["value_text"] == "不在线"


def test_mute_unmute_then_mute_again(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    assert core.search_sources(context, "蓝色", automatic=True) == ()
    assert owner.show(item.ref)["value"]["payload"]["value_text"] == "蓝色"
    owner.apply(owner.preview([item.ref], mode="unmute"))
    assert core.search_sources(context, "蓝色", automatic=True)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    assert core.search_sources(context, "蓝色", automatic=True) == ()


def test_unmute_cannot_revive_deleted_content(app):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref]))
    with pytest.raises(ContractError) as error:
        owner.apply(owner.preview([item.ref], mode="unmute"))
    assert error.value.code == "ACCESS_DENIED"
    assert core.search_sources(context, "蓝色") == ()


def restore_snapshot(core, context, snapshot):
    """Restore a synthetic snapshot against the independent latest ledger."""
    from scope_recall.core.restore import begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger

    authority = InstallationMaintenance(context)
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(snapshot, core.storage.path)
    replay_deletion_ledger(core.storage, authority, ledger)


def test_owner_can_retract_a_fact_without_rewriting_its_source(app):
    core, context = app
    item, original = initial(core, context)
    owner = management(core, context)
    receipt = owner.revise(item.ref, expected_revision=1, value=None)
    assert receipt["item"]["revision"] == 2
    assert owner.show(item.ref)["value"]["state"] == "retracted"
    assert core.source(context, original.ref, 1).event == original.event
    assert "蓝色" in core.source(context, receipt["source_ref"], 1).event["content"]


def test_restore_keeps_suppression_inherited_by_a_later_source(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    fresh = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", when="2026-09-06T12:00:00Z")
    assert fresh.suppressed
    snapshot = tmp_path / "TEST-inherited-suppression.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    restore_snapshot(core, context, snapshot)
    assert core.source(context, fresh.ref, 1).suppressed
    assert core.search_sources(context, "蓝色", automatic=True) == ()


@pytest.mark.parametrize("restore", [False, True])
def test_unmute_resumes_unstarted_evaluation_including_after_restore(app, tmp_path, restore):
    core, context = app
    item, _source, _proposal, registered = _candidate(core, context)
    assert registered.work_queued
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    snapshot = tmp_path / "TEST-muted-candidate.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    owner.apply(owner.preview([item.ref], mode="unmute"))
    if restore:
        restore_snapshot(core, context, snapshot)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert lifecycle[0]["processing_state"] == "pending_evaluation"
    assert evaluations[0]["state"] == "queued"
    assert work[0]["state"] == "pending"
    assert len(evaluations) == len(work) == 1
    _finish_source_work(core)
    core.clock.now = work[0]["available_at"]
    evaluator = Evaluator()
    core.drain_worker(context, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1


def test_unmute_does_not_repeat_an_already_attempted_evaluation(app):
    core, context = app
    item, _source, _proposal, _registered = _candidate(core, context)
    with core.storage.write(context) as tx:
        tx._check(write=True).execute("UPDATE candidate_evaluations SET model_attempted_at=?", (core.clock.utc_now(),))
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    owner.apply(owner.preview([item.ref], mode="unmute"))
    lifecycle, evaluations, work = _candidate_rows(core)
    assert lifecycle[0]["processing_state"] == "waiting_evidence"
    assert evaluations[0]["state"] == work[0]["state"] == "obsolete"


def test_restore_keeps_a_missing_source_suppressed_when_it_is_replayed(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    snapshot = tmp_path / "TEST-before-inherited-source.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    fresh = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", key="TEST-late-restatement")
    assert fresh.suppressed
    owner.apply(owner.preview([item.ref], mode="unmute"))
    restore_snapshot(core, context, snapshot)
    core.record_event(context, fresh.event, scope_id=fresh.scope_id, remaining_seconds=10)
    assert core.source(context, fresh.ref, 1).suppressed


def test_restore_keeps_each_source_versions_actual_suppression(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    owner = management(core, context)
    owner.apply(owner.preview([item.ref], mode="suppress"))
    first = capture(core, context, "我现在明确讨论TEST-project 配色 蓝色。", key="TEST-revised-source")
    event = dict(first.event, content="NameOne uses green.", source_revision=2)
    core.record_event(context, event, scope_id=first.scope_id, remaining_seconds=10)
    assert not core.source(context, first.ref, 2).suppressed
    snapshot = tmp_path / "TEST-per-version-suppression.sqlite3"
    sqlite_backup(core.storage.path, snapshot)
    restore_snapshot(core, context, snapshot)
    assert core.source(context, first.ref, 1).suppressed
    assert not core.source(context, first.ref, 2).suppressed
    owner.apply(owner.preview([first.ref], mode="unmute"))
    assert not core.source(context, first.ref, 1).suppressed


def test_transaction_view_cannot_gain_write_scopes_or_outlive_its_owner(app):
    core, context = app
    with core.storage.read(context) as tx:
        view = tx.with_context(context)
        with pytest.raises(ContractError, match="read_only"):
            tx.with_context(context, writable=True)
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            tx.with_context(replace(context, allowed_scope_ids=frozenset({"TEST-other-scope"})))
    with pytest.raises(ContractError, match="transaction_closed"):
        view.status()
