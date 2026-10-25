"""Web translation preserves owner checks without opening sockets in the contract tier."""

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance import ui_api
from scope_recall.maintenance.ui_api import UIApi
from scope_recall.maintenance.ui_http import ThreadingHTTPServer, UIServer
from test_claims import app, capture, initial
from v11_support import recall_request


@pytest.fixture
def web_api(app):
    core, context = app
    item, _source = initial(core, context)
    return UIApi(MemoryManagement(core, InstallationMaintenance(context))), item


def test_confirmation_requires_review_and_keeps_owner_receipt(web_api):
    api, item = web_api
    preview = api.preview({"action": "mute", "ref": item.ref})
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        api.confirm({"id": preview["id"]})
    receipt = api.confirm({"id": preview["id"], "confirmed": True})
    assert receipt["result"]["actor"] == "local_user_management"
    assert api.read("receipt", {"id": preview["id"]}) == receipt
    assert api.management.show(item.ref)["value"]["suppressed"]


def test_expiration_and_bounded_review_storage(web_api, monkeypatch):
    api, item = web_api
    monkeypatch.setattr(ui_api, "RETAINED_RESULTS", 1)
    first = api.preview({"action": "mute", "ref": item.ref})
    second = api.preview({"action": "mute", "ref": item.ref})
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        api.confirm({"id": first["id"], "confirmed": True})
    created = api.previews[second["id"]][0]
    monkeypatch.setattr(ui_api.time, "monotonic", lambda: created + ui_api.REVIEW_SECONDS + 1)
    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        api.confirm({"id": second["id"], "confirmed": True})


@pytest.mark.parametrize("data", [{"limit": False}, {"limit": "bad"}, {"query": []}, {"since": "bad"}, {"kind": "bad"}])
def test_bad_filter_inputs_are_not_coerced(web_api, data):
    api, _item = web_api
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        api.read("list", data)


def test_constructor_has_no_address_or_port_input(monkeypatch):
    addresses = []

    def capture_address(server, address, handler):
        addresses.append(address)
        server.server_address = (address[0], 12345)
        server.server_port = 12345

    monkeypatch.setattr(ThreadingHTTPServer, "__init__", capture_address)
    UIServer(None)
    assert addresses == [("127.0.0.1", 0)]


def test_session_deletion_ignores_the_browse_query(web_api, app):
    api, item = web_api
    core, context = app
    source = capture(core, context, "NameOne needle")
    preview = api.preview({"action": "forget-session", "session": "TEST-session", "query": "needle"})
    refs = {target["ref"] for target in preview["preview"]["plan"]["affected"]}
    assert {item.ref, source.ref} <= refs


def test_old_private_off_preview_refuses_replacement_rule(web_api):
    api, _item = web_api
    api.management.private_on(target="global")
    preview = api.preview({"action": "private-off", "target": "global"})
    api.management.private_off(target="global")
    replacement = api.management.private_on(target="global")
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        api.confirm({"id": preview["id"], "confirmed": True})
    assert api.management.private_status()["rules"] == [replacement]


@pytest.mark.parametrize("data", [{}, {"value": None}, {"retract": False}])
def test_correction_requires_value_or_explicit_withdrawal(web_api, data):
    api, item = web_api
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        api.preview(dict(action="revise", ref=item.ref, expected_revision=1, **data))
    assert api.management.show(item.ref)["revision"] == 1


def test_owner_inspection_during_full_private_mode_keeps_agent_and_deletion_guards(web_api, app):
    from dataclasses import replace

    core, context = app
    api, item = web_api

    class NoModels:
        calls = 0

        def __getattr__(self, name):
            self.calls += 1
            raise AssertionError("owner inspection must not call models or embeddings")

    core.vectors = core.consolidation = NoModels()
    core.recall_pipeline.vector_port = core.vectors
    core.recall(context, recall_request())
    calls = core.vectors.calls
    assert calls > 0
    api.management.private_on(target="global")
    assert api.read("list", {})["items"]
    assert api.read("search", {"query": "TEST"})["items"]
    assert api.management.show(item.ref)["ref"] == item.ref
    assert api.management.history(item.ref)["items"][0]["ref"] == item.ref
    assert api.preview({"action": "revise", "ref": item.ref, "value": "green", "expected_revision": 1})
    assert core.vectors.calls == calls
    with pytest.raises(ContractError, match="private_mode"):
        core.inspect_object(context, item.ref)
    with pytest.raises(ContractError):
        core.inspect_object(context, item.ref, owner_authority=object())
    api.management.authority = InstallationMaintenance(replace(context, allowed_scope_ids=frozenset()))
    with pytest.raises(ContractError):
        api.management.show(item.ref)
    api.management.authority = InstallationMaintenance(context)
    api.management.apply(api.management.preview([item.ref]))
    with pytest.raises(ContractError):
        api.management.show(item.ref)


def test_backup_status_uses_public_state_reader(web_api, monkeypatch):
    api, _item = web_api
    monkeypatch.setattr(
        ui_api.backup_upkeep, "read_backup_state", lambda directory: {"last_error": "synthetic failure"}, raising=False
    )
    monkeypatch.setattr(ui_api.backup_upkeep, "backup_job_problem", lambda directory: None)
    assert api.backup()["problem"] == "synthetic failure"
