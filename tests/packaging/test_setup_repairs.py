"""Setup preserves operator choices and proves readiness against the selected installation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from scope_recall.maintenance.setup import build_plan, parser
from scope_recall.maintenance.setup_apply import apply_setup
from scope_recall.maintenance.setup_configuration import read_models, runtime_payload
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig

from tests.packaging.test_setup import fake_user, snapshot  # noqa: F401


def options(host="workbuddy", *extra):
    return parser().parse_args(["--host", host, "--test-mode", "--worker", "manual", *extra])


def installed(user, host="workbuddy"):
    (user / ("." + host)).mkdir()
    args = options(host)
    result = apply_setup(build_plan(args))
    assert result["status"] == "ready", result
    return args, result


def model_file(user, **extra):
    value = {"auxiliary": {"external_embedding": False, "external_consolidation": False, **extra}}
    path = user / "TEST-models.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_model_update_preserves_shared_worker_and_previews_only_explicit_changes(fake_user):
    args, _ = installed(fake_user)
    store = build_plan(args).store
    path = store / "runtime-config.json"
    raw = json.loads(path.read_text("utf-8"))
    raw.update(supervisor_enabled=False, max_items=7, daily_work_limit=19, owner_id="TEST-worker")
    raw["binding"]["scope_ids"].append("TEST-narrow-other")
    path.write_text(json.dumps(raw), encoding="utf-8")
    route = model_file(fake_user, consolidation_reserve_input=8192)
    args = parser().parse_args(["--host", "workbuddy", "--test-mode", "--models", str(route)])
    plan = build_plan(args)
    assert plan.to_dict().get("configuration_changes"), "the owner must see field-level before/after values"
    assert plan.worker == "manual"
    result = apply_setup(plan)
    assert result["status"] == "ready", result
    after = json.loads(path.read_text("utf-8"))
    for key in raw.keys() - {"auxiliary"}:
        assert after[key] == raw[key], key
    assert after["auxiliary"]["consolidation_reserve_input"] == 8192


def test_target_vector_failure_prevents_full_state_even_when_synthetic_chain_passes(fake_user, monkeypatch):
    from scope_recall.maintenance import setup_apply, setup_verify

    args, _ = installed(fake_user)
    doctor = setup_apply.run_doctor

    def damaged(**kwargs):
        report = doctor(**kwargs)
        report.capability_gaps.append("vector_store_unreadable")
        return report

    monkeypatch.setattr(setup_apply, "run_doctor", damaged)
    monkeypatch.setattr(
        setup_verify, "verify_memory", lambda *a, **kw: dict(cross_session_recall=True, models_verified=True)
    )
    result = apply_setup(build_plan(args))
    assert result["state"] == "原文检索可用", result
    assert "vector_store_unreadable" in str(result["missing"])


def test_host_secret_backup_stays_adjacent_and_rolls_back_exactly(fake_user, capsys):
    home = fake_user / ".hermes"
    home.mkdir()
    path = home / "config.yaml"
    original = b"memory:\n  provider: builtin\nother_service:\n  api_key: TEST-synthetic-sensitive-value\n"
    path.write_bytes(original)
    result = apply_setup(build_plan(options("hermes")))
    assert result["status"] == "ready", result
    backups = [Path(p) for p in result["backups"] if Path(p).read_bytes() == original]
    assert backups and all(p.parent == home for p in backups)
    for p in home.rglob("*"):
        if p.is_file() and p != path and p not in backups:
            assert b"TEST-synthetic-sensitive-value" not in p.read_bytes(), str(p)
    receipt = Path(result["receipt"]).read_text("utf-8")
    assert "TEST-synthetic-sensitive-value" not in receipt + capsys.readouterr().out
    assert result["backup_hashes"][str(backups[0])]
    if os.name != "nt":
        assert backups[0].stat().st_mode & 0o777 == 0o600
    path.write_bytes(backups[0].read_bytes())
    assert path.read_bytes() == original


def test_budget_recommendations_scale_usage_period_and_inflight_separately():
    from scope_recall.maintenance.setup_workload import recommended_totals

    value = recommended_totals(daily_turns=50, period="monthly", days=31)
    assert value["turns"] == 1550
    assert value["safety_factor"] > 1
    assert value["inflight_input"] > 0
    assert value["cap_micro_usd"] == 0
    assert value["total_input_cap"] == value["usage_input_cap"] + value["inflight_input"]


def test_wal_discovery_never_creates_sidecars(fake_user):
    from scope_recall.maintenance.onboarding import inspect_installation

    home = fake_user / ".codex"
    data = home / "data"
    data.mkdir(parents=True)
    path = data / "memory.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE source_events(id)")
        db.execute("CREATE TABLE instance_meta(id)")
    (home / "codex-installation.json").write_text("{}", encoding="utf-8")
    before = snapshot(home)
    inspect_installation(home, host="codex")
    assert snapshot(home) == before


def test_cold_cli_dry_run_leaves_the_user_tree_untouched(fake_user):
    # A dry run promises nothing changes in user files, host configuration or stores.
    # Python's own bytecode cache beside the installed package is outside that promise,
    # so the package lives outside the snapshot and bytecode writing stays enabled.
    root = Path(__file__).resolve().parents[2]
    source = fake_user.parent / "TEST-cold-package" / "scope_recall"
    manifest = json.loads((root / "packaging/v11-module-allowlist.json").read_text("utf-8"))
    files = manifest["python_modules"] + manifest["package_data"] if isinstance(manifest, dict) else manifest
    # Use the wheel's declared Python sources, excluding test/build products.
    for name in files:
        rel = Path(name)

        if str(rel).startswith("scope_recall/"):
            rel = Path(*rel.parts[1:])
        if (root / rel).is_file():
            target = source / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / rel).read_bytes())
    (source / "__init__.py").write_bytes((root / "__init__.py").read_bytes())
    (fake_user / ".workbuddy").mkdir()
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPATH"] = str(source.parent)
    # Console scripts sit beside a venv interpreter but in Scripts/ (or bin/) of a base installation.
    command = Path(sysconfig.get_path("scripts")) / ("scope-recall.exe" if os.name == "nt" else "scope-recall")
    before = snapshot(fake_user)
    child = subprocess.run(
        [str(command), "setup", "--dry-run", "--test-mode"],
        env=env,
        capture_output=True,
        timeout=45,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
    assert snapshot(fake_user) == before


def test_model_caps_json_round_trips_through_runtime_reader(fake_user):
    path = model_file(
        fake_user,
        budget=dict(
            approved_models=["TEST-model"],
            pricing={"TEST-model": dict(input_usd_per_million="0", output_usd_per_million="0")},
            model_token_caps={"TEST-model": dict(input=100000, output=10000)},
        ),
    )
    value = read_models(path)
    auxiliary = AuxiliaryRuntimeConfig.from_mapping(json.loads(json.dumps(value["auxiliary"])))
    assert auxiliary.budget.model_token_caps["TEST-model"] == (100000, 10000)


def test_each_project_registration_has_its_own_prior_bytes_in_receipt(fake_user):
    from scope_recall.adapters.clients.config import install_codex_scope_recall

    home = fake_user / ".codex"
    projects = [fake_user / ("TEST-project-" + str(i)) for i in range(3)]
    for project in projects:
        (project / ".git").mkdir(parents=True)
        (project / ".git/config").write_text('[remote "origin"]\nurl=https://example.test/repo.git\n', encoding="utf-8")
    config, core = install_codex_scope_recall(home, project_root=projects[0], agent_id="TEST-setup")
    first = apply_setup(build_plan(options("codex", "--project", str(projects[1]))), register_copies=True)
    previous = config.config_path.read_bytes()
    second = apply_setup(build_plan(options("codex", "--project", str(projects[2]))), register_copies=True)
    backups = [Path(p) for p in second["backups"] if Path(p).read_bytes() == previous]
    assert backups and not set(second["backups"]) & set(first["backups"])
    assert "project-registration" in second["completed"]


def test_generated_docs_cover_nested_wire_shapes_and_optional_defaults():
    from scripts.generate_configuration_docs import render

    text = render()
    for name in ("InstanceBinding", "ModelPricing", "SubscriptionBudgetPolicy"):
        assert "## " + name in text
    assert '"input"' in text and '"output"' in text
    row = next(line for line in text.splitlines() if "`consolidation_reserve_input`" in line)
    assert "required" not in row and "32768" in row


def test_codex_rerun_keeps_existing_plugin_policy_and_metadata(fake_user):
    from scope_recall.maintenance.setup_activation import register_codex

    target = fake_user / ".codex/skills/scope-recall"
    path = fake_user / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    old = dict(
        name="scope-recall",
        source=dict(source="local", path="./.codex/skills/scope-recall"),
        policy=dict(installation="AVAILABLE", authentication="ON_USE"),
        custom=dict(owner="TEST-owner"),
    )
    path.write_text(json.dumps(dict(name="local", plugins=[old])), encoding="utf-8")
    register_codex(fake_user / ".codex", target, fake_user, [])
    entry = json.loads(path.read_text("utf-8"))["plugins"][0]
    assert all(entry[key] == value for key, value in old.items())


def test_install_and_registration_conflicts_are_found_before_binding_writes(fake_user):
    home = fake_user / ".workbuddy"
    home.mkdir()
    (home / "settings.json").write_text("[]", encoding="utf-8")
    before = snapshot(fake_user)
    plan = build_plan(options())
    assert plan.blockers, "invalid host merge must be detected during planning"
    assert snapshot(fake_user) == before


def test_setup_binding_uses_the_shared_runtime_contract(fake_user):
    from scope_recall.maintenance.doctor import load_binding
    from scope_recall.maintenance.shared import worker_configuration

    args, _ = installed(fake_user)
    binding, _ = load_binding("workbuddy", fake_user / ".workbuddy")
    value = runtime_payload(binding)
    expected = worker_configuration(value, binding)
    for key in ("binding", "allowed_scope_ids", "auxiliary"):
        assert value[key] == expected[key]
    assert value["session_id"] == expected["session_id"]


def test_install_documentation_distinguishes_adapter_probe_from_entry_commands():
    text = (Path(__file__).resolve().parents[2] / "docs/install.md").read_text("utf-8")
    assert "The installation check exercises host adapters and installed commands." not in text
    assert "separate" in text[text.index("## Verification and readiness") : text.index("## Going back")]


def test_every_parser_configuration_input_is_documented():
    import ast
    import dataclasses

    from scripts.generate_configuration_docs import input_definitions, render

    text = render()
    documented = {item.name for contract in input_definitions() for item in dataclasses.fields(contract.definition)}
    root = Path(__file__).resolve().parents[2]
    accepted = set()
    for name in ("instance_config", "auxiliary", "codex_cli", "subscription_budget"):
        tree = ast.parse((root / "runtime" / (name + ".py")).read_text("utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not (
                function.name.endswith("from_mapping") or function.name == "_consolidation_opt_in"
            ):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"raw", "binding_raw", "rates"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    accepted.add(node.args[0].value)
    assert accepted <= documented
    assert all("`" + name + "`" in text for name in accepted)


def test_preflight_rechecks_changed_host_files_without_leaving_partial_bindings(fake_user):
    home = fake_user / ".workbuddy"
    home.mkdir()
    plan = build_plan(options())
    assert not plan.blockers
    (home / "settings.json").write_text("[]", encoding="utf-8")
    before = snapshot(fake_user)
    result = apply_setup(plan)
    assert result["status"] == "blocked" and result["failed_step"] == "preflight"
    assert snapshot(fake_user) == before


def test_model_updates_preserve_a_custom_worker_ledger_identity(fake_user):
    args, _ = installed(fake_user)
    path = build_plan(args).store / "runtime-config.json"
    raw = json.loads(path.read_text("utf-8"))
    custom = fake_user / "TEST-custom-spend.sqlite3"
    raw["auxiliary"]["ledger_path"] = str(custom)
    raw["auxiliary"]["installation_dir"] = str(fake_user / "TEST-custom-control")
    path.write_text(json.dumps(raw), encoding="utf-8")
    route = model_file(fake_user, consolidation_reserve_input=8192)
    plan = build_plan(options("workbuddy", "--models", str(route)))
    result = apply_setup(plan)
    assert result["status"] == "ready", result
    after = json.loads(path.read_text("utf-8"))
    assert after["auxiliary"]["ledger_path"] == str(custom)
    assert after["auxiliary"]["installation_dir"] == raw["auxiliary"]["installation_dir"]


def test_guided_model_changes_are_recomputed_before_confirmation(fake_user, monkeypatch, capsys):
    from scope_recall.maintenance import setup_models
    from scope_recall.maintenance.setup import main

    installed(fake_user)
    recipe = read_models(model_file(fake_user, consolidation_reserve_input=8192))
    monkeypatch.setattr(setup_models, "configure_models", lambda: recipe)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    before = snapshot(fake_user)
    assert main(["--host", "workbuddy", "--configure-models", "--test-mode"]) == 0
    assert "consolidation_reserve_input" in capsys.readouterr().out
    assert snapshot(fake_user) == before


@pytest.mark.parametrize("obstacle", ["file_target", "parent_file", "marketplace"])
def test_all_planned_file_types_are_checked_before_mutation(fake_user, obstacle):
    if obstacle == "marketplace":
        (fake_user / ".codex").mkdir()
        target = fake_user / ".agents/plugins/marketplace.json"
        target.mkdir(parents=True)
        args = options("codex")
    else:
        home = fake_user / ".workbuddy"
        home.mkdir()
        if obstacle == "file_target":
            (home / "skills/scope-recall-memory/SKILL.md").mkdir(parents=True)
        else:
            (home / "skills").write_text("TEST-obstacle", encoding="utf-8")
        args = options()
    before = snapshot(fake_user)
    assert build_plan(args).blockers
    assert snapshot(fake_user) == before


@pytest.mark.parametrize(
    "gap,raw_available",
    [
        ("hermes_runtime_package_missing", False),
        ("hermes_runtime_package_mismatch", False),
        ("hermes_runtime_vector_dependencies_missing", True),
    ],
)
def test_hermes_target_environment_is_part_of_readiness(fake_user, monkeypatch, gap, raw_available):
    from scope_recall.maintenance import setup_apply, setup_verify

    args, _ = installed(fake_user, "hermes")
    doctor = setup_apply.run_doctor

    def broken(**kwargs):
        report = doctor(**kwargs)
        report.capability_gaps.append(gap)
        return report

    monkeypatch.setattr(setup_apply, "run_doctor", broken)
    monkeypatch.setattr(
        setup_verify, "verify_memory", lambda *a, **kw: dict(cross_session_recall=True, models_verified=True)
    )
    result = apply_setup(build_plan(args))
    assert result["state"] == ("原文检索可用" if raw_available else None), result


@pytest.mark.parametrize("old_kind,new_kind", [("openai", "openai_responses"), ("openai_responses", "openai")])
def test_route_kind_changes_replace_only_the_route_subtree(fake_user, old_kind, new_kind):
    from scope_recall.maintenance.setup_configuration import validate_models

    from tests.v11_support import context

    def recipe(kind):
        route = dict(
            kind=kind,
            model="TEST-chat",
            endpoint="https://example.test/chat",
            credential_env="TEST_CHAT_ENV",
            max_output_tokens=512,
        )
        if kind == "openai":
            route["output_limit_field"] = "max_tokens"
        else:
            route["text_format"] = {"type": "json_object"}
        return validate_models(
            dict(auxiliary=dict(external_embedding=False, external_consolidation=False, consolidation=route))
        )

    binding = context(fake_user / "TEST-route-store").binding
    before = runtime_payload(binding, models=recipe(old_kind), worker="manual")
    before["max_items"] = 7
    after = runtime_payload(binding, models=recipe(new_kind), existing=before)
    assert AuxiliaryRuntimeConfig.from_mapping(after["auxiliary"]).consolidation.kind == new_kind
    assert after["max_items"] == 7


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_adjacent_backups_and_replacements_preserve_a_restricted_source_dacl(fake_user):
    import win32security
    from scope_recall.maintenance.setup_configuration import write_configuration

    path = fake_user / "TEST-private.yaml"
    path.write_text("TEST-sensitive-value", encoding="utf-8")
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;FA;;;OW)", 1)
    # This binding parses SECURITY_INFORMATION as a signed Windows long.
    win32security.SetFileSecurity(str(path), -2147483644, descriptor)

    def entries(file):
        acl = win32security.GetFileSecurity(str(file), 4).GetSecurityDescriptorDacl()
        return [
            (ace[0], ace[1], win32security.ConvertSidToStringSid(ace[2]))
            for ace in (acl.GetAce(i) for i in range(acl.GetAceCount()))
        ]

    original = entries(path)
    backups = []
    write_configuration(path, "TEST-sensitive-value-changed", backups)
    assert entries(path) == original
    assert entries(Path(backups[0])) == original


def test_local_codex_receipt_parent_is_checked_at_its_actual_location(fake_user):
    from scope_recall.adapters.clients.config import install_codex_scope_recall

    home = fake_user / ".codex"
    project = fake_user / "TEST-project"
    project.mkdir()
    install_codex_scope_recall(home, project_root=project)
    (home / "scope-recall").write_text("TEST-blocked-receipt-parent", encoding="utf-8")
    before = snapshot(fake_user)
    assert build_plan(options("codex", "--project", str(project))).blockers
    assert snapshot(fake_user) == before


def test_model_wire_object_fields_replace_their_complete_shape():
    from scope_recall.maintenance.setup_configuration import merge_model_fields

    before = dict(
        auxiliary=dict(
            consolidation=dict(
                endpoint="https://example.test/chat",
                response_format=dict(type="json_schema", json_schema=dict(name="old")),
            )
        )
    )
    after = merge_model_fields(
        before, dict(auxiliary=dict(consolidation=dict(response_format=dict(type="json_object"))))
    )
    assert after["auxiliary"]["consolidation"]["response_format"] == {"type": "json_object"}
    assert after["auxiliary"]["consolidation"]["endpoint"] == before["auxiliary"]["consolidation"]["endpoint"]


def test_route_replacement_plan_includes_removed_active_fields(fake_user):
    args, _ = installed(fake_user)
    store = build_plan(args).store
    path = store / "runtime-config.json"
    raw = json.loads(path.read_text("utf-8"))
    raw["auxiliary"]["consolidation"] = dict(
        model="TEST-chat",
        endpoint="https://example.test/chat",
        credential_env="TEST_CHAT_ENV",
        max_output_tokens=512,
        output_limit_field="max_tokens",
    )
    path.write_text(json.dumps(raw), encoding="utf-8")
    route = model_file(
        fake_user,
        consolidation=dict(
            kind="openai_responses",
            model="TEST-chat",
            endpoint="https://example.test/responses",
            credential_env="TEST_CHAT_ENV",
            max_output_tokens=512,
        ),
    )
    plan = build_plan(options("workbuddy", "--models", str(route)))
    assert any(
        row["field"] == "auxiliary.consolidation.output_limit_field" and row.get("action") == "remove"
        for row in plan.configuration_changes
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows host configuration DACL")
def test_uninstall_preserves_the_host_configuration_access_boundary(fake_user):
    import win32security
    from scope_recall.maintenance.install import apply_uninstall, plan_uninstall

    _, result = installed(fake_user)
    home = fake_user / ".workbuddy"
    path = home / "settings.json"
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;FA;;;OW)", 1)
    win32security.SetFileSecurity(str(path), -2147483644, descriptor)
    apply_uninstall(plan_uninstall(instance_root=home))
    acl = win32security.GetFileSecurity(str(path), 4).GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 1
    assert win32security.ConvertSidToStringSid(acl.GetAce(0)[2]) == "S-1-3-4"
