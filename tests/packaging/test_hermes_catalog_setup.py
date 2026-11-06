"""Catalog-owned plugins use the same setup boundaries as the command-line installer."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from scope_recall.adapters.hermes.provider import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes.shared_entries import load_binding_for_home, read_shared_payload
from scope_recall.maintenance.install_common import InstallError
from scope_recall.maintenance.setup import build_plan, parser
from scope_recall.maintenance.setup_apply import _activate_hermes, apply_setup

from tests.packaging.test_setup import fake_user, snapshot  # noqa: F401


def catalog_plugin(home):
    plugin = home / "plugins" / "scope-recall"
    (plugin / ".git").mkdir(parents=True)
    (plugin / ".git" / "config").write_text("TEST-catalog-checkout", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: scope-recall\nversion: 1.0.0\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("# TEST host-owned wrapper\n", encoding="utf-8")
    (home / "plugins" / ".install-metadata.json").write_text(
        json.dumps({"scope-recall": {"source": "https://example.test/plugin", "revision": "a" * 40}}),
        encoding="utf-8",
    )
    return plugin


def options(home):
    return parser().parse_args(["--host", "hermes", "--home", str(home), "--test-mode", "--worker", "manual"])


def test_catalog_post_setup_uses_existing_setup_without_prompt_or_plugin_writes(fake_user, monkeypatch):
    home = fake_user / ".hermes"
    plugin = catalog_plugin(home)
    before = snapshot(plugin)
    (home / "config.yaml").write_bytes(b"# keep\nmemory:\n  provider: builtin # chosen\n")
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("setup prompted"))
    from scope_recall.maintenance.setup import configure_hermes

    provider = ScopeRecallHermesAdapter(setup=configure_hermes)
    provider.post_setup(str(home), {})
    assert snapshot(plugin) == before
    assert (home / "config.yaml").read_bytes() == b"# keep\nmemory:\n  provider: scope-recall # chosen\n"
    assert (home / "skills" / "scope-recall-memory" / "SKILL.md").is_file()
    manifest = load_binding_for_home(home)
    assert manifest.data_directory == fake_user / ".scope-recall" / "shared"
    assert manifest.agent_identity == "default"
    receipt = json.loads((home / ".scope-recall-install-receipt.json").read_text("utf-8"))
    assert receipt["plugin_directory_owner"] == "hermes"
    assert not any(Path(row["path"]).is_relative_to(plugin) for row in receipt["files"])


def test_selected_provider_without_any_data_is_fresh(fake_user):
    home = fake_user / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: scope-recall\n", encoding="utf-8")
    plan = build_plan(options(home))
    assert not plan.blockers
    assert plan.hosts[0]["state"] == "fresh"


def test_two_profiles_share_store_with_distinct_host_identities(fake_user):
    homes = [fake_user / ".hermes", fake_user / ".hermes" / "profiles" / "research"]
    for home, identity in zip(homes, ["default", "research"], strict=True):
        home.mkdir(parents=True, exist_ok=True)
        result = apply_setup(build_plan(options(home)))
        assert result["status"] == "ready", result
        manifest = load_binding_for_home(home)
        assert manifest.agent_identity == identity
        provider = ScopeRecallHermesAdapter()
        try:
            provider.initialize(
                "TEST-profile",
                hermes_home=str(home),
                platform="cli",
                user_id="local",
                agent_identity=identity,
                agent_workspace="hermes",
            )
        finally:
            provider.shutdown()
    payload = read_shared_payload(fake_user / ".scope-recall" / "shared")
    assert {(row["entry_id"], row["agent_identity"]) for row in payload["entries"]} == {
        ("hermes", "default"),
        ("hermes-research", "research"),
    }


def test_profile_entry_names_fit_entry_ids_or_stay_plain(fake_user):
    from scope_recall.maintenance.setup_apply import hermes_entry_name

    profiles = fake_user / ".hermes" / "profiles"
    assert hermes_entry_name(fake_user / ".hermes") == "hermes"
    assert hermes_entry_name(profiles / "night_shift") == "hermes-night-shift"
    assert hermes_entry_name(profiles / ("p" * 30)) == "hermes"
    assert hermes_entry_name(fake_user / "elsewhere") == "hermes"


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_provider_edit_preserves_all_other_bytes(fake_user, ending):
    home = fake_user / ".hermes"
    home.mkdir()
    original = ending.join(
        [
            b"# heading",
            b"model: 'TEST'",
            b"memory: # comment",
            b'  provider: "builtin" # keep',
            b"  limit: 42",
            b"tail: true",
            b"",
        ]
    )
    (home / "config.yaml").write_bytes(original)
    _activate_hermes(home, [])
    assert (home / "config.yaml").read_bytes() == original.replace(b'"builtin"', b'"scope-recall"')


@pytest.mark.parametrize(
    "original",
    [
        "memory: {provider: builtin}\n",
        "memory: &m\n  provider: builtin\ncopy: *m\n",
        "defaults: &m\n  provider: builtin\nmemory: *m\n",
    ],
)
def test_provider_edit_refuses_unsafe_yaml_without_writes(fake_user, original):
    home = fake_user / ".hermes"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(original, encoding="utf-8")
    before = snapshot(home)
    with pytest.raises(InstallError, match="memory|provider"):
        _activate_hermes(home, [])
    assert snapshot(home) == before


def test_unbound_provider_is_unavailable_and_hooks_do_not_raise(fake_user):
    provider = ScopeRecallHermesAdapter()
    assert not provider.is_available()
    assert "hermes memory setup" in provider.unavailable_reason()
    provider.sync_turn("TEST-user", "TEST-answer")
    provider.on_session_end([])
    provider.shutdown()


def test_catalog_manifest_declares_hooks_and_pinned_core():
    from scope_recall._version import __version__

    root = Path(__file__).resolve().parents[2]
    for relative in ["adapters/hermes/plugin.yaml", "distribution/hermes/plugin.yaml"]:
        value = yaml.safe_load((root / relative).read_text("utf-8"))
        assert "provides_hooks" in value and "hooks" not in value
    value = yaml.safe_load((root / "distribution/hermes/plugin.yaml").read_text("utf-8"))
    assert value["requires_hermes"] == ">=0.22.5"
    assert value["pip_dependencies"] == [f"hermes-scope-recall[vectors]=={__version__}"]


@pytest.mark.parametrize("child", ["  honcho:\n    enabled: true\n", "  values:\n    - a\n", "  prompt: |\n    TEST\n"])
def test_missing_provider_stays_inside_memory_with_nested_last_child(fake_user, child):
    home = fake_user / ".hermes"
    home.mkdir()
    original = "memory:\n" + child + "model:\n  provider: TEST-model\n"
    (home / "config.yaml").write_text(original, encoding="utf-8")
    _activate_hermes(home, [])
    result = (home / "config.yaml").read_text("utf-8")
    assert result == original.replace("memory:\n", "memory:\n  provider: scope-recall\n", 1)
    assert yaml.safe_load(result)["model"]["provider"] == "TEST-model"


@pytest.mark.parametrize("original", [b"# TEST\r\nmodel: 'TEST'\r\n", b"model: TEST", b"\xef\xbb\xbf# TEST\n"])
def test_missing_memory_appends_without_changing_original_bytes(fake_user, original):
    home = fake_user / ".hermes"
    home.mkdir()
    path = home / "config.yaml"
    path.write_bytes(original)
    _activate_hermes(home, [])
    assert path.read_bytes().startswith(original)
    assert yaml.safe_load(path.read_bytes())["memory"]["provider"] == "scope-recall"


def legacy_entry(user):
    from scope_recall.adapters.shared_entries import write_shared_payload
    from scope_recall.maintenance.shared import attach, init_shared

    home, store = user / ".hermes", user / ".scope-recall" / "shared"
    init_shared(root=store, agent_id="scope-recall", test_mode=True)
    attach(host="hermes", instance_root=home, root=store, entry_id="hermes", display_name="TEST")
    payload = read_shared_payload(store)
    payload["entries"][0].pop("agent_identity", None)
    write_shared_payload(store, payload)
    return home, store


def write_original(home, host, entry, text):
    from scope_recall.contracts import TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.maintenance.doctor import load_binding

    from tests.v11_support import source_event

    binding, _ = load_binding(host, home)
    core = MemoryCore(CoreConfig(binding))
    core.record_event(
        TrustedContext(binding, "TEST-history", binding.scope_ids, "human_direct", entry_id=entry),
        source_event(content=text),
        scope_id=next(iter(binding.scope_ids)),
    )


def source_rows(store):
    with sqlite3.connect(store / "memory.sqlite3") as db:
        return db.execute("SELECT * FROM source_events ORDER BY source_id").fetchall()


def test_unused_legacy_identity_repair_preserves_other_entry_data(fake_user):
    from scope_recall.maintenance.shared import attach

    home, store = legacy_entry(fake_user)
    client = fake_user / ".codex"
    attach(
        host="codex",
        instance_root=client,
        root=store,
        entry_id="codex",
        display_name="TEST-client",
        grants_like=("hermes",),
        capture_like="hermes",
    )
    write_original(client, "codex", "codex", "TEST other entry original remains")
    before = source_rows(store)
    identity_before = read_shared_payload(store)["installation_id"]
    result = apply_setup(build_plan(options(home)))
    assert result["status"] == "ready", result
    repair = result["hosts"][0]["identity_repair"]
    assert repair["captured_sources"] == 0 and repair["agent_identity"] == "default"
    assert Path(repair["receipt"]).is_file()
    assert read_shared_payload(store)["installation_id"] == identity_before
    assert read_shared_payload(store)["agent_id"] == "scope-recall"
    assert source_rows(store) == before
    assert load_binding_for_home(home).agent_identity == "default"


def test_legacy_identity_with_captured_sources_is_not_rewritten(fake_user):
    home, store = legacy_entry(fake_user)
    write_original(home, "hermes", "hermes", "TEST legacy original remains")
    before = (store / "installation.json").read_bytes(), source_rows(store)
    plan = build_plan(options(home))
    assert plan.blockers
    assert (store / "installation.json").read_bytes() == before[0]
    assert source_rows(store) == before[1]


def test_catalog_uninstall_preserves_directory_even_with_old_receipt(fake_user):
    from scope_recall.maintenance.install import apply_uninstall, plan_uninstall

    home = fake_user / ".hermes"
    home.mkdir()
    assert apply_setup(build_plan(options(home)))["status"] == "ready"
    plugin = catalog_plugin(home)
    before = snapshot(plugin)
    plan = plan_uninstall(instance_root=home)
    assert not plan.conflicts
    assert not any(Path(name).is_relative_to(plugin) for name in plan.files_to_remove)
    apply_uninstall(plan)
    assert snapshot(plugin) == before


def test_self_check_uses_selected_entry_with_real_profile_identity(fake_user):
    from scope_recall.adapters.hermes.identity import HermesIdentityError
    from scope_recall.maintenance.setup_verify import verify_memory

    home, _store = legacy_entry(fake_user)
    with pytest.raises(HermesIdentityError, match="agent_identity conflict"):
        verify_memory(home, "hermes", {})


def test_catalog_record_and_manifest_name_are_both_required(fake_user):
    from scope_recall.maintenance.install_hermes import host_owned_plugin

    home = fake_user / ".hermes"
    plugin = catalog_plugin(home)
    assert host_owned_plugin(home, plugin)
    (plugin / "plugin.yaml").write_text("name: unrelated\n", encoding="utf-8")
    assert not host_owned_plugin(home, plugin)


def test_registered_provider_has_setup_callback(fake_user):
    from types import SimpleNamespace

    from scope_recall.adapters.hermes import register_adapter
    from scope_recall.maintenance.setup import configure_hermes

    provider = register_adapter(
        SimpleNamespace(register_memory_provider=lambda *a: None, register_hook=lambda *a, **kw: None)
    )
    try:
        assert provider._setup is configure_hermes
    finally:
        provider.shutdown()


def test_unattended_setup_refuses_unsafe_config_without_prompt_or_activation(fake_user, monkeypatch):
    from scope_recall.maintenance.setup import configure_hermes

    home = fake_user / ".hermes"
    catalog_plugin(home)
    (home / "config.yaml").write_text("memory: {provider: builtin}\n", encoding="utf-8")
    before = snapshot(fake_user)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unattended setup prompted"))
    with pytest.raises(SystemExit, match="memory.provider"):
        ScopeRecallHermesAdapter(setup=configure_hermes).post_setup(str(home), {})
    assert snapshot(fake_user) == before


def test_wizard_records_the_plugin_environment_interpreter(tmp_path):
    """Inside Hermes ``sys.executable`` is the base interpreter; it cannot import the plugin environment's package."""
    from scope_recall.maintenance.setup import package_interpreter

    venv = tmp_path / "TEST-plugin-env" / "venv"
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (venv / "pyvenv.cfg").write_text("home = TEST-base\n", encoding="utf-8")
    module = venv / "Lib" / "site-packages" / "scope_recall" / "maintenance" / "setup.py"
    assert package_interpreter(module) == python
    assert package_interpreter(tmp_path / "TEST-checkout" / "maintenance" / "setup.py") == Path(sys.executable)


def test_wizard_failure_names_the_installer_reason(fake_user):
    """The host wizard prints no traceback, so the refusal text itself must reach the user."""
    from scope_recall.maintenance.setup import configure_hermes

    home = fake_user / ".hermes"
    catalog_plugin(home)
    # An earlier installation's namespace in the user folder makes the default store location an agent's home.
    (fake_user / "scope-recall").mkdir()
    with pytest.raises(SystemExit, match="inside an agent's home") as failure:
        ScopeRecallHermesAdapter(setup=configure_hermes).post_setup(str(home), {})
    assert "Setup receipt:" in str(failure.value)
    config = home / "config.yaml"
    assert not config.exists() or "scope-recall" not in config.read_text("utf-8")
