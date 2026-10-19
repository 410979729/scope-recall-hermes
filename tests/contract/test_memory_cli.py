"""Every management command uses the same tested owner API and JSON envelope."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import validate
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance import cli, memory
from test_claims import app, initial
from test_restore import backup, run, store
from v11_support import downgrade_store


@pytest.fixture
def command(app, monkeypatch, capsys):
    core, context = app
    item, source = initial(core, context)
    api = MemoryManagement(core, InstallationMaintenance(context))
    monkeypatch.setattr(memory, "open_management", lambda args: api)

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
    "action", ("list", "search", "show", "history", "revise", "mute", "unmute", "forget", "forget-session")
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


def test_previous_schema_upgrades_without_changing_sources(store):
    source = store.say("NameOne uses green.")
    downgrade_store(store.core.storage.path, 1110)
    assert store.core.initialize().schema_version == 1111
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
