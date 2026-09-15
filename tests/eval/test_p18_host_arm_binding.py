"""Offline focused tests for P18 host/arm launch binding preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import subprocess
import zipfile

import pytest

from tests.eval.p18_host_arm_binding import (
    BASELINE_578B,
    FROZEN_HERMES_COMMIT,
    HostArmBindingError,
    _P12_RUNTIME_TEMPLATE,
    _materialize_candidate_runtime_config,
    prepare_host_arm_binding,
)
from tests.eval.p18_arm_provision import provision_public_arm_plan


ROOT = Path(__file__).resolve().parents[2]


def test_CLI_preflight_checks_actual_package_and_config_without_starting_child(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import p18_formal_evidence
    import p18_hermes_cli_transport as cli
    entry = _plan(tmp_path / "TEST-CLI-freeze", "hermes_cli_local_input_v1", "C")
    wheel = _wheel(tmp_path / "TEST-candidate.whl")
    executable, version = _exe()
    binding = _prepare(entry, host_executable=executable, host_version=version,
                       candidate_wheel=wheel, candidate_sha256=_sha(wheel))
    config_path = tmp_path / "TEST-formal.json"
    config_path.write_text("{}")
    # Public fake host/candidate identity, never usable outside this test.
    monkeypatch.setattr(cli, "verify_binding", lambda binding: None)
    monkeypatch.setattr(p18_formal_evidence, "verify_formal_run_config", lambda path:
        SimpleNamespace(formal_execution_allowed=True, details={"hermes_method_path":"TEST-method", "wheel_sha256":_sha(wheel)}))
    def forbidden(*args, **kwargs):
        raise AssertionError("zero-process preflight started a child")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    transport = cli.HermesCLITransport(binding,config_path,None)
    assert transport.owner.bridge is None and transport.owner.gateway is None
    config = json.loads(Path(binding["roots"]["config_path"]).read_bytes())
    config["agent"]["max_turns"] = 99
    Path(binding["roots"]["config_path"]).write_text(json.dumps(config))
    with pytest.raises(cli.HermesCLIError,match="CLI_launch_file_changed"):
        cli.HermesCLITransport(binding,config_path,None)
    # An explicitly rebound config must still obey the primary-call contract.
    binding["launch_contract"]["config_artifact"]["sha256"] = _sha(Path(binding["roots"]["config_path"]))
    with pytest.raises(cli.HermesCLIError,match="primary_calls"):
        cli.HermesCLITransport(binding,config_path,None)
    config["agent"]["max_turns"] = 3
    Path(binding["roots"]["config_path"]).write_text(json.dumps(config))
    binding["launch_contract"]["config_artifact"]["sha256"] = _sha(Path(binding["roots"]["config_path"]))
    (Path(binding["loader"]["module_search_path"])/"scope_recall/__init__.py").write_text("# changed")
    with pytest.raises(cli.HermesCLIError,match="installed_candidate_bytes"):
        cli.HermesCLITransport(binding,config_path,None)


@pytest.mark.parametrize("arm", ["A", "C", "D"])
def test_CLI_arm_uses_real_cli_config_and_local_audience(tmp_path, arm):
    archive = tmp_path / "PUBLIC-archive.jsonl"
    archive.write_text('{"history":[{"text":"PUBLIC source"}]}\n')
    entry = _plan(tmp_path / "TEST-CLI", "hermes_cli_local_input_v1", arm, archive=archive if arm == "D" else None)
    wheel = _wheel(tmp_path / "TEST-candidate.whl")
    executable, version = _exe()
    kwargs = {"candidate_wheel":wheel,"candidate_sha256":_sha(wheel)} if arm == "C" else {}
    result = _prepare(entry, host_executable=executable, host_version=version, **kwargs)
    config = json.loads(Path(result["roots"]["config_path"]).read_bytes())
    env = json.loads(Path(result["roots"]["environment_path"]).read_bytes())
    assert not any(key.startswith("A2A_") for key in env)
    assert config["platform_toolsets"] == {"cli":["terminal","file","memory"]}
    assert config["memory"]["memory_enabled"] == (arm == "A")
    assert config["gateway"]["platforms"]["a2a"]["enabled"] is False
    assert Path(result["roots"]["home_path"]) != Path(config["terminal"]["cwd"])
    assert "--cli" in result["launch_contract"]["argv"] and "gateway" not in result["launch_contract"]["argv"]
    assert result["fixed_host"]["runtime_python_sha256"] == _sha(Path(result["fixed_host"]["runtime_python_path"]))
    if arm == "C":
        from scope_recall.adapters.hermes.identity import bind_hermes_identity
        identity = bind_hermes_identity("TEST-actual-id", hermes_home=result["roots"]["home_path"],
            platform="cli", agent_identity="default", agent_workspace="hermes")
        assert identity.runtime_audience.capture_scope_id is not None
        assert identity.scope.chat_id == "local" and identity.scope.thread_id == "main"
    assert result["model_calls"] == 0 and not result["semantic_pass"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(root: Path, host: str, arm: str, *, baseline: Path | None = None, archive: Path | None = None) -> Path:
    entry = root / host / f"arm-{arm}"
    entry.mkdir(parents=True)
    source = {"status": "HOST_PROVIDED_AT_EXECUTION", "sha256": None}
    storage = {"archive_path": None, "archive_sha256": None}
    if baseline is not None:
        target = entry / "source" / "baseline-578b"
        target.mkdir(parents=True)
        for item in baseline.rglob("*"):
            if item.is_file():
                destination = target / item.relative_to(baseline)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.read_bytes())
        if not (target / "plugin.yaml").is_file():
            (target / "plugin.yaml").write_text("name: scope-recall\nversion: 2.0.1\n", encoding="utf-8")
        source = {"status": "PASS", "baseline_ref": BASELINE_578B, "archive_root": str(target), "source_sha256": _tree_sha(target)}
    if archive is not None:
        target = entry / "archive" / "raw-archive.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(archive.read_bytes())
        storage = {"archive_path": str(target), "archive_sha256": _sha(target)}
    plan = {
        "schema": "scope-recall.p18.arm-provision-entry.v1",
        "host_id": host,
        "arm_id": arm,
        "host": {"isolated_test_root": str(entry.resolve())},
        "arm": {"code_source": source},
        "storage": storage,
    }
    (entry / "arm-plan.json").write_text(json.dumps(plan) + "\n", encoding="utf-8")
    return entry


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _exe() -> tuple[Path, str]:
    return Path(sys.executable), "TEST-python-host-1"


def _wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("scope_recall/__init__.py", "def register(ctx): return None\n")
        archive.writestr("scope_recall-TEST.dist-info/METADATA", "Metadata-Version: 2.1\nName: scope-recall\n")
    return path


def _prepare(entry: Path, **kwargs):
    source_root = entry / "TEST-hermes-source"
    if entry.parts[-2] in {"hermes_a2a", "hermes_cli_local_input_v1"}:
        source_root.mkdir(exist_ok=True)
        (source_root / "pyproject.toml").write_text("frozen Hermes TEST source", encoding="utf-8")
        kwargs.update(host_source_root=source_root, host_source_commit=FROZEN_HERMES_COMMIT)
    return prepare_host_arm_binding(entry, **kwargs)


def test_candidate_binding_creates_isolated_launch_contract_without_starting_host(tmp_path: Path):
    entry = _plan(tmp_path / "TEST-P18", "hermes_a2a", "C")
    wheel = _wheel(tmp_path / "TEST-candidate.whl")
    executable, version = _exe()

    receipt = _prepare(
        entry,
        host_executable=executable,
        host_version=version,
        candidate_wheel=wheel,
        candidate_sha256=_sha(wheel),
    )
    assert receipt["status"] == "PREPARED"
    assert receipt["formal_execution_started"] is False
    assert receipt["host_started"] is False
    assert receipt["network_calls"] == receipt["model_calls"] == 0
    assert receipt["source"]["mode"] == "FROZEN_CANDIDATE_WHEEL"
    assert receipt["source"]["candidate_wheel"]["sha256"] == _sha(wheel)
    assert receipt["semantic_pass"] is False
    assert Path(receipt["roots"]["home_path"]).is_dir()
    assert Path(receipt["roots"]["workspace_path"]).is_dir()
    assert Path(receipt["roots"]["workspace_path"]) != Path(receipt["roots"]["home_path"])
    assert Path(receipt["roots"]["state_path"]).is_dir()
    assert Path(receipt["roots"]["database_path"]).parent != Path(receipt["roots"]["state_path"])
    assert Path(receipt["roots"]["environment_path"]).is_file()
    assert Path(receipt["roots"]["runtime_config_path"]).is_file()
    hermes_config = json.loads(Path(receipt["roots"]["config_path"]).read_text(encoding="utf-8"))
    assert hermes_config["terminal"]["cwd"] == receipt["roots"]["workspace_path"]
    assert receipt["launch_contract"]["working_directory"] == receipt["roots"]["workspace_path"]
    assert hermes_config["terminal"]["cwd"] != receipt["roots"]["home_path"]
    environment = json.loads(Path(receipt["roots"]["environment_path"]).read_text(encoding="utf-8"))
    assert environment["HERMES_HOME"] == receipt["roots"]["home_path"]


def test_binding_accepts_an_existing_public_arm_provision_entry(tmp_path: Path):
    provision_root = tmp_path / "TEST-existing-provision"
    provision_public_arm_plan(provision_root, repo_root=ROOT, hosts=("hermes_a2a",), arms=("C",))
    entry = provision_root / "hermes_a2a" / "arm-C"
    wheel = _wheel(tmp_path / "TEST-frozen-candidate.whl")
    executable, version = _exe()

    receipt = _prepare(
        entry,
        host_executable=executable,
        host_version=version,
        candidate_wheel=wheel,
        candidate_sha256=_sha(wheel),
    )
    assert receipt["status"] == "PREPARED"
    assert receipt["source"]["mode"] == "FROZEN_CANDIDATE_WHEEL"
    assert receipt["roots"]["home_path"].startswith(str(entry / "host-binding"))
    hermes_config = json.loads(Path(receipt["roots"]["config_path"]).read_text(encoding="utf-8"))
    environment = json.loads(Path(receipt["roots"]["environment_path"]).read_text(encoding="utf-8"))
    assert Path(receipt["roots"]["config_path"]).name == "config.yaml"
    assert hermes_config["plugins"]["enabled"] == ["platforms/a2a", "scope_recall"]
    assert receipt["launch_contract"]["argv"][-2:] == ["gateway", "run"]
    assert environment["HERMES_HOME"] == receipt["roots"]["home_path"]
    assert "TOKEN" not in json.dumps(environment)
    assert "PYTHONPATH" in environment


def test_hermes_archive_and_simple_search_have_real_loader_argv(tmp_path: Path):
    archive_root = tmp_path / "TEST-baseline-578b"
    archive_root.mkdir(parents=True)
    (archive_root / "__init__.py").write_text("def register(ctx): return None\n", encoding="utf-8")
    (archive_root / "plugin.yaml").write_text("name: scope-recall\nversion: 2.0.1\n", encoding="utf-8")
    executable, version = _exe()
    baseline_entry = _plan(tmp_path / "TEST-P18-B", "hermes_a2a", "B", baseline=archive_root)
    baseline = _prepare(baseline_entry, host_executable=executable, host_version=version)
    assert baseline["loader"]["entrypoint"] == "__init__.py:register"
    assert Path(baseline["loader"]["plugin_manifest"]["path"]).is_file()

    archive = tmp_path / "TEST-raw-archive.jsonl"
    archive.write_text('{"history": [], "query": {"text": "synthetic"}}\n', encoding="utf-8")
    simple_entry = _plan(tmp_path / "TEST-P18-D", "hermes_a2a", "D", archive=archive)
    simple = _prepare(simple_entry, host_executable=executable, host_version=version)
    assert simple["loader"]["registration"] == "Hermes plugin loader"
    assert simple["loader"]["provider"] == "ArchiveSimpleSearchProvider"
    assert simple["launch_contract"]["argv"][-2:] == ["gateway", "run"]
    assert Path(simple["loader"]["plugin_manifest"]["path"]).is_file()
    compile(
        (Path(simple["loader"]["plugin_directory"]["path"]) / "__init__.py").read_text(encoding="utf-8"),
        "archive-simple-search-plugin",
        "exec",
    )


def test_CLI_B_does_not_create_candidate_schema_in_legacy_database(tmp_path):
    archive = tmp_path/"TEST-baseline"
    archive.mkdir()
    (archive/"__init__.py").write_text("def register(ctx): return None\n")
    entry = _plan(tmp_path/"TEST-B", "hermes_cli_local_input_v1", "B", baseline=archive)
    executable,version = _exe()
    result = _prepare(entry,host_executable=executable,host_version=version)
    assert result["installation_manifest"] is None
    assert Path(result["roots"]["database_path"]).name == "memory.sqlite3"
    assert not Path(result["roots"]["database_path"]).exists()
    assert result["source"]["mode"] == "FROZEN_BASELINE_578B"


def test_codex_does_not_receive_hermes_home_and_unimplemented_arms_are_explicit(tmp_path: Path):
    executable, version = _exe()
    a_entry = _plan(tmp_path / "TEST-P18", "codex_windows_desktop", "A")
    a = _prepare(a_entry, host_executable=executable, host_version=version)
    assert a["status"] == "PREPARED"
    assert "HERMES_HOME" not in json.dumps(a)
    assert a["launch_contract"]["argv"][-1] == "app-server"
    assert a["loader"]["scope_recall_hooks"] == []

    archive = tmp_path / "TEST-codex-d-archive.jsonl"
    archive.write_text("{}\n", encoding="utf-8")
    d_entry = _plan(tmp_path / "TEST-P18-D", "codex_windows_desktop", "D", archive=archive)
    d = _prepare(d_entry, host_executable=executable, host_version=version)
    assert d["status"] == "PREPARED"
    assert "simple search" in d["reason"]
    assert "HERMES_HOME" not in json.dumps(d)
    assert Path(d["loader"]["hooks"]["path"]).is_file()
    assert Path(d["loader"]["plugin_manifest"]["path"]).is_file()
    hook_script = Path(d["loader"]["hook_script"]["path"])
    assert hook_script.is_file()
    compile(hook_script.read_text(encoding="utf-8"), "simple-search-hook", "exec")
    assert "scope_recall" not in hook_script.read_text(encoding="utf-8").lower()


def test_codex_candidate_writes_actual_install_config_and_native_hooks_without_claiming_runtime(tmp_path: Path):
    executable, version = _exe()
    entry = _plan(tmp_path / "TEST-P18", "codex_windows_desktop", "C")
    wheel = _wheel(tmp_path / "TEST-codex-candidate.whl")
    receipt = _prepare(
        entry,
        host_executable=executable,
        host_version=version,
        candidate_wheel=wheel,
        candidate_sha256=_sha(wheel),
    )
    assert receipt["status"] == "PREPARED", receipt.get("reason")
    assert "HERMES_HOME" not in json.dumps(receipt)
    assert Path(receipt["roots"]["installation_config_path"]).name == "codex-installation.json"
    assert Path(receipt["roots"]["runtime_config_path"]).is_file()
    runtime = json.loads(Path(receipt["roots"]["runtime_config_path"]).read_text(encoding="utf-8"))
    assert set((runtime["auxiliary"] or {})) >= {"budget", "embedding", "consolidation"}
    assert set((runtime["vector"] or {})) >= {"storage_dir", "table_name", "dimensions", "metric"}
    environment = json.loads(Path(receipt["roots"]["environment_path"]).read_text(encoding="utf-8"))
    assert environment["CODEX_HOME"] == receipt["roots"]["home_path"]
    hooks = Path(receipt["loader"]["hooks"]["path"])
    assert hooks.is_file()
    assert "scope_recall.adapters.codex.hook_entry" in hooks.read_text(encoding="utf-8")
    hook_config = json.loads(hooks.read_text(encoding="utf-8"))
    hook_commands = [
        hook["command"]
        for event in hook_config["hooks"].values()
        for group in event
        for hook in group["hooks"]
    ]
    assert any("TEST-FINAL-RUNTIME-ENV\\Scripts\\python.exe" in command for command in hook_commands)
    assert receipt["launch_contract"]["requires_actual_desktop_ui"] is True
    assert receipt["semantic_pass"] is False


def test_codex_C_uses_installed_maintenance_namespace(
    tmp_path: Path, monkeypatch,
):
    """The installed candidate exposes ``scope_recall.maintenance`` only."""
    import builtins

    real_import = builtins.__import__

    def installed_only_import(name, *args, **kwargs):
        if name == "maintenance" or name.startswith("maintenance."):
            raise ImportError("source-tree maintenance alias is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", installed_only_import)
    executable, version = _exe()
    entry = _plan(tmp_path / "TEST-C", "codex_windows_desktop", "C")
    wheel = _wheel(tmp_path / "TEST-C.whl")
    receipt = _prepare(
        entry,
        host_executable=executable,
        host_version=version,
        candidate_wheel=wheel,
        candidate_sha256=_sha(wheel),
    )

    assert receipt["status"] == "PREPARED", receipt.get("reason")
    assert Path(receipt["loader"]["candidate_install"]["path"]).is_dir()


def test_hermes_arms_share_normal_tools_and_only_vary_memory_provider(tmp_path: Path):
    from tests.eval.p18_host_arm_binding import _hermes_config

    configs = [_hermes_config(tmp_path / f"TEST-home-{arm}", arm) for arm in ("A", "B", "C", "D")]
    normal = [set(config["platform_toolsets"]["a2a"]) - {"memory"} for config in configs]

    assert normal == [{"terminal", "file"}] * 4
    assert all("memory" in config["platform_toolsets"]["a2a"] for config in configs)


def test_codex_candidate_runtime_parses_with_binding_owned_vector_paths(tmp_path: Path):
    from scope_recall.core.recall_policy import SPACE_ID
    from scope_recall.runtime.instance import RuntimeInstanceConfig

    entry = _plan(tmp_path / "TEST-P18", "codex_windows_desktop", "C")
    wheel = _wheel(tmp_path / "TEST-candidate-runtime.whl")
    executable, version = _exe()
    receipt = _prepare(
        entry,
        host_executable=executable,
        host_version=version,
        candidate_wheel=wheel,
        candidate_sha256=_sha(wheel),
    )
    raw = json.loads(Path(receipt["roots"]["runtime_config_path"]).read_text(encoding="utf-8"))
    parsed = RuntimeInstanceConfig.from_mapping(raw)
    data_directory = Path(raw["binding"]["data_directory"]).resolve()

    assert parsed.binding.data_directory == data_directory
    assert Path(raw["auxiliary"]["installation_dir"]).resolve() == data_directory
    assert Path(raw["auxiliary"]["ledger_path"]).resolve().parent != data_directory
    assert Path(raw["vector"]["storage_dir"]).resolve() == data_directory / "vectors" / SPACE_ID


def test_external_auxiliary_preserves_explicit_shared_ledger_and_missing_ledger_fails_closed(tmp_path: Path):
    template = json.loads(_P12_RUNTIME_TEMPLATE.read_text(encoding="utf-8"))
    installation = {
        "agent_id": "TEST-P18-codex-C",
        "installation_id": "TEST-installation",
        "data_directory": str((tmp_path / "TEST-unit-data").resolve()),
        "scope_ids": ["audience:owner_private:TEST-P18"],
        "test_mode": True,
    }
    expected_ledger = str(Path(template["auxiliary"]["ledger_path"]).resolve())
    materialized = _materialize_candidate_runtime_config(
        tmp_path / "TEST-unit-home",
        installation,
        session_id="TEST-session",
        base_template=_P12_RUNTIME_TEMPLATE,
    )
    assert materialized["auxiliary"]["ledger_path"] == expected_ledger
    assert (
        materialized["auxiliary"]["consolidation"]["headers"]["x-opencode-session"]
        == "scope-recall-test-p18-TEST-session"
    )
    assert Path(materialized["auxiliary"]["installation_dir"]) == Path(installation["data_directory"])
    assert Path(materialized["vector"]["storage_dir"]).parent.parent == Path(installation["data_directory"])

    missing_ledger = dict(template)
    missing_ledger["auxiliary"] = dict(template["auxiliary"])
    missing_ledger["auxiliary"].pop("ledger_path", None)
    missing_path = tmp_path / "TEST-template-without-ledger.json"
    missing_path.write_text(json.dumps(missing_ledger) + "\n", encoding="utf-8")
    with pytest.raises(HostArmBindingError, match="explicit shared ledger_path"):
        _materialize_candidate_runtime_config(
            tmp_path / "TEST-unit-home-missing-ledger",
            installation,
            session_id="TEST-session",
            base_template=missing_path,
        )


def test_frozen_hermes_public_provider_loader_loads_real_wrapper_offline(tmp_path: Path):
    """Exercise the frozen loader, wrapper, and local adapter without a host/network."""
    hermes_source = ROOT.parent.parent / "TEST-Hermes-runtime-v1" / "hermes-source-79445"
    home = tmp_path / "TEST-hermes-loader-home"
    plugin = home / "plugins" / "scope_recall"
    plugin.mkdir(parents=True)
    shutil.copyfile(ROOT / "distribution" / "hermes" / "__init__.py", plugin / "__init__.py")
    shutil.copyfile(ROOT / "distribution" / "hermes" / "plugin.yaml", plugin / "plugin.yaml")
    loader_script = (
        "import json, sys; "
        f"sys.path[:0]=[{str(hermes_source)!r}, {str(ROOT)!r}]; "
        "from plugins.memory import load_memory_provider; "
        "provider=load_memory_provider('scope_recall', register_skills=False); "
        "print(json.dumps({'name': provider.name, 'tools': len(provider.get_tool_schemas())}))"
    )
    environment = {"HERMES_HOME": str(home), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", loader_script],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["name"] == "scope-recall"
    assert receipt["tools"] >= 1


def test_native_arm_explicitly_disables_scope_recall(tmp_path: Path):
    entry = _plan(tmp_path / "TEST-P18-NATIVE-FIXTURE", "hermes_a2a", "A")
    executable, version = _exe()
    receipt = _prepare(entry, host_executable=executable, host_version=version)
    assert receipt["source"] == {
        "mode": "HOST_NATIVE_MEMORY",
        "scope_recall_enabled": False,
        "scope_recall_source": None,
        "host_native_memory_enabled": True,
    }
    config = json.loads(Path(receipt["roots"]["runtime_config_path"]).read_text(encoding="utf-8"))
    assert config["scope_recall_enabled"] is False
    hermes_config = json.loads(Path(receipt["roots"]["config_path"]).read_text(encoding="utf-8"))
    assert hermes_config["memory"]["memory_enabled"] is True
    assert "scope_recall" not in hermes_config["plugins"]["enabled"]


def test_baseline_is_fixed_archive_for_Hermes_and_Codex_is_unsupported(tmp_path: Path):
    archive_root = tmp_path / "TEST-baseline-578b"
    archive_root.mkdir(parents=True)
    (archive_root / "pyproject.toml").write_text("synthetic baseline", encoding="utf-8")
    entry = _plan(tmp_path / "TEST-P18", "hermes_a2a", "B", baseline=archive_root)
    executable, version = _exe()
    receipt = _prepare(entry, host_executable=executable, host_version=version)
    assert receipt["status"] == "PREPARED"
    assert receipt["source"]["baseline_ref"] == BASELINE_578B
    assert receipt["source"]["candidate_wheel"] is None
    assert Path(receipt["source"]["archive"]["path"]).is_dir()

    codex_entry = _plan(tmp_path / "TEST-P18-codex", "codex_windows_desktop", "B", baseline=archive_root)
    unsupported = _prepare(codex_entry, host_executable=executable, host_version=version)
    assert unsupported["status"] == "UNSUPPORTED"
    assert unsupported["source"]["mode"] == "UNSUPPORTED"
    assert unsupported["launch_contract"]["cli_substitution_forbidden"] is True


def test_hermes_manifest_matches_frozen_unthreaded_a2a_context_exactly(tmp_path: Path):
    entry = _plan(tmp_path / "TEST-P18-identity", "hermes_a2a", "B", baseline=tmp_path / "TEST-baseline")
    executable, version = _exe()
    context_id = "TEST-context-p18-identity"
    receipt = _prepare(entry, host_executable=executable, host_version=version, host_context_id=context_id)
    assert receipt["host_context_id"] == context_id
    assert receipt["launch_contract"]["host_context_id"] == context_id
    installation = receipt["installation_manifest"]
    manifest = json.loads(Path(installation["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["agent_id"] == "default"
    exact = [item for item in manifest["audiences"] if item["kind"] == "conversation"]
    assert exact == [{
        "agent_workspace": "hermes",
        "allowed_scope_ids": exact[0]["allowed_scope_ids"],
        "capture_scope_id": exact[0]["capture_scope_id"],
        "chat_id": context_id,
        "chat_type": "dm",
        "kind": "conversation",
        "platform": "a2a",
        "thread_id": "",
    }]
    assert all(item["thread_id"] != "main" for item in exact)


def test_archive_simple_search_uses_plan_archive_hash_and_no_candidate(tmp_path: Path):
    archive = tmp_path / "TEST-raw-archive.jsonl"
    archive.write_text('{"history": [], "query": {"text": "synthetic"}}\n', encoding="utf-8")
    entry = _plan(tmp_path / "TEST-P18", "hermes_a2a", "D", archive=archive)
    executable, version = _exe()
    receipt = _prepare(entry, host_executable=executable, host_version=version)
    assert receipt["source"]["mode"] == "ARCHIVE_SIMPLE_SEARCH"
    assert receipt["source"]["simple_search_only"] is True
    assert receipt["source"]["archive"]["sha256"] == _sha(archive)
    assert receipt["source"]["candidate_wheel"] is None


def test_candidate_hash_mismatch_and_missing_candidate_fail_closed(tmp_path: Path):
    entry = _plan(tmp_path / "TEST-P18", "hermes_a2a", "C")
    wheel = _wheel(tmp_path / "TEST-candidate.whl")
    executable, version = _exe()
    with pytest.raises(HostArmBindingError, match="digest mismatch"):
        _prepare(entry, host_executable=executable, host_version=version, candidate_wheel=wheel, candidate_sha256="0" * 64)

    other = _plan(tmp_path / "TEST-P18-other", "hermes_a2a", "C")
    with pytest.raises(HostArmBindingError, match="explicit frozen candidate"):
        _prepare(other, host_executable=executable, host_version=version)


def test_binding_roots_are_per_entry_and_overwrite_is_forbidden(tmp_path: Path):
    first = _plan(tmp_path / "TEST-P18", "hermes_a2a", "A")
    archive = tmp_path / "TEST-archive.jsonl"
    archive.write_text("{}\n", encoding="utf-8")
    second = _plan(tmp_path / "TEST-P18", "hermes_a2a", "D", archive=archive)
    executable, version = _exe()
    first_receipt = _prepare(first, host_executable=executable, host_version=version)
    second_receipt = _prepare(second, host_executable=executable, host_version=version)
    assert first_receipt["roots"]["state_path"] != second_receipt["roots"]["state_path"]
    with pytest.raises(HostArmBindingError, match="already exists"):
        _prepare(first, host_executable=executable, host_version=version)


def test_non_test_or_production_roots_are_rejected(tmp_path: Path):
    executable, version = _exe()
    with pytest.raises(HostArmBindingError, match="F:\\\\Agents"):
        prepare_host_arm_binding(r"F:\Agents\P18-host", host_executable=executable, host_version=version)
