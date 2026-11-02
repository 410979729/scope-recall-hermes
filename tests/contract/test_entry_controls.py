"""Entry controls apply to the next capture and recall through a bound context."""

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.capture_inbox import durable_record_event, enqueue
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.contract.test_claims import app, capture
from tests.v11_support import recall_request, source_event


def test_entry_state_lifecycle(app):
    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    capture(core, context, "TEST observatory palette is amber.")
    query = recall_request(query="TEST observatory palette")
    assert core.recall_packet(context, query)["items"]

    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    refused = core.record_event(
        context,
        source_event(source_event_key="TEST-readonly", content="TEST new read only text"),
        scope_id="TEST-scope",
    )
    assert refused.durability == "not_persisted"
    assert "entry_state:read-only" in refused.gaps
    assert core.recall_packet(context, query)["items"]

    owner.set_entry_state("local", "paused", actor="TEST-owner")
    refused = core.record_event(
        context, source_event(source_event_key="TEST-paused", content="TEST new paused text"), scope_id="TEST-scope"
    )
    assert refused.durability == "not_persisted"
    assert "entry_state:paused" in refused.gaps
    for mode in ("auto", "current"):
        packet = core.recall_packet(context, recall_request(query="TEST observatory palette", mode=mode))
        assert not packet["items"]
        assert "entry_state:paused" in packet["gaps"]

    owner.set_entry_state("local", "normal", actor="TEST-owner")
    assert core.recall_packet(context, query)["items"]
    resumed = core.record_event(context, source_event(source_event_key="TEST-resumed"), scope_id="TEST-scope")
    assert resumed.durability == "persisted"
    history = owner.entry("local")["history"]
    assert [item["to_state"] for item in history] == ["normal", "paused", "read-only"]
    assert all(item["actor"] == "TEST-owner" and item["changed_at"] for item in history)


def test_state_change_requires_management_authority(app):
    core, context = app
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        MemoryManagement(core, context).set_entry_state("local", "paused", actor="TEST-entry")


