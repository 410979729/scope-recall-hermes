"""Every management command uses the same tested owner API and JSON envelope."""

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import validate
from scope_recall.core import management_query
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.maintenance import cli, memory
from test_claims import app, capture, initial
from test_restore import backup, run, store
from v11_support import downgrade_store


def test_secret_command_has_metadata_only_in_json_and_plain_output(app, monkeypatch, capsys):
    from test_memory_admin import legacy_secret

    core, context = app
    flagged = legacy_secret(core, context, monkeypatch)
    api = MemoryManagement(core, InstallationMaintenance(context))
    monkeypatch.setattr(memory, "open_management", lambda args, **_: api)
    for options in (["--json"], []):
        assert cli.main(["memory", "secrets", *options]) == 0
        output = capsys.readouterr().out
        assert flagged.ref in output and "Xk9mP2qa" not in output
        if options:
            result = json.loads(output)
            assert result["action"] == "secrets" and result["data"]["rows_read"] >= 1
            validate(
                result,
                json.loads(
                    (Path(__file__).resolve().parents[2] / "contracts" / "memory_management.schema.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
        else:
            assert "Read " in output and "PATTERNS" in output and "REVISION" in output


def test_secret_command_opens_an_existing_store_without_initialization(store, monkeypatch, capsys):
    from scope_recall.core import MemoryCore

    store.say("NameOne uses green.")

    def forbidden(self):
        raise AssertionError("a read-only inventory cannot initialize or migrate a store")

    monkeypatch.setattr(MemoryCore, "initialize", forbidden)
    assert cli.main(["memory", "secrets", *store.target(), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["items"] == [] and result["data"]["rows_read"] >= 1


def test_the_page_parser_opens_and_initializes_a_store(store, monkeypatch):
    """The management page's parser defines no memory action; the shared entry still binds and initializes."""
    from scope_recall.core import MemoryCore
    from scope_recall.maintenance import ui

    store.say("NameOne uses green.")
    initialized = []
    initialize = MemoryCore.initialize
    monkeypatch.setattr(MemoryCore, "initialize", lambda self: initialized.append(self) or initialize(self))
    api = memory.open_management(ui.parser().parse_args(store.target()))
    assert isinstance(api, MemoryManagement) and len(initialized) == 1


@pytest.fixture
def command(app, monkeypatch, capsys):
    core, context = app
    item, source = initial(core, context)
    api = MemoryManagement(core, InstallationMaintenance(context))
    monkeypatch.setattr(memory, "open_management", lambda args, **_: api)

    def call(action, *args):
        code = cli.main(["memory", action, *args, "--json"])
        result = json.loads(capsys.readouterr().out)
        assert result["format"] == memory.FORMAT
        assert result["action"] == action
        validate(
            result,
            json.loads(
                (Path(__file__).resolve().parents[2] / "contracts" / "memory_management.schema.json").read_text()
            ),
        )
        return code, result

    return call, item, source


@pytest.mark.parametrize(
    "action", ("list", "search", "show", "history", "revise", "mute", "unmute", "forget", "forget-session", "secrets")
)
def test_each_command_reaches_the_owner_api(command, action):
    call, item, _source = command
    arguments = {
        "list": [],
        "search": ["蓝色"],
        "show": [item.ref],
        "history": [item.ref],
        "revise": [item.ref, "--value", "绿色", "--expected-revision", "1"],
        "mute": [item.ref, "--yes"],
        "unmute": [item.ref, "--yes"],
        "forget": [item.ref, "--yes"],
        "forget-session": ["--session", "TEST-session", "--yes"],
        "secrets": [],
    }
    code, result = call(action, *arguments[action])
    assert code == 0 and result["status"] == "ok", result


def test_cli_preview_is_read_only_and_can_be_required_on_apply(command):
    call, item, _source = command
    code, result = call("forget", item.ref)
    assert code == 0 and result["data"]["applied"] is False
    preview = result["data"]["preview"]
    assert call("show", item.ref)[0] == 0
    code, result = call("forget", item.ref, "--yes", "--preview-id", preview["preview_id"])
    assert code == 0 and result["data"]["applied"] is True
    assert call("show", item.ref)[0] == 2


def test_cli_can_retract_a_fact(command):
    call, item, _source = command
    code, result = call("revise", item.ref, "--retract", "--expected-revision", "1")
    assert code == 0 and result["status"] == "ok", result
    assert call("show", item.ref)[1]["data"]["value"]["state"] == "retracted"


def test_cli_selects_real_installed_store_and_prints_a_table(store, capsys):
    source = store.say("NameOne uses green.")
    assert cli.main(["memory", "list", *store.target()]) == 0
    text = capsys.readouterr().out
    assert "KIND" in text and "REF" in text and source.ref in text
    assert cli.main(["memory", "list", *store.target(), "--agent", "agent2"]) == 0
    assert "KIND" in capsys.readouterr().out


def test_complete_search_without_match_reports_examined(command, capsys):
    assert cli.main(["memory", "search", "TEST absent needle"]) == 0
    text = capsys.readouterr().out
    assert "None of the " in text and "objects examined matched" in text
    assert "Scan complete" in text and "Continue:" not in text


def test_match_limit_stop_reports_reason_and_continuation(command, capsys):
    call, _item, _source = command
    api = memory.open_management(None)
    for i in range(2):
        capture(api.core, api.authority.context, f"TEST matching needle {i}")
    code, result = call("search", "TEST matching needle", "--limit", "1")
    assert code == 0 and result["data"]["stop_reason"] == "limit"
    assert len(result["data"]["items"]) == 1
    assert cli.main(["memory", "search", "TEST matching needle", "--limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "Scan stopped at the match limit." in text
    assert "Repeat the same command with --cursor " in text


def test_budget_stop_without_match_reports_continuation(command, capsys, monkeypatch):
    call, _item, _source = command
    api = memory.open_management(None)
    for i in range(40):
        capture(api.core, api.authority.context, f"TEST haystack {i}")
    monkeypatch.setattr(management_query, "SCAN_SECONDS", 0)
    assert cli.main(["memory", "search", "TEST absent needle", "--kind", "event"]) == 0
    text = capsys.readouterr().out
    assert "No object matched among the 16 examined" in text
    assert "time budget" in text and "more remain" in text
    assert "Repeat the same command with --cursor " in text


def test_json_reports_scan_progress_and_a_usable_cursor(command, monkeypatch):
    call, _item, _source = command
    api = memory.open_management(None)
    for i in range(40):
        capture(api.core, api.authority.context, f"TEST haystack {i}")
    monkeypatch.setattr(management_query, "SCAN_SECONDS", 0)
    code, result = call("search", "TEST absent needle", "--kind", "event")
    assert code == 0
    data = result["data"]
    assert data["examined"] == 16 and data["scan_complete"] is False
    assert data["stop_reason"] == "time_budget"
    assert set(data) == {"items", "memory_epoch", "examined", "scan_complete", "stop_reason", "next_cursor"}
    assert data["next_cursor"].replace("-", "").replace("_", "").isalnum()
    monkeypatch.setattr(management_query, "SCAN_SECONDS", 30)
    code, result = call("search", "TEST absent needle", "--kind", "event", "--cursor", data["next_cursor"])
    assert code == 0 and result["data"]["scan_complete"]
    assert result["data"]["examined"] == 25 and result["data"]["next_cursor"] is None


def test_malformed_cursor_returns_input_invalid_without_traceback(command):
    call, _item, _source = command
    value = dict(after=["0001-01-01T00:00:00+01:00", "event-TEST"], binding="a" * 64, version=1)
    invalid = ["not base64!", json.dumps(value)]
    for decoded in (
        value,
        [],
        None,
        {"after": None},
        dict(value, after=["2026-01-01T00:00:00.000000+00:00", "\ud800"]),
    ):
        invalid.append(base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("="))
    for cursor in invalid:
        code, result = call("list", "--cursor", cursor)
        assert code == 2 and result["code"] == "INPUT_INVALID" and result["field"] == "cursor"


def test_previous_schema_upgrades_without_changing_sources(store):
    source = store.say("NameOne uses green.")
    downgrade_store(store.core.storage.path, 1110)
    assert store.core.initialize().schema_version == SCHEMA_VERSION
    assert store.core.source(store.ctx, source.ref, 1).event == source.event
    with store.core.storage.read(store.ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM management_actions").fetchone()[0] == 0


def test_restore_does_not_reapply_a_released_suppression(store, capsys):
    source = store.say("NameOne uses green.")
    context = replace(store.ctx, allowed_scope_ids=store.ctx.binding.scope_ids)
    owner = MemoryManagement(store.core, InstallationMaintenance(context))
    owner.apply(owner.preview([source.ref], mode="suppress"))
    snapshot = backup(capsys, store, "TEST-muted")
    owner.apply(owner.preview([source.ref], mode="unmute"))
    assert store.core.search_sources(store.ctx, "green", automatic=True)
    code, result = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-rollback"),
        "--apply",
    )
    assert code == 0, result
    assert store.core.search_sources(store.ctx, "green", automatic=True)
    owner.apply(owner.preview([source.ref], mode="suppress"))
    assert store.core.search_sources(store.ctx, "green", automatic=True) == ()


def test_rollback_to_previous_schema_prepares_a_compatible_ledger(store, capsys):
    source = store.say("NameOne uses green.")
    snapshot = backup(capsys, store, "TEST-before-management")
    downgrade_store(snapshot, 1110)
    store.forget(source)
    code, result = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-rollback"),
        "--apply",
    )
    assert code == 0 and result["status"] == "prepared", result
    ledger = json.loads((store.data / "restore-ledger.json").read_text(encoding="utf-8"))
    assert ledger["format"] == "scope-recall-deletion-ledger/1"
    assert "suppressed_objects" not in ledger
    assert source.ref in {m["object_ref"] for op in ledger["operations"] for m in op["members"]}
