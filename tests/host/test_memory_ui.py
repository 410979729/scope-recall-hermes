"""The local page translates real HTTP requests to the existing owner APIs."""

import json
import sqlite3
from dataclasses import replace
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance import cli, doctor_store, ui, ui_api
from scope_recall.maintenance.ui_api import UIApi
from scope_recall.maintenance.ui_http import UIServer
from test_claims import app, capture, initial

from tests.host.test_memory_ui_http import send


@pytest.fixture
def page(app):
    core, context = app
    item, source = initial(core, context)
    owner = MemoryManagement(core, InstallationMaintenance(context))
    api = UIApi(owner)
    with UIServer(api) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def call(route, data=None, post=False):
            url = "/api/" + route + ("?" + urlencode(data) if data and not post else "")
            status, _, body = send(server, url, method="POST" if post else "GET", data=data)
            return status, json.loads(body)

        call.server = server
        try:
            yield call, api, item, source
        finally:
            server.shutdown()
            thread.join(6)
            assert not thread.is_alive()


def ok(call, route, data=None, post=False):
    status, payload = call(route, data, post)
    assert status == 200, payload
    return payload["data"]


@pytest.mark.parametrize("route", ["list", "search"])
def test_inventory_filters_continuation_and_errors(page, route):
    call, api, item, source = page
    values = {"limit": 1, **({"query": "TEST"} if route == "search" else {})}
    first = ok(call, route, values)
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = ok(call, route, dict(values, cursor=first["next_cursor"]))
    assert second["items"][0]["ref"] != first["items"][0]["ref"]
    for data in ({"cursor": "bad"}, {"limit": 0}, {"kind": "bad"}, {"since": "bad"}):
        assert call(route, data)[0] == 400
    assert (
        ok(call, route, {"kind": "event", "session": "TEST-session", "agent": "TEST-agent"})["items"][0]["ref"]
        == source.ref
    )
    assert ok(call, route, {"agent": "agent1"})["items"] == []


def test_detail_original_words_fact_sources_and_history(page):
    call, api, item, source = page
    detail = ok(call, "detail", {"ref": item.ref})
    assert detail["value"]["payload"]["evidence_spans"][0]["source_ref"] == source.ref
    assert ok(call, "detail", {"ref": source.ref})["value"]["event"]["content"]
    assert ok(call, "history", {"ref": item.ref})["items"][0]["revision"] == 1
    for route in ("detail", "history"):
        assert call(route, {"ref": "claim-missing"})[0] == 400
        assert call(route, {"ref": item.ref, "revision": "bad", "limit": 0})[0] == 400


@pytest.mark.parametrize("action", ["revise", "mute", "unmute", "forget", "forget-session"])
def test_every_action_preview_confirmation_receipt_and_errors(page, action):
    call, api, item, source = page
    data = {"action": action, "ref": item.ref, "expected_revision": 1, "value": "NameOne uses green."}
    if action == "forget-session":
        data["session"] = "TEST-session"
    if action == "unmute":
        api.management.apply(api.management.preview([item.ref], mode="suppress"))
    plan = ok(call, "preview", data, True)
    assert api.management.show(item.ref)["revision"] == 1
    assert call("confirm", {"id": plan["id"]}, True)[0] == 400
    receipt = ok(call, "confirm", {"id": plan["id"], "confirmed": True}, True)
    assert receipt["action"] == action and receipt["result"]
    assert ok(call, "receipt", {"id": plan["id"]}) == receipt
    assert call("confirm", {"id": plan["id"], "confirmed": True}, True)[0] == 400
    assert call("receipt", {"id": "missing"})[0] == 400
    bad = {"action": action, "ref": "claim-missing"}
    assert call("preview", bad, True)[0] == 400
    if action in {"forget", "forget-session"}:
        with pytest.raises(ContractError):
            api.management.show(item.ref)
    elif action == "revise":
        assert api.management.show(item.ref)["revision"] == 2
    else:
        assert bool(api.management.show(item.ref)["value"]["suppressed"]) == (action == "mute")


def test_stale_selection_and_substituted_confirmation_are_refused(page):
    call, api, item, source = page
    plan = ok(call, "preview", {"action": "forget", "ref": item.ref}, True)
    api.management.revise(item.ref, expected_revision=1, value="NameOne uses green.")
    assert call("confirm", {"id": plan["id"], "confirmed": True}, True)[0] == 400
    plan = ok(call, "preview", {"action": "mute", "ref": item.ref}, True)
    result = ok(call, "confirm", {"id": plan["id"], "confirmed": True, "action": "forget", "ref": source.ref}, True)
    assert result["action"] == "mute"
    assert api.management.show(source.ref)


def test_correction_stale_revision_retraction_and_raw_source_error(page):
    call, api, item, source = page
    for ref, revision in ((source.ref, 1), (item.ref, 99)):
        plan = ok(
            call,
            "preview",
            {"action": "revise", "ref": ref, "expected_revision": revision, "value": "NameOne uses green."},
            True,
        )
        assert call("confirm", {"id": plan["id"], "confirmed": True}, True)[0] == 400
    plan = ok(call, "preview", {"action": "revise", "ref": item.ref, "expected_revision": 1, "retract": True}, True)
    ok(call, "confirm", {"id": plan["id"], "confirmed": True}, True)
    assert api.management.show(item.ref)["value"]["state"] == "retracted"
    first = ok(call, "history", {"ref": item.ref, "limit": 1})
    assert first["next_before"] == 2
    assert ok(call, "history", {"ref": item.ref, "before": 2})["items"][0]["revision"] == 1