def test_host_ingress_refuses_before_storing_content(app):
    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    result = durable_record_event(
        core.storage,
        core.clock,
        context,
        source_event(content="TEST forbidden ingress"),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    assert result.durability == "not_persisted"
    assert "entry_state:read-only" in result.gaps
    with core.storage.read(context) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_queued_capture_is_discarded_after_state_change(app):
    core, context = app
    token, prepared = enqueue(
        core.storage,
        core.clock,
        context,
        source_event(content="TEST previously queued"),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    from scope_recall.core.capture import record_event

    result = record_event(
        core.storage,
        core.clock,
        context,
        {},
        scope_id="TEST-scope",
        remaining_seconds=10,
        _prepared=prepared,
        _inbox_token=token,
    )
    assert result.durability == "not_persisted"
    assert "entry_state:paused" in result.gaps
    with core.storage.read(context) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        assert tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_render_checks_entry_state_and_version(app):
    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    capture(core, context, "TEST harbor palette is amber.")
    packet = core.recall_packet(context, recall_request(query="TEST harbor palette"))
    assert core.prepare_recall_render(context, packet).context is not None
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    assert core.prepare_recall_render(context, packet).context is None
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    assert core.prepare_recall_render(context, packet).context is None
    released = core.release_recall_packet(context, packet)
    assert not released["items"] and "entry_state:paused" in released["gaps"]
    fresh = core.recall_packet(context, recall_request(query="TEST harbor palette"))
    assert core.prepare_recall_render(context, fresh).context is not None


def test_paused_entry_public_content_reads_are_empty(app):
    from tests.contract.test_claims import initial

    core, context = app
    claim, source = initial(core, context)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    assert core.source_by_event_key(context, source.event["source_event_key"]) is not None
    assert core.current_claim(context, claim.ref) is not None
    assert core.claim_history(context, claim.ref)
    assert core.episodes(context)
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    assert core.source_by_event_key(context, source.event["source_event_key"]) is None
    assert core.current_claim(context, claim.ref) is None
    assert not core.claim_history(context, claim.ref)
    assert not core.episodes(context)


def test_history_records_process_account_separately_from_actor(app, monkeypatch):
    import os

    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    monkeypatch.setenv("LOGNAME", "TEST-forged-account")
    monkeypatch.setenv("USERNAME", "TEST-forged-account")
    owner.set_entry_state("local", "paused", actor="TEST-label")
    history = owner.entry("local")["history"][0]
    assert history["actor"] == "TEST-label"
    assert history["account_id"].startswith("S-1-") if os.name == "nt" else history["account_id"] == str(os.getuid())


def test_paused_entry_cannot_read_a_source_directly(app):
    core, context = app
    source = capture(core, context, "TEST telescope case is green.")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    assert core.source(context, source.ref, source.revision) is None


def test_paused_entry_read_views_are_empty_and_explain_why(app):
    from scope_recall.core.retrieval import CollectionQuery

    core, context = app
    source = capture(core, context, "TEST telescope case is green.")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    request = {"protocol_version": "1.1", "request_id": "TEST-view", "subject": "TEST-project"}
    profile = core.profile(context, request)
    entity = core.entity(context, dict(request, action="probe"))
    trace = core.trace(context, request)
    collection = core.collection(context, CollectionQuery(object_kind="event"))
    inspected = core.inspect_object(context, source.ref)
    assert all("entry_state:paused" in view["gaps"] for view in (profile, entity, trace))
    assert not any(profile["sections"].values()) and not entity["statements"] and not trace["paths"]
    assert not collection.items and "entry_state:paused" in collection.gaps
    assert inspected.value is None and "entry_state:paused" in inspected.gaps


def test_local_entry_activity_includes_its_captures(app):
    core, context = app
    capture(core, context, "TEST local activity lantern.")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    details = owner.entry("local")
    assert details["last_activity"]
    assert any("lantern" in str(item) for item in details["recent_writes"])


def test_doctor_reports_entry_state(app):
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import check_storage

    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    report = DoctorReport(host="codex", status="ok")
    assert check_storage(report, context.binding, context.binding.data_directory)
    assert report.entries == [{"id": "local", "state": "read-only", "version": 1}]


def test_entry_activity_includes_its_recall_records(app):
    core, context = app
    capture(core, context, "TEST coral lantern evidence.")
    core.recall_packet(context, recall_request(request_id="TEST-entry-history", query="TEST coral lantern"))
    owner = MemoryManagement(core, InstallationMaintenance(context))
    detail = owner.entry("local")
    assert any(row["request_id"] == "TEST-entry-history" for row in detail["recent_reads"])
    assert detail["last_activity"]


def test_entry_recent_writes_include_originals_and_facts(app):
    from tests.contract.test_claims import initial

    core, context = app
    initial(core, context)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    items = owner.entry("local")["recent_writes"]
    assert {"event", "claim"} <= {item["kind"] for item in items}
    assert len(items) <= 20


def test_render_cache_separates_entries_with_equal_request_ids():
    from scope_recall.core.recall_packet import RecallPacketRenderer

    from tests.v11_support import recall_item, recall_packet

    renderer = RecallPacketRenderer()
    packet = recall_packet(items=[recall_item()])
    a = renderer.prepare(
        packet,
        installation_id="TEST-store",
        session_id="TEST-session",
        request_id=packet["request_id"],
        entry_id="TEST-a",
    )
    b = renderer.prepare(
        packet,
        installation_id="TEST-store",
        session_id="TEST-session",
        request_id=packet["request_id"],
        entry_id="TEST-b",
    )
    assert a.context is not None and b.context is not None


def test_entry_state_fences_session_brief_and_delivery(app):
    from scope_recall.core.session_brief import rebuild_scope

    from tests.contract.test_claims import initial

    core, context = app
    item, _ = initial(core, context, when=core.clock.now)
    with core.storage.write(context) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.now)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "read-only", actor="TEST-owner")
    old = core.session_brief(context, deliver=True, defer_confirmation=True)
    assert item.ref in old["text"]
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    paused = core.session_brief(context, deliver=True)
    assert not paused["text"] and not paused["items"]
    assert paused["delivery_reason"] == "entry_state:paused"
    assert core.confirm_session_brief(context, old) == "entry_state:paused"
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    assert core.confirm_session_brief(context, old) == "entry_state:paused"
    assert item.ref in core.session_brief(context, deliver=True)["text"]


def test_entry_history_stores_actor_and_account_in_separate_columns(app):
    core, context = app
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.set_entry_state("local", "paused", actor="TEST-label")
    with core.storage.read(context) as tx:
        row = tx._check().execute("SELECT actor,account_id FROM entry_control_history").fetchone()
    assert row[0] == "TEST-label" and row[1]


def test_brief_confirmation_rechecks_entry_in_write_transaction(app, monkeypatch):
    from scope_recall.core.session_brief import rebuild_scope

    from tests.contract.test_claims import initial

    core, context = app
    item, _ = initial(core, context, when=core.clock.now)
    with core.storage.write(context) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.now)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    brief = core.session_brief(context, deliver=True, defer_confirmation=True)
    assert item.ref in brief["text"]
    original = core.storage.write
    changed = False

    def race(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            owner.set_entry_state("local", "paused", actor="TEST-owner")
            owner.set_entry_state("local", "normal", actor="TEST-owner")
        return original(*args, **kwargs)

    monkeypatch.setattr(core.storage, "write", race)
    assert core.confirm_session_brief(context, brief) == "entry_state:paused"
    with core.storage.read(context) as tx:
        assert tx._check().execute("SELECT count(*) FROM session_brief_injections").fetchone()[0] == 0


def test_prepared_brief_cannot_replace_its_entry_fence(app):
    from scope_recall.core.session_brief import rebuild_scope

    from tests.contract.test_claims import initial

    core, context = app
    item, _ = initial(core, context, when=core.clock.now)
    with core.storage.write(context) as tx:
        rebuild_scope(tx, "TEST-scope", core.clock.now)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    brief = core.session_brief(context, deliver=True, defer_confirmation=True)
    assert item.ref in brief["text"]
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    brief["_entry_fence"] = core._entry_fence(context)
    assert core.confirm_session_brief(context, brief) == "entry_state:paused"
