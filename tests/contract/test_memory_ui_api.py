"""Web translation preserves owner checks without opening sockets in the contract tier."""

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance import ui_api
from scope_recall.maintenance.ui_api import UIApi
from scope_recall.maintenance.ui_http import ThreadingHTTPServer, UIServer
from test_claims import app, capture, initial


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