def test_private_switch_status_rules_and_error(page):
    call, api, item, source = page
    assert ok(call, "private")["mode"] == "none"
    for action in ("private-on", "private-off"):
        plan = ok(call, "preview", {"action": action, "target": "global"}, True)
        ok(call, "confirm", {"id": plan["id"], "confirmed": True}, True)
        status = ok(call, "private")
        assert status["mode"] == ("full" if action == "private-on" else "none")
        assert bool(status["rules"]) == (action == "private-on")
    assert call("private", {"session": ""})[0] == 400
    plan = ok(call, "preview", {"action": "private-on", "target": "bad"}, True)
    assert call("confirm", {"id": plan["id"], "confirmed": True}, True)[0] == 400


@pytest.mark.parametrize("suffix", ["directory", "archive.zip"])
def test_export_uses_existing_writer_and_refuses_overwrite(page, tmp_path, suffix):
    call, api, item, source = page
    destination = tmp_path / suffix
    for expected in (200, 400):
        plan = ok(call, "preview", {"action": "export", "out": str(destination)}, True)
        assert call("confirm", {"id": plan["id"], "confirmed": True}, True)[0] == expected
        assert destination.exists()
    assert call("preview", {"action": "export", "kind": "bad"}, True)[0] == 400


def test_overview_and_backup_reuse_owners_and_errors(page, monkeypatch):
    call, api, item, source = page
    assert ok(call, "overview")["sources"] >= 1
    assert ok(call, "overview")["store_bytes"] > 0
    assert ok(call, "backup")["latest_bundle"] is None
    assert ok(call, "backup")["checks"][0]["name"] == "deletion_mirror"
    monkeypatch.setattr(ui_api, "check_storage", lambda *args: False)
    assert call("overview")[0] == 400

    def unavailable(*args):
        raise OSError("synthetic inaccessible destination")

    monkeypatch.setattr(ui_api, "configured_backup_dir", unavailable)
    assert call("backup")[0] == 503


def test_overview_doctor_read_failure_is_retryable(page, monkeypatch):
    call, _api, _item, _source = page
    reads = []

    def unavailable(storage, context, **kwargs):
        reads.append(kwargs)
        raise sqlite3.OperationalError("synthetic unavailable store")

    monkeypatch.setattr(doctor_store.SQLiteStorage, "_transaction", unavailable)
    status, payload = call("overview")
    assert reads and reads[0]["writable"] is False
    assert (status, payload) == (503, {"status": "error", "code": "STORE_UNAVAILABLE"})


def test_overview_missing_database_keeps_missing_response(page, tmp_path):
    call, api, _item, _source = page
    api.binding = replace(api.binding, data_directory=tmp_path / "missing-store")
    assert call("overview") == (400, {"status": "error", "code": "SOURCE_MISSING", "field": "store_status"})


def test_backup_displays_owned_bundle_mirror_and_scheduler_problem(page, monkeypatch, tmp_path):
    call, api, item, source = page
    requested = []

    def bundles(directory, installation, copy):
        requested.append((directory, installation, copy))
        return [SimpleNamespace(path=tmp_path / "synthetic-backup.zip")]

    monkeypatch.setattr(ui_api, "configured_backup_dir", lambda directory: tmp_path)
    monkeypatch.setattr(ui_api, "find_owned_backup_bundles", bundles)
    monkeypatch.setattr(ui_api.backup_upkeep, "backup_job_problem", lambda directory: "synthetic scheduled problem")
    data = ok(call, "backup")
    assert data["latest_bundle"] == str(tmp_path / "synthetic-backup.zip")
    assert data["problem"] == "synthetic scheduled problem"
    assert requested == [(tmp_path, api.binding.installation_id, api.copy_id)]


def test_read_requests_and_refused_transport_leave_truth_unchanged(page):
    call, api, item, source = page
    directory = api.binding.data_directory
    before = (directory / "memory.sqlite3").read_bytes()
    assert send(call.server, token=False)[0] == 403
    assert send(call.server, headers={"Host": "foreign.invalid"})[0] == 403
    for route, data in (
        ("list", {}),
        ("detail", {"ref": item.ref}),
        ("history", {"ref": item.ref}),
        ("private", {}),
        ("overview", {}),
        ("backup", {}),
    ):
        ok(call, route, data)
    assert (directory / "memory.sqlite3").read_bytes() == before
    assert call("confirm", {"confirmed": True})[0] == 405
    assert api.management.show(item.ref)["revision"] == 1


def test_form_confirmation_uses_real_management_and_json_stays_strict(page):
    call, api, item, _source = page
    plan = ok(call, "preview", {"action": "mute", "ref": item.ref}, True)
    assert call("confirm", {"id": plan["id"], "confirmed": "true"}, True)[0] == 400
    body = urlencode({"token": call.server.session_token, "id": plan["id"], "confirmed": "true"}).encode()
    status, _, payload = send(
        call.server,
        "/api/confirm",
        method="POST",
        token=False,
        raw=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 200, payload
    assert api.management.show(item.ref)["value"]["suppressed"]


def test_cli_opens_capability_and_closes_on_interrupt(page, monkeypatch, capsys):
    call, api, item, source = page
    monkeypatch.setattr(ui, "open_management", lambda args: api.management)
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url))
    servers = []

    def interrupt(server):
        servers.append(server)
        raise KeyboardInterrupt

    monkeypatch.setattr(UIServer, "serve_forever", interrupt)
    assert cli.main(["ui", "--root", "unused"]) == 0
    assert opened[0] == servers[0].url and opened[0] in capsys.readouterr().out
    assert servers[0].socket.fileno() == -1
    with pytest.raises(SystemExit):
        ui.parser().parse_args(["--listen", "0.0.0.0"])
    monkeypatch.setattr(
        ui, "open_management", lambda args: (_ for _ in ()).throw(ContractError("SOURCE_MISSING", "database"))
    )
    assert cli.main(["ui", "--root", "unused"]) == 2
