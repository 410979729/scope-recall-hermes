"""Complete setup on synthetic host homes, with no real host or model service."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from scope_recall.maintenance.setup import build_plan, parser
from scope_recall.maintenance.setup_apply import apply_setup


@pytest.fixture
def fake_user(tmp_path, monkeypatch):
    user = tmp_path / "TEST-user"
    user.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(user))
    for name, directory in (
        ("HERMES_HOME", ".hermes"),
        ("CODEX_HOME", ".codex"),
        ("CLAUDE_CONFIG_DIR", ".claude"),
        ("CODEBUDDY_CONFIG_DIR", ".workbuddy"),
        ("DSH_HOME", ".dsh"),
    ):
        monkeypatch.setenv(name, str(user / directory))
    yield user
    from scope_recall.adapters.clients.local_endpoint import stop_residents
    from scope_recall.runtime.process_probe import probe_process

    stopped = [
        pid
        for host, folder in (
            ("codex", ".codex"),
            ("claude-code", ".claude"),
            ("workbuddy", ".workbuddy"),
            ("dsh", ".dsh"),
        )
        for pid in stop_residents(user / folder, host)
    ]
    deadline = time.monotonic() + 5
    while any(probe_process(pid).running for pid in stopped) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(probe_process(pid).running for pid in stopped), "owned resident did not exit"


def snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
    }


def test_dry_run_writes_nothing_and_refusal_does_not_apply(fake_user, monkeypatch, capsys, record_property):
    from scope_recall.maintenance.setup import main

    (fake_user / ".workbuddy").mkdir()
    (fake_user / ".workbuddy" / "settings.json").write_text('{"theme":"TEST-theme"}', encoding="utf-8")
    before = snapshot(fake_user)
    assert main(["--dry-run", "--plan-json"]) == 0
    plan_output = capsys.readouterr().out
    record_property("dry_run_plan", plan_output)
    value = json.loads(plan_output)
    assert value["models"]["budget"]["total_call_cap"] == 0
    after = snapshot(fake_user)
    assert after == before
    import hashlib

    for label, data in (("dry_run_before", before), ("dry_run_after", after)):
        record_property(
            label,
            json.dumps(
                {
                    name: {
                        "mtime_ns": row[0],
                        "sha256": hashlib.sha256(row[1]).hexdigest() if row[1] is not None else None,
                    }
                    for name, row in data.items()
                }
            ),
        )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main([]) == 0
    assert snapshot(fake_user) == before


@pytest.mark.parametrize(
    "host,folder",
    [
        ("codex", ".codex"),
        ("claude-code", ".claude"),
        ("hermes", ".hermes"),
        ("workbuddy", ".workbuddy"),
        ("dsh", ".dsh"),
    ],
)
def test_new_install_and_rerun_preserve_truth(fake_user, host, folder, record_property):
    home = fake_user / folder
    home.mkdir()
    started = time.monotonic()
    args = parser().parse_args(["--host", host, "--test-mode", "--worker", "manual"])
    plan = build_plan(args)
    result = apply_setup(plan)
    record_property("first_setup", json.dumps(result, ensure_ascii=False))
    assert result["status"] == "ready", result
    assert result["state"] == "原文检索可用"
    assert [ord(char) for char in result["state"]] == [21407, 25991, 26816, 32034, 21487, 29992]
    assert result["hosts"][0]["verification"]["cross_session_recall"]
    record_property("seconds_to_recall", time.monotonic() - started)
    store = json.loads((home / "scope-recall" / "attachment.json").read_text(encoding="utf-8"))["root"]
    identifier = json.loads((Path(store) / "installation.json").read_text(encoding="utf-8"))["installation_id"]
    import sqlite3

    from scope_recall.contracts import TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.maintenance.doctor import load_binding

    from tests.v11_support import source_event

    binding, _directory = load_binding(host, home)
    core = MemoryCore(CoreConfig(binding))
    core.record_event(
        TrustedContext(binding, "TEST-existing", binding.scope_ids, "human_direct", entry_id=host),
        source_event(content="TEST existing original remains byte exact"),
        scope_id=next(iter(binding.scope_ids)),
    )
    with sqlite3.connect(Path(store) / "memory.sqlite3") as db:
        originals = db.execute(
            "SELECT source_id,content,source_revision FROM source_events ORDER BY source_id"
        ).fetchall()
    # The second setup must reuse the binding and store even after a wrapper disappears.
    if host in {"workbuddy", "dsh"}:
        skill = home / "skills" / "scope-recall-memory" / "SKILL.md"
        assert skill.is_file()
        skill.unlink()
    again = apply_setup(build_plan(args))
    record_property("rerun_setup", json.dumps(again, ensure_ascii=False))
    assert again["status"] == "ready", again
    assert json.loads((Path(store) / "installation.json").read_text(encoding="utf-8"))["installation_id"] == identifier
    with sqlite3.connect(Path(store) / "memory.sqlite3") as db:
        assert (
            db.execute("SELECT source_id,content,source_revision FROM source_events ORDER BY source_id").fetchall()
            == originals
        )
    if host in {"workbuddy", "dsh"}:
        assert skill.is_file()
    assert not list(home.rglob("setup-check-*"))


def test_configured_models_use_the_existing_metered_test_transport(fake_user, monkeypatch, record_property, capsys):
    from scope_recall.runtime import instance
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime

    from tests.contract.test_runtime_auxiliary import FakeTransport

    (fake_user / ".workbuddy").mkdir()
    calls = []

    def response(**kwargs):
        body = json.loads(kwargs["body"])
        calls.append(body)
        if "input" in body:
            data = {
                "data": [{"embedding": [0.01] * 64, "index": index} for index, _text in enumerate(body["input"])],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            }
        else:
            answer = {
                "protocol_version": "1.1",
                "source_refs": [],
                "claim_proposals": [],
                "resume_proposals": [],
                "reference_proposals": [],
            }
            data = {
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        return 200, json.dumps(data).encode()

    transport = FakeTransport(response)
    monkeypatch.setattr(
        instance, "build_auxiliary_runtime", lambda config: build_auxiliary_runtime(config, transport=transport)
    )
    monkeypatch.setenv("SCOPE_RECALL_TEST_SETUP_KEY", "test-placeholder")
    models = {
        "auxiliary": {
            "external_embedding": True,
            "external_consolidation": True,
            "budget": {
                "cap_micro_usd": 1000000,
                "total_input_cap": 10000000,
                "total_output_cap": 1000000,
                "total_call_cap": 100,
                "approved_models": ["TEST-embedding", "TEST-chat"],
                "pricing": {
                    name: {"input_usd_per_million": "0", "output_usd_per_million": "0"}
                    for name in ("TEST-embedding", "TEST-chat")
                },
            },
            "embedding": {
                "model": "TEST-embedding",
                "endpoint": "https://example.test/v1/embeddings",
                "dimensions": 64,
                "dialect": "openai",
                "credential_env": "SCOPE_RECALL_TEST_SETUP_KEY",
            },
            "consolidation": {
                "model": "TEST-chat",
                "endpoint": "https://example.test/v1/chat/completions",
                "credential_env": "SCOPE_RECALL_TEST_SETUP_KEY",
                "output_limit_field": "max_tokens",
                "max_output_tokens": 512,
            },
        },
        "vector_threshold": 0.7,
        "vector": {
            "backend": "sqlite-bruteforce",
            "storage_dir": str(fake_user / "TEST-vector"),
            "table_name": "TEST-vectors",
            "dimensions": 64,
            "test_injection_override": True,
        },
    }
    route = fake_user / "TEST-routes.json"
    route.write_text(json.dumps(models), encoding="utf-8")
    args = parser().parse_args(["--host", "workbuddy", "--models", str(route), "--test-mode", "--worker", "manual"])
    plan = build_plan(args)
    assert "test-placeholder" not in json.dumps(plan.to_dict())
    from scope_recall.maintenance.setup import main

    started = time.monotonic()
    assert main(["--host", "workbuddy", "--models", str(route), "--test-mode", "--worker", "manual", "--yes"]) == 0
    stdout = capsys.readouterr().out
    result = json.loads("{" + stdout.rsplit("\n{", 1)[1])
    record_property("seconds_to_full_recall", time.monotonic() - started)
    record_property("configured_stdout", stdout)
    record_property("configured_setup", json.dumps(result, ensure_ascii=False))
    assert result["status"] == "ready", result
    assert result["state"] == "完整记忆功能可用", result
    assert any("input" in body for body in calls)
    assert any("messages" in body for body in calls)
    receipt = Path(result["receipt"]).read_text(encoding="utf-8")
    assert "test-placeholder" not in receipt

    import sqlite3

    from scope_recall.maintenance.install_common import InstallError

    store = plan.store
    with sqlite3.connect(store / "auxiliary-budget.sqlite3") as db:
        used = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert used > 0
    calls_before = len(calls)
    models["auxiliary"]["budget"]["total_call_cap"] = used
    route.write_text(json.dumps(models), encoding="utf-8")
    exhausted = apply_setup(build_plan(args))
    assert exhausted["status"] == "ready"
    assert exhausted["state"] == "原文检索可用"
    assert len(calls) == calls_before
    models["auxiliary"]["embedding"]["model"] = "TEST-another-space"
    route.write_text(json.dumps(models), encoding="utf-8")
    before = snapshot(fake_user)
    blocked = build_plan(args)
    assert any("embedding space change" in reason for reason in blocked.blockers)
    with pytest.raises(InstallError, match="blocked"):
        apply_setup(blocked)
    assert snapshot(fake_user) == before


def test_failure_has_completed_stages_and_can_resume(fake_user, monkeypatch):
    from scope_recall.maintenance import setup_apply

    home = fake_user / ".workbuddy"
    home.mkdir()
    args = parser().parse_args(["--host", "workbuddy", "--test-mode", "--worker", "manual"])
    original = setup_apply._install
    monkeypatch.setattr(setup_apply, "_install", lambda *args: (_ for _ in ()).throw(OSError("TEST failure")))
    failed = apply_setup(build_plan(args))
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "workbuddy:install"
    assert failed["completed"] == ["workbuddy:binding", "workbuddy:configuration"]
    monkeypatch.setattr(setup_apply, "_install", original)
    assert apply_setup(build_plan(args))["status"] == "ready"


def test_origin_copy_requires_its_own_confirmation(fake_user, monkeypatch):
    from scope_recall.adapters.clients.config import install_codex_scope_recall, load_codex_config
    from scope_recall.maintenance.doctor import run_doctor
    from scope_recall.maintenance.setup_projects import project_suggestions, register_project_copy

    first, second = fake_user / "TEST-first", fake_user / "TEST-second"
    for project in (first, second):
        (project / ".git").mkdir(parents=True)
        (project / ".git" / "config").write_text(
            '[remote "origin"]\nurl=https://example.test/team/project.git\n', encoding="utf-8"
        )
    home = fake_user / ".codex"
    config, _core = install_codex_scope_recall(home, project_root=first, agent_id="TEST-setup")
    suggestion = project_suggestions(home, second)[0]
    report = run_doctor(host="codex", instance_root=home, project_root=second)
    assert report.project_copy_suggestions == [suggestion]
    assert str(second) not in config.project_roots
    args = parser().parse_args(
        ["--host", "codex", "--project", str(second), "--test-mode", "--worker", "manual", "--yes"]
    )
    assert apply_setup(build_plan(args))["status"] == "ready"
    assert str(second) not in load_codex_config(config.config_path).project_roots
    assert apply_setup(build_plan(args), register_copies=True)["status"] == "ready"
    config = load_codex_config(config.config_path)
    assert config.project_roots[os.path.normcase(str(second))] == config.project_roots[os.path.normcase(str(first))]
    assert project_suggestions(home, second) == []


def test_unknown_route_fields_and_secret_values_are_not_copied(fake_user):
    from scope_recall.maintenance.setup_configuration import read_models

    path = fake_user / "TEST-models.json"
    path.write_text(
        json.dumps(
            {
                "auxiliary": {
                    "external_embedding": False,
                    "external_consolidation": False,
                    "password": "TEST-value-is-never-copied",
                }
            }
        ),
        encoding="utf-8",
    )
    value = read_models(path)
    assert "password" not in json.dumps(value)


def test_generated_configuration_reference_matches_definitions():
    from scripts.generate_configuration_docs import TARGET, render

    assert TARGET.read_text(encoding="utf-8") == render()


def test_guided_models_ask_for_environment_names_without_key_values(monkeypatch):
    from scope_recall.maintenance.setup_models import configure_models

    answers = iter(
        [
            "TEST-embedding",
            "https://example.test/v1/embeddings",
            "64",
            "openai",
            "TEST_EMBED_ENV",
            "TEST-chat",
            "https://example.test/v1/chat/completions",
            "TEST_CHAT_ENV",
            "max_tokens",
            "0",
            "0",
            "0",
            "0",
            "20",
            "monthly",
            "1",
            "800",
            "20000000",
            "2000000",
            "UTC",
            "0.7",
        ]
    )
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", ask)
    models = configure_models()
    assert models["auxiliary"]["embedding"]["credential_env"] == "TEST_EMBED_ENV"
    assert models["auxiliary"]["consolidation"]["credential_env"] == "TEST_CHAT_ENV"
    assert all("key value" not in prompt.lower() for prompt in prompts)


def test_one_cli_command_installs_a_fresh_isolated_user(fake_user, record_property):
    import subprocess

    (fake_user / ".workbuddy").mkdir()
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "scope_recall.maintenance.cli",
            "setup",
            "--test-mode",
            "--worker",
            "manual",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    record_property("one_command_stdout", child.stdout)
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert "\u539f\u6587\u68c0\u7d22\u53ef\u7528" in child.stdout


def test_corrupt_selected_store_never_reports_ready(fake_user):
    home = fake_user / ".workbuddy"
    home.mkdir()
    args = parser().parse_args(["--host", "workbuddy", "--test-mode", "--worker", "manual"])
    first = apply_setup(build_plan(args))
    assert first["status"] == "ready"
    pointer = json.loads((home / "scope-recall/attachment.json").read_text(encoding="utf-8"))
    database = Path(pointer["root"]) / "memory.sqlite3"
    database.write_bytes(b"TEST corrupt database")
    result = apply_setup(build_plan(args))
    assert result["status"] != "ready", result


def test_probe_reuses_the_selected_budget_ledger(fake_user, monkeypatch):
    from scope_recall.maintenance import setup_verify
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig

    home = fake_user / ".workbuddy"
    home.mkdir()
    args = parser().parse_args(["--host", "workbuddy", "--test-mode", "--worker", "manual"])
    assert apply_setup(build_plan(args))["status"] == "ready"
    runtime_path = home / "scope-recall/runtime-config.json"
    source = json.loads(runtime_path.read_text(encoding="utf-8"))
    expected = RuntimeInstanceConfig.from_mapping(source).auxiliary.ledger_path
    original = setup_verify.run_worker
    observed = []

    def run(path, **kwargs):
        config = RuntimeInstanceConfig.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))
        observed.append(config.auxiliary.ledger_path)
        return original(path, **kwargs)

    monkeypatch.setattr(setup_verify, "run_worker", run)
    assert setup_verify.verify_memory(home, "workbuddy", source)["cross_session_recall"]
    assert observed == [expected]


def test_all_discovered_hosts_share_one_new_store(fake_user):
    for folder in (".claude", ".codex", ".hermes", ".workbuddy", ".dsh"):
        (fake_user / folder).mkdir()
    args = parser().parse_args(["--test-mode", "--worker", "manual"])
    plan = build_plan(args)
    result = apply_setup(plan)
    assert result["status"] == "ready", result
    identifiers = {item["installation"]["installation_id"] for item in result["hosts"]}
    assert len(identifiers) == 1


def test_ambiguous_owner_grants_require_a_source_in_the_plan(fake_user):
    from scope_recall.adapters.shared_entries import attach_shared_record, client_entry_record
    from scope_recall.maintenance.install_common import InstallError
    from scope_recall.maintenance.shared import init_shared

    store = fake_user / "TEST-shared"
    init_shared(root=store, agent_id="TEST-scopes", test_mode=True)
    for entry, scope in (("first", "TEST-private-first"), ("second", "TEST-private-second")):
        attach_shared_record(
            store,
            client_entry_record(
                host="codex",
                home=fake_user / entry,
                entry_id=entry,
                display_name=entry,
                attached_at="2026-11-02T00:00:00Z",
                allowed_scope_ids=["TEST-common", scope],
                writable_scope_ids=["TEST-common", scope],
                capture_scope_id="TEST-common",
            ),
            now="2026-11-02T00:00:00Z",
        )
    (fake_user / ".workbuddy").mkdir()
    arguments = ["--host", "workbuddy", "--store", str(store), "--test-mode", "--worker", "manual"]
    before = snapshot(fake_user)
    with pytest.raises(InstallError, match="--share-with"):
        build_plan(parser().parse_args(arguments))
    assert snapshot(fake_user) == before
    plan = build_plan(parser().parse_args(arguments + ["--share-with", "second"]))
    assert plan.to_dict()["hosts"][0]["scopes"]["read_scopes"] == ["TEST-common", "TEST-private-second"]
    assert apply_setup(plan)["status"] == "ready"


def test_missing_host_core_selects_a_working_client_python_or_plans_hermes_install(fake_user, monkeypatch):
    from scope_recall.maintenance import setup_packages

    candidate = fake_user / "TEST-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidate.parent.mkdir(parents=True)
    (candidate.parent.parent / "pyvenv.cfg").write_text("home = TEST-base", encoding="utf-8")
    monkeypatch.setattr(setup_packages, "probe_python_package", lambda python: {})
    monkeypatch.setattr(setup_packages.shutil, "which", lambda name: str(fake_user / "TEST-uv.exe"))
    client, command, blocker = setup_packages.choose_python("codex", candidate, explicit=False)
    assert client == Path(sys.executable) and not command and blocker is None
    hermes, command, blocker = setup_packages.choose_python("hermes", candidate, explicit=False)
    assert hermes == candidate and command[:3] == [str(fake_user / "TEST-uv.exe"), "pip", "install"]
    assert blocker is None


def test_rerun_keeps_an_existing_manual_worker_choice(fake_user):
    home = fake_user / ".workbuddy"
    home.mkdir()
    first = build_plan(parser().parse_args(["--host", "workbuddy", "--test-mode", "--worker", "manual"]))
    assert apply_setup(first)["status"] == "ready"
    plan = build_plan(parser().parse_args(["--host", "workbuddy", "--test-mode"]))
    assert plan.hosts[0]["worker"] == "manual"
    result = apply_setup(plan)
    assert result["status"] == "ready"
    assert result["hosts"][0]["worker"]["mode"] == "manual"


def test_dry_run_in_a_host_venv_plans_package_install_without_running_it(fake_user, monkeypatch):
    from scope_recall.maintenance import setup_packages

    home = fake_user / ".hermes"
    candidate = home / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"TEST never executed")
    (candidate.parent.parent / "pyvenv.cfg").write_text("home = TEST-base", encoding="utf-8")
    monkeypatch.setattr(setup_packages, "probe_python_package", lambda python: {})
    monkeypatch.setattr(setup_packages.shutil, "which", lambda name: str(fake_user / "TEST-uv.exe"))
    before = snapshot(fake_user)
    plan = build_plan(parser().parse_args(["--host", "hermes", "--test-mode", "--dry-run"]))
    assert plan.hosts[0]["package_command"]
    assert plan.hosts[0]["python"] == str(candidate)
    assert snapshot(fake_user) == before


def test_missing_core_is_installed_into_a_new_hermes_venv(fake_user, monkeypatch, record_property):
    import shutil
    import venv

    if shutil.which("uv") is None:
        pytest.skip("the existing uv helper is required for a pip-less host venv")
    home = fake_user / ".hermes"
    environment = home / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    monkeypatch.setenv("UV_CACHE_DIR", str(fake_user / "TEST-package-cache"))
    args = parser().parse_args(["--host", "hermes", "--test-mode", "--worker", "manual"])
    plan = build_plan(args)
    assert plan.hosts[0]["package_command"]
    result = apply_setup(plan)
    record_property("host_venv_setup", json.dumps(result, ensure_ascii=False))
    assert result["status"] == "ready", result
    assert Path(result["hosts"][0]["doctor"]["python_executable"]).is_relative_to(environment)
    assert build_plan(args).hosts[0]["package_command"] == []


def test_setup_finds_a_legacy_separate_entry_home_and_keeps_credential_reference(fake_user):
    from scope_recall.maintenance.install import apply_install, plan_install
    from scope_recall.maintenance.install_receipt import load_receipt
    from scope_recall.maintenance.shared import attach, init_shared

    home = fake_user / ".workbuddy"
    home.mkdir()
    entry, store = fake_user / "TEST-entry", fake_user / "TEST-shared"
    credentials = fake_user / "TEST-env-reference"
    credentials.write_text("TEST_PLACEHOLDER=synthetic\n", encoding="utf-8")
    init_shared(root=store, agent_id="TEST-legacy", test_mode=True)
    attach(host="workbuddy", instance_root=entry, root=store, entry_id="legacy-entry", display_name="TEST legacy")
    apply_install(
        plan_install(
            host="workbuddy",
            target_plugin_dir=home,
            instance_root=entry,
            project_root=None,
            agent_id="TEST-legacy",
            python_executable=Path(sys.executable),
            test_mode=True,
            env_file=credentials,
        )
    )
    args = parser().parse_args(["--host", "workbuddy", "--test-mode", "--worker", "manual"])
    plan = build_plan(args)
    assert Path(plan.hosts[0]["home"]) == entry
    result = apply_setup(plan)
    assert result["status"] == "ready", result
    assert result["hosts"][0]["doctor"]["shared_store"]["entry_id"] == "legacy-entry"
    assert Path(load_receipt(entry)["env_file"]) == credentials
    server = json.loads((home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["scope-recall"]
    assert server["args"][-2] == "--env-file" and Path(server["args"][-1]) == credentials


def test_verification_credential_references_are_temporary_and_scoped(fake_user, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.maintenance.setup_verify import temporary_credentials

    monkeypatch.delenv("TEST_EMBED_ENV", raising=False)
    monkeypatch.delenv("TEST_UNRELATED", raising=False)
    path = fake_user / "TEST-credential-reference"
    path.write_text("TEST_EMBED_ENV=synthetic\nTEST_UNRELATED=ignored\n", encoding="utf-8")
    config = SimpleNamespace(
        auxiliary=SimpleNamespace(
            embedding=SimpleNamespace(credential_env="TEST_EMBED_ENV"), consolidation=SimpleNamespace(kind="codex_cli")
        )
    )
    with temporary_credentials(config, str(path)):
        assert os.environ["TEST_EMBED_ENV"] == "synthetic"
        assert "TEST_UNRELATED" not in os.environ
    assert "TEST_EMBED_ENV" not in os.environ
