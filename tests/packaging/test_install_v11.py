"""Packaging tests for the bounded v1.1 installer scaffold."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scope_recall._version import __version__


def _assert_hook_uses_current_interpreter(command: str, executable: Path) -> None:
    from v11_guard import _windows_command_tokens

    tokens = _windows_command_tokens(command) if os.name == "nt" else shlex.split(command)
    invoked = Path(tokens[0].strip("'\""))
    assert invoked.samefile(executable)
    assert tokens[1:4] == ["-I", "-B", "-m"]
    assert tokens[4] == "scope_recall.adapters.codex.hook_entry"

# Bind local maintenance/ to scope_recall.maintenance until root wires package metadata.
if importlib.util.find_spec("scope_recall.maintenance") is None:
    import maintenance as _maintenance

    sys.modules["scope_recall.maintenance"] = _maintenance
    for _name in ("install", "doctor", "cli"):
        sys.modules[f"scope_recall.maintenance.{_name}"] = importlib.import_module(f"maintenance.{_name}")

from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.install import (
    InstallError,
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(paths: list[Path]) -> dict[str, str]:
    return {str(path): _sha256_file(path) for path in paths if path.is_file()}


def _install_paths(tmp_path: Path, *, host: str) -> tuple[Path, Path, Path]:
    instance_root = (tmp_path / "instance").resolve()
    plugin_dir = (tmp_path / "plugins" / "scope-recall").resolve()
    project_root = (tmp_path / "workspace").resolve()
    plugin_dir.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    return instance_root, plugin_dir, project_root


def test_relative_paths_rejected(tmp_path):
    plugin_dir = (tmp_path / "plugins" / "scope-recall").resolve()
    project_root = (tmp_path / "workspace").resolve()
    plugin_dir.mkdir(parents=True)
    project_root.mkdir()

    with pytest.raises(InstallError, match="must be absolute"):
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root="relative/instance",
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )


@pytest.mark.parametrize("host", ["hermes", "codex"])
def test_agent_setup_skill_discovery_ownership_and_uninstall(tmp_path, host):
    instance, plugin, project = _install_paths(tmp_path, host=host)
    skill = (instance if host == "hermes" else plugin) / "skills" / "scope-recall-setup" / "SKILL.md"
    options = dict(host=host, target_plugin_dir=plugin, instance_root=instance,
                   project_root=project, agent_id="TEST-setup-skill", python_executable=Path(sys.executable))
    apply_install(plan_install(**options))
    assert "scope-recall setup" in skill.read_text(encoding="utf-8")
    receipt = json.loads((instance / ".scope-recall-install-receipt.json").read_text())
    assert any(Path(item["path"]) == skill for item in receipt["files"])
    if host == "codex":
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
        assert manifest["version"] == __version__.replace("rc", "-rc.")
    apply_install(plan_install(**options))
    apply_uninstall(plan_uninstall(instance_root=instance, target_plugin_dir=plugin))
    assert not skill.exists()


def test_hermes_existing_user_skill_is_not_overwritten(tmp_path):
    instance, plugin, project = _install_paths(tmp_path, host="hermes")
    skill = instance / "skills" / "scope-recall-setup" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("TEST-user-owned skill")
    plan = plan_install(host="hermes", target_plugin_dir=plugin, instance_root=instance,
                        project_root=project, agent_id="TEST-setup-skill", python_executable=Path(sys.executable))
    with pytest.raises(InstallError):
        apply_install(plan)
    assert skill.read_text() == "TEST-user-owned skill"


def test_no_receipt_owned_name_collision(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    collision = plugin_dir / "hooks" / "hooks.json"
    collision.parent.mkdir(parents=True)
    collision.write_text('{"hooks": {}}', encoding="utf-8")

    plan = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P14-agent",
        python_executable=Path(sys.executable),
    )
    assert any("no-receipt collision" in item for item in plan.conflicts)
    with pytest.raises(InstallError):
        apply_install(plan)


def test_receipt_path_tampering_rejected(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    receipt_path = instance_root / ".scope-recall-install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["files"][0]["path"] = str((tmp_path / "outside" / "hooks.json").resolve())
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    with pytest.raises(InstallError):
        plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir)


def test_edited_uninstall_retains_wrapper(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    hooks = plugin_dir / "hooks" / "hooks.json"
    hooks.write_text(hooks.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    uninstall_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir)
    assert any("hooks.json" in item for item in uninstall_plan.edited_files)
    result = apply_uninstall(uninstall_plan)
    assert hooks.is_file()
    assert result.edited_files
    assert (instance_root / ".scope-recall-install-receipt.json").is_file()


def test_stale_plan_protection(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    stale = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P14-agent",
        python_executable=Path(sys.executable),
    )
    (plugin_dir / "foreign.txt").write_text("block", encoding="utf-8")
    with pytest.raises(InstallError):
        apply_install(stale)


def test_hook_command_quotes_spaces(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    spaced_python = tmp_path / "bin space" / ("python.cmd" if os.name == "nt" else "python.sh")
    spaced_python.parent.mkdir(parents=True)
    if os.name == "nt":
        spaced_python.write_text(f'@"{sys.executable}" %*\n', encoding="utf-8")
    else:
        spaced_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        spaced_python.chmod(0o755)

    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=spaced_python.resolve(),
        )
    )
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert "bin space" in hook["command"]
    launcher = Path(hook["commandWindows"])
    assert launcher.name == "scope-recall-hook.cmd"
    windows_script = launcher.read_text(encoding="utf-8")
    assert "bin space" in windows_script
    assert "-I" in hook["command"]
    assert "-B" in hook["command"]
    assert "-m scope_recall.adapters.codex.hook_entry" in hook["command"]


def test_preview_creates_nothing_and_codex_install_doctor_uninstall(tmp_path):
    instance_root = (tmp_path / "instance").resolve()
    plugin_dir = (tmp_path / "plugins" / "scope-recall").resolve()
    project_root = (tmp_path / "workspace").resolve()
    foreign = plugin_dir / "foreign.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("leave-me", encoding="utf-8")
    project_root.mkdir()

    preview = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P14-agent",
        python_executable=Path(sys.executable),
    )
    assert preview.conflicts
    assert not instance_root.exists()
    assert not (plugin_dir / "hooks").exists()

    foreign.unlink()
    preview = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P14-agent",
        python_executable=Path(sys.executable),
    )
    assert not preview.conflicts
    assert not instance_root.exists()

    result = apply_install(preview)
    assert result.host_registration_pending is True
    assert result.hook_trust_pending is True
    assert result.full_mode_unverified is True
    assert (instance_root / "codex-installation.json").is_file()
    assert (instance_root / "data" / "memory.sqlite3").is_file()
    assert (plugin_dir / "hooks" / "hooks.json").is_file()
    assert (plugin_dir / ".mcp.json").is_file()
    plugin_json = json.loads((plugin_dir / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin_json["name"] == plugin_dir.name
    assert plugin_json["version"] == __version__.replace(".dev", "-dev.", 1).replace("rc", "-rc.", 1)
    assert "skills" not in plugin_json
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    _assert_hook_uses_current_interpreter(command, Path(sys.executable))
    assert str(instance_root / "codex-installation.json") in command
    assert "unsupported" not in json.dumps(hooks)

    tracked = [
        instance_root / "codex-installation.json",
        instance_root / "data" / "memory.sqlite3",
        plugin_dir / "hooks" / "hooks.json",
    ]
    before = _snapshot(tracked)
    report = run_doctor(host="codex", instance_root=instance_root, python_executable=Path(sys.executable))
    after = _snapshot(tracked)
    assert before == after
    assert report.binding_ok is True
    assert report.database_present is True
    assert report.schema_version is not None
    assert report.host_registration_status == "pending"
    assert report.hook_trust_status == "pending"
    if report.package_ok:
        assert report.package_source in {"development", "installed"}
    elif report.package_version is not None:
        assert report.package_source in {"development", "installed"}
        assert any(gap in report.capability_gaps for gap in
                   {"python_package_version_mismatch", "python_package_metadata_mismatch"})
    else:
        assert report.package_source is None
        assert "python_package_missing" in report.capability_gaps

    foreign.write_text("still-here", encoding="utf-8")
    memory_hash = _sha256_file(instance_root / "data" / "memory.sqlite3")
    uninstall_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir)
    uninstall = apply_uninstall(uninstall_plan)
    assert uninstall.memory_retained is True
    assert uninstall.purged is False
    assert (instance_root / "data" / "memory.sqlite3").is_file()
    assert _sha256_file(instance_root / "data" / "memory.sqlite3") == memory_hash
    assert (instance_root / ".scope-recall-install-receipt.json").is_file()
    assert foreign.is_file()
    assert not (plugin_dir / "hooks" / "hooks.json").is_file()


def test_hermes_fresh_install_and_doctor(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    result = apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    assert result.host_registration_pending is True
    assert (instance_root / "scope-recall" / "installation.json").is_file()
    assert (instance_root / "scope-recall" / "memory.sqlite3").is_file()
    init_source = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    assert "register_memory_provider" in init_source
    manifest = json.loads((instance_root / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    assert {row["agent_workspace"] for row in manifest["audiences"]} == {"hermes"}
    receipt = json.loads((instance_root / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["agent_workspace"] == "hermes"

    from scope_recall.adapters.hermes.identity import bind_hermes_identity

    identity = bind_hermes_identity(
        "TEST-session",
        hermes_home=str(instance_root),
        platform="cli",
        agent_identity="TEST-P14-agent",
        agent_workspace="hermes",
        user_id="local",
        agent_context="primary",
    )
    assert identity.runtime_audience.capability_gaps == ()
    assert identity.runtime_audience.allowed_scope_ids

    report = run_doctor(host="hermes", instance_root=instance_root, python_executable=Path(sys.executable))
    assert report.binding_ok is True
    assert report.database_present is True
    # The installer does not manufacture or select a host config. Current
    # doctor reports that concrete gap instead of the older constant pending.
    assert report.host_registration_status == "host_config_missing"
    assert report.hook_trust_status == "unknown"


@pytest.mark.parametrize("host", ["hermes", "codex"])
def test_install_mode_defaults_to_production_and_test_mode_is_explicit(tmp_path, host):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path / "production", host=host)
    production_plan = plan_install(
        host=host,
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P19-production",
        python_executable=Path(sys.executable),
    )
    assert production_plan.test_mode is False
    assert production_plan.to_dict()["test_mode"] is False
    apply_install(production_plan)

    config_path = (
        instance_root / "scope-recall" / "installation.json"
        if host == "hermes"
        else instance_root / "codex-installation.json"
    )
    assert json.loads(config_path.read_text(encoding="utf-8"))["test_mode"] is False

    test_instance, test_plugin, test_project = _install_paths(tmp_path / "test", host=host)
    test_plan = plan_install(
        host=host,
        target_plugin_dir=test_plugin,
        instance_root=test_instance,
        project_root=test_project,
        agent_id="TEST-P19-test",
        python_executable=Path(sys.executable),
        test_mode=True,
    )
    assert test_plan.test_mode is True
    apply_install(test_plan)
    test_config_path = (
        test_instance / "scope-recall" / "installation.json"
        if host == "hermes"
        else test_instance / "codex-installation.json"
    )
    assert json.loads(test_config_path.read_text(encoding="utf-8"))["test_mode"] is True


def test_maintenance_cli_requires_explicit_test_mode(tmp_path, capsys):
    from scope_recall.maintenance import cli as maintenance_cli

    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    base = [
        "plan-install",
        "--host",
        "codex",
        "--target-plugin-dir",
        str(plugin_dir),
        "--instance-root",
        str(instance_root),
        "--project-root",
        str(project_root),
        "--agent-id",
        "TEST-P19-cli",
        "--python",
        str(Path(sys.executable)),
    ]
    assert maintenance_cli.main(base) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["test_mode"] is False
    assert planned["agent_workspace"] == ""
    assert maintenance_cli.main([*base, "--test-mode"]) == 0
    assert json.loads(capsys.readouterr().out)["test_mode"] is True


@pytest.mark.parametrize(
    ("host", "existing_mode", "requested_mode"),
    [
        ("hermes", False, True),
        ("hermes", True, False),
        ("codex", False, True),
        ("codex", True, False),
    ],
)
def test_reuse_rejects_mode_mismatch_without_writes(
    tmp_path, host, existing_mode, requested_mode
):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path / "existing", host=host)
    initial = plan_install(
        host=host,
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P19-reuse",
        python_executable=Path(sys.executable),
        test_mode=existing_mode,
    )
    apply_install(initial)
    config_path = (
        instance_root / "scope-recall" / "installation.json"
        if host == "hermes"
        else instance_root / "codex-installation.json"
    )
    data_path = (
        instance_root / "scope-recall" / "memory.sqlite3"
        if host == "hermes"
        else instance_root / "data" / "memory.sqlite3"
    )
    tracked = [config_path, data_path]
    if host == "codex":
        tracked.append(plugin_dir / "hooks" / "hooks.json")
    else:
        tracked.append(plugin_dir / "__init__.py")
    tracked.append(instance_root / ".scope-recall-install-receipt.json")
    before = _snapshot(tracked)

    mismatch = plan_install(
        host=host,
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P19-reuse",
        python_executable=Path(sys.executable),
        test_mode=requested_mode,
    )
    assert any("test_mode mismatch" in item for item in mismatch.conflicts)
    assert _snapshot(tracked) == before
    with pytest.raises(InstallError, match="test_mode mismatch"):
        apply_install(mismatch)
    assert _snapshot(tracked) == before

    same_mode = plan_install(
        host=host,
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P19-reuse",
        python_executable=Path(sys.executable),
        test_mode=existing_mode,
    )
    assert not same_mode.conflicts
    apply_install(same_mode)


def test_conflict_detection_for_unrelated_plugin_file(tmp_path):
    plugin_dir = (tmp_path / "plugin").resolve()
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "other-host.txt").write_text("x", encoding="utf-8")
    plan = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=(tmp_path / "instance").resolve(),
        project_root=(tmp_path / "workspace").resolve(),
        agent_id="TEST-P14-agent",
        python_executable=Path(sys.executable),
    )
    assert plan.conflicts
    with pytest.raises(InstallError):
        apply_install(plan)


def test_explicit_purge_removes_owned_data_and_keeps_host_files(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    host_file = instance_root / "host-session.sqlite3"
    host_file.parent.mkdir(parents=True, exist_ok=True)
    host_file.write_text("host-owned", encoding="utf-8")
    purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
    assert purge_plan.conflicts == []
    assert purge_plan.purge_allowed is True
    result = apply_uninstall(purge_plan, purge=True)
    assert result.purged is True
    assert result.memory_retained is False
    assert not (instance_root / "data" / "memory.sqlite3").exists()
    assert not (instance_root / "codex-installation.json").exists()
    assert host_file.is_file()


def test_purge_rejects_unknown_retained_file(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    retained = instance_root / "data" / "retained"
    retained.mkdir()
    (retained / "unbound").write_bytes(b"foreign")
    purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
    assert any("unknown_retained_file" in item for item in purge_plan.conflicts)
    with pytest.raises(InstallError, match="purge_refused"):
        apply_uninstall(purge_plan, purge=True)
    assert (instance_root / "data" / "memory.sqlite3").is_file()


def test_purge_removes_owned_retained_blob(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    config = json.loads((instance_root / "codex-installation.json").read_text(encoding="utf-8"))
    data_dir = instance_root / "data"
    blob = b"owned attachment"
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    retained = data_dir / "retained"
    retained.mkdir()
    (retained / digest).write_bytes(blob)
    conn = sqlite3.connect(data_dir / "memory.sqlite3")
    scope_id = conn.execute("SELECT scope_id FROM instance_scopes LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO artifacts(artifact_id,scope_id,project_id,branch_id,current_revision,read_blocked,suppressed) VALUES (?,?,?,?,1,0,0)",
        ("owned-artifact", scope_id, None, None),
    )
    conn.execute(
        "INSERT INTO artifact_versions(artifact_id,revision,label,media_type,sha256,size_bytes,retention_state,relative_path,blob_json,description_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "owned-artifact",
            1,
            "owned",
            "text/plain",
            digest,
            len(blob),
            "retained_artifact",
            f"retained/{digest}",
            json.dumps(
                {
                    "sha256": digest,
                    "size_bytes": len(blob),
                    "media_type": "text/plain",
                    "relative_path": f"retained/{digest}",
                    "installation_id": config["installation_id"],
                    "agent_id": config["agent_id"],
                }
            ),
            "[]",
            "2026-09-06T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
    assert purge_plan.conflicts == []
    result = apply_uninstall(purge_plan, purge=True)
    assert result.purged is True
    assert not (retained / digest).exists()


def test_purge_rejects_reparse_retained_directory(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    outside = tmp_path / "outside-retained"
    outside.mkdir()
    retained = instance_root / "data" / "retained"
    try:
        retained.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("test environment does not permit directory symlinks")
        import _winapi
        _winapi.CreateJunction(str(outside), str(retained))
    purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
    assert any("symlink or reparse" in item for item in purge_plan.conflicts)
    assert (instance_root / "data" / "memory.sqlite3").is_file()


def test_purge_refuses_active_truth_writer(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    apply_install(
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    data_dir = instance_root / "data"
    ready = tmp_path / "writer-ready"
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from scope_recall.core.writer_lease import holding_truth_writer_lease\n"
        "data=Path(sys.argv[1]); ready=Path(sys.argv[2])\n"
        "with holding_truth_writer_lease(data, role='provider'):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    input()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(data_dir), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
        assert any("purge_busy" in item for item in purge_plan.conflicts)
        assert (instance_root / "data" / "memory.sqlite3").is_file()
    finally:
        if child.stdin is not None:
            child.stdin.write("\n")
            child.stdin.flush()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _hermes_host_sentinels(home: Path) -> dict[str, str]:
    files = {
        home / "config.yaml": "model: none\nmemory:\n  provider: default\n",
        home / "SOUL.md": "# host soul sentinel\n",
        home / "plugins" / "other-plugin" / "plugin.yaml": "name: other-plugin\n",
        home / "sessions" / "sess-1.json": '{"id":"sess-1"}\n',
    }
    snapshot: dict[str, str] = {}
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        snapshot[str(path)] = _sha256_file(path)
    return snapshot


def test_hermes_existing_home_coexistence_upgrade_uninstall_purge(tmp_path):
    home = (tmp_path / "hermes-home").resolve()
    plugin_dir = (tmp_path / "wrapper" / "scope-recall").resolve()
    project_root = (tmp_path / "workspace").resolve()
    plugin_dir.mkdir(parents=True)
    project_root.mkdir()
    sentinels = _hermes_host_sentinels(home)

    first = apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=home,
            project_root=project_root,
            agent_id="TEST-P19-home",
            python_executable=Path(sys.executable),
        )
    )
    assert first.installation_id
    assert (home / "scope-recall" / "installation.json").is_file()
    assert _snapshot([Path(path) for path in sentinels]) == sentinels
    receipt = json.loads((home / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
    owned_paths = [item["path"] for item in receipt.get("files", [])]

    def _norm(path: Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))

    for path in sentinels:
        assert _norm(Path(path)) not in owned_paths
    assert _norm(home) not in owned_paths
    namespace = _norm(home / "scope-recall")
    plugin_norm = _norm(plugin_dir)
    # The Hermes setup skill is the one owned file that deliberately lives
    # outside the namespace: ``maintenance/install.py`` writes it to
    # ``instance_root/skills`` and the uninstall path already carves out exactly
    # this path.  The assertion was never updated when that landed.
    setup_skill = _norm(home / "skills" / "scope-recall-setup" / "SKILL.md")
    assert owned_paths and all(
        item.startswith(namespace + os.sep)
        or item == namespace
        or item.startswith(plugin_norm + os.sep)
        or item == plugin_norm
        or item == setup_skill
        or item.endswith(".scope-recall-install-receipt.json")
        for item in owned_paths
    )

    upgrade = apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=home,
            project_root=project_root,
            agent_id="TEST-P19-home",
            python_executable=Path(sys.executable),
        )
    )
    assert upgrade.installation_id == first.installation_id
    assert _snapshot([Path(path) for path in sentinels]) == sentinels

    memory_hash = _sha256_file(home / "scope-recall" / "memory.sqlite3")
    uninstall = apply_uninstall(plan_uninstall(instance_root=home, target_plugin_dir=plugin_dir))
    assert uninstall.memory_retained is True
    assert uninstall.purged is False
    assert (home / "scope-recall" / "memory.sqlite3").is_file()
    assert _sha256_file(home / "scope-recall" / "memory.sqlite3") == memory_hash
    assert not (plugin_dir / "__init__.py").is_file()
    assert _snapshot([Path(path) for path in sentinels]) == sentinels

    apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=home,
            project_root=project_root,
            agent_id="TEST-P19-home",
            python_executable=Path(sys.executable),
        )
    )
    purge = apply_uninstall(
        plan_uninstall(instance_root=home, target_plugin_dir=plugin_dir, purge=True),
        purge=True,
    )
    assert purge.purged is True
    assert not (home / "scope-recall" / "memory.sqlite3").exists()
    assert not (home / "scope-recall" / "installation.json").exists()
    assert _snapshot([Path(path) for path in sentinels]) == sentinels


def test_hermes_unknown_scope_recall_namespace_refused(tmp_path):
    home = (tmp_path / "hermes-home").resolve()
    plugin_dir = (tmp_path / "wrapper" / "scope-recall").resolve()
    project_root = (tmp_path / "workspace").resolve()
    plugin_dir.mkdir(parents=True)
    project_root.mkdir()
    sentinels = _hermes_host_sentinels(home)
    unknown = home / "scope-recall"
    unknown.mkdir()
    (unknown / "stray.txt").write_text("not ours", encoding="utf-8")

    plan = plan_install(
        host="hermes",
        target_plugin_dir=plugin_dir,
        instance_root=home,
        project_root=project_root,
        agent_id="TEST-P19-unknown",
        python_executable=Path(sys.executable),
    )
    assert any("foreign instance content" in item and "scope-recall" in item for item in plan.conflicts)
    with pytest.raises(InstallError, match="foreign instance content"):
        apply_install(plan)
    assert _snapshot([Path(path) for path in sentinels]) == sentinels
    assert (unknown / "stray.txt").read_text(encoding="utf-8") == "not ours"


def test_hermes_purge_refuses_active_truth_writer(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    data_dir = instance_root / "scope-recall"
    ready = tmp_path / "writer-ready"
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from scope_recall.core.writer_lease import holding_truth_writer_lease\n"
        "data=Path(sys.argv[1]); ready=Path(sys.argv[2])\n"
        "with holding_truth_writer_lease(data, role='provider'):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    input()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(data_dir), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
        assert any("purge_busy" in item for item in purge_plan.conflicts)
        assert (data_dir / "memory.sqlite3").is_file()
    finally:
        if child.stdin is not None:
            child.stdin.write("\n")
            child.stdin.flush()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_explicit_purge_hermes_removes_owned_data(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P14-agent",
            python_executable=Path(sys.executable),
        )
    )
    purge_plan = plan_uninstall(instance_root=instance_root, target_plugin_dir=plugin_dir, purge=True)
    assert purge_plan.conflicts == []
    result = apply_uninstall(purge_plan, purge=True)
    assert result.purged is True
    assert not (instance_root / "scope-recall" / "memory.sqlite3").exists()
    assert not (instance_root / "scope-recall" / "installation.json").exists()


def test_hermes_default_workspace_matches_host_contract(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    plan = plan_install(
        host="hermes",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="default",
        python_executable=Path(sys.executable),
        test_mode=True,
    )
    assert plan.agent_workspace == "hermes"
    apply_install(plan)
    manifest = json.loads((instance_root / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    assert {row["agent_workspace"] for row in manifest["audiences"]} == {"hermes"}
    receipt = json.loads((instance_root / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["agent_workspace"] == "hermes"
    assert "workspace:6:hermes" in manifest["audiences"][0]["capture_scope_id"]

    from scope_recall.adapters.hermes.identity import bind_hermes_identity

    matched = bind_hermes_identity(
        "TEST-host-session",
        hermes_home=str(instance_root),
        platform="cli",
        agent_identity="default",
        agent_workspace="hermes",
        user_id="local",
        agent_context="primary",
    )
    assert matched.runtime_audience.capability_gaps == ()
    mismatched = bind_hermes_identity(
        "TEST-host-session",
        hermes_home=str(instance_root),
        platform="cli",
        agent_identity="default",
        agent_workspace="default",
        user_id="local",
        agent_context="primary",
    )
    gaps = mismatched.runtime_audience.capability_gaps
    assert "capability_gap:audience_unmapped" in gaps
    assert "capability_gap:no_allowed_scope" in gaps
    assert not mismatched.runtime_audience.allowed_scope_ids


def test_hermes_explicit_workspace_mismatch_is_fail_closed(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="default",
            python_executable=Path(sys.executable),
            agent_workspace="default",
            test_mode=True,
        )
    )
    manifest = json.loads((instance_root / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    assert {row["agent_workspace"] for row in manifest["audiences"]} == {"default"}

    from scope_recall.adapters.hermes.identity import bind_hermes_identity

    host_like = bind_hermes_identity(
        "TEST-host-session",
        hermes_home=str(instance_root),
        platform="cli",
        agent_identity="default",
        agent_workspace="hermes",
        user_id="local",
        agent_context="primary",
    )
    gaps = host_like.runtime_audience.capability_gaps
    assert "capability_gap:audience_unmapped" in gaps
    assert "capability_gap:no_allowed_scope" in gaps
    assert not host_like.runtime_audience.allowed_scope_ids


def test_hermes_reuse_rejects_workspace_mismatch_without_writes(tmp_path):
    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    apply_install(
        plan_install(
            host="hermes",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="TEST-P19-ws",
            python_executable=Path(sys.executable),
            test_mode=True,
        )
    )
    tracked = [
        instance_root / "scope-recall" / "installation.json",
        instance_root / "scope-recall" / "memory.sqlite3",
        plugin_dir / "__init__.py",
        instance_root / ".scope-recall-install-receipt.json",
    ]
    before = _snapshot(tracked)
    mismatch = plan_install(
        host="hermes",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="TEST-P19-ws",
        python_executable=Path(sys.executable),
        agent_workspace="default",
        test_mode=True,
    )
    assert any("agent_workspace mismatch" in item for item in mismatch.conflicts)
    assert _snapshot(tracked) == before
    with pytest.raises(InstallError, match="agent_workspace mismatch"):
        apply_install(mismatch)
    assert _snapshot(tracked) == before


def test_hermes_reuse_preserves_explicit_legacy_workspace_audience(tmp_path):
    from scope_recall.adapters.hermes.installation import (
        build_installation_manifest, install_hermes_scope_recall,
    )
    instance, plugin, project = _install_paths(tmp_path, host="hermes")
    base = build_installation_manifest(instance, agent_id="TEST-matrix", agent_workspace="hermes", test_mode=True)
    owner = base.audiences[0]
    historical = {**owner, "kind": "conversation", "agent_workspace": "legacy-workspace",
                  "allowed_scope_ids": ["TEST-legacy-scope"], "writable_scope_ids": ["TEST-legacy-scope"],
                  "capture_scope_id": "TEST-legacy-scope"}
    install_hermes_scope_recall(instance, agent_id="TEST-matrix", agent_workspace="hermes",
                               audiences=[owner, historical], test_mode=True)
    manifest_path = instance / "scope-recall/installation.json"
    before = manifest_path.read_bytes()
    plan = plan_install(host="hermes", target_plugin_dir=plugin, instance_root=instance,
                        project_root=project, agent_id="TEST-matrix", agent_workspace="hermes",
                        python_executable=Path(sys.executable), test_mode=True)
    assert not plan.conflicts
    apply_install(plan)
    assert manifest_path.read_bytes() == before
    import yaml
    assert yaml.safe_load((plugin / "plugin.yaml").read_text())["version"] == __version__


def test_codex_rejects_agent_workspace_and_keeps_empty_binding(tmp_path):
    from scope_recall.maintenance import cli as maintenance_cli

    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    plan = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="main",
        python_executable=Path(sys.executable),
    )
    assert plan.agent_workspace == ""
    with pytest.raises(InstallError, match="not used for Codex"):
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="main",
            python_executable=Path(sys.executable),
            agent_workspace="hermes",
        )
    argv = [
        "plan-install",
        "--host",
        "codex",
        "--target-plugin-dir",
        str(plugin_dir),
        "--instance-root",
        str(instance_root),
        "--project-root",
        str(project_root),
        "--agent-id",
        "main",
        "--agent-workspace",
        "hermes",
        "--python",
        str(Path(sys.executable)),
    ]
    assert maintenance_cli.main(argv) == 2


def test_hermes_cli_default_and_explicit_workspace(tmp_path, capsys):
    from scope_recall.maintenance import cli as maintenance_cli

    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="hermes")
    base = [
        "plan-install",
        "--host",
        "hermes",
        "--target-plugin-dir",
        str(plugin_dir),
        "--instance-root",
        str(instance_root),
        "--project-root",
        str(project_root),
        "--agent-id",
        "default",
        "--python",
        str(Path(sys.executable)),
        "--test-mode",
    ]
    assert maintenance_cli.main(base) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["agent_workspace"] == "hermes"
    assert maintenance_cli.main([*base, "--agent-workspace", "custom-ws"]) == 0
    assert json.loads(capsys.readouterr().out)["agent_workspace"] == "custom-ws"
    assert maintenance_cli.main(["apply-install", *base[1:]]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["installation_id"]
    receipt = json.loads((instance_root / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
    assert receipt["agent_workspace"] == "hermes"
    manifest = json.loads((instance_root / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    assert {row["agent_workspace"] for row in manifest["audiences"]} == {"hermes"}


def test_codex_env_file_is_written_into_every_wrapper_and_hermes_rejects_it(tmp_path, capsys):
    """Codex starts the MCP server and hooks with its own environment, so the
    credential file the worker already uses must reach both wrappers verbatim
    and be recorded in the receipt; Hermes processes inherit the gateway
    environment and must not carry a second credential path."""
    from scope_recall.maintenance import cli as maintenance_cli

    instance_root, plugin_dir, project_root = _install_paths(tmp_path, host="codex")
    env_file = (tmp_path / "secrets" / "embedding.env").resolve()
    env_file.parent.mkdir()
    env_file.write_text("SCOPE_RECALL_TEST_EMBED_KEY=unused-by-installer\n", encoding="utf-8")

    plan = plan_install(
        host="codex",
        target_plugin_dir=plugin_dir,
        instance_root=instance_root,
        project_root=project_root,
        agent_id="main",
        python_executable=Path(sys.executable),
        env_file=env_file,
    )
    assert not plan.conflicts
    assert plan.env_file == env_file
    assert plan.to_dict()["env_file"] == str(env_file)
    apply_install(plan)

    mcp = json.loads((plugin_dir / ".mcp.json").read_text(encoding="utf-8"))
    args = mcp["mcpServers"]["scope-recall"]["args"]
    assert args[:4] == ["-I", "-B", "-m", "scope_recall.adapters.codex.mcp_entry"]
    assert args[-4:] == ["--workspace", str(project_root), "--env-file", str(env_file)]
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for event, entries in hooks["hooks"].items():
        command = entries[0]["hooks"][0]["command"]
        assert shlex.split(command)[-2:] == ["--env-file", str(env_file)], event
    launcher = (plugin_dir / "hooks" / "scope-recall-hook.cmd").read_bytes().decode("utf-8")
    assert f'"--env-file" "{env_file}"' in launcher
    receipt = json.loads((instance_root / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
    assert Path(receipt["env_file"]).samefile(env_file)

    # A file the entry could never read is refused at plan time, not at first recall.
    with pytest.raises(InstallError, match="env_file"):
        plan_install(
            host="codex",
            target_plugin_dir=plugin_dir,
            instance_root=instance_root,
            project_root=project_root,
            agent_id="main",
            python_executable=Path(sys.executable),
            env_file=tmp_path / "secrets" / "missing.env",
        )

    hermes_instance, hermes_plugin, hermes_project = _install_paths(tmp_path / "hermes", host="hermes")
    with pytest.raises(InstallError, match="only used for Codex"):
        plan_install(
            host="hermes",
            target_plugin_dir=hermes_plugin,
            instance_root=hermes_instance,
            project_root=hermes_project,
            agent_id="default",
            python_executable=Path(sys.executable),
            env_file=env_file,
            test_mode=True,
        )
    assert maintenance_cli.main([
        "plan-install", "--host", "hermes",
        "--target-plugin-dir", str(hermes_plugin),
        "--instance-root", str(hermes_instance),
        "--project-root", str(hermes_project),
        "--agent-id", "default",
        "--python", str(Path(sys.executable)),
        "--env-file", str(env_file),
        "--test-mode",
    ]) == 2
    capsys.readouterr()


def test_package_health_record_bytes_and_declared_dependencies(tmp_path, monkeypatch):
    """A real dist-info fixture, not a hard-coded production-version table."""
    import base64
    import tomllib
    from importlib import metadata
    from scope_recall.maintenance.package_health import record_integrity, dependency_health

    site = tmp_path / "TEST-site"
    dist_dir = site / "hermes_scope_recall-3.1.0rc28.dist-info"
    dist_dir.mkdir(parents=True)
    payload = site / "scope_recall" / "sample.py"
    payload.parent.mkdir()
    raw = b"value = 1\r\n"
    payload.write_bytes(raw)
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    (dist_dir / "RECORD").write_text(f"scope_recall/sample.py,sha256={digest},{len(raw)}\n", encoding="utf-8")
    project = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specs = list(project["dependencies"])
    for extra in ("lancedb", "codex"):
        specs.extend(f"{spec}; extra == '{extra}'" for spec in project["optional-dependencies"][extra])
    (dist_dir / "METADATA").write_text("Metadata-Version: 2.1\nName: hermes-scope-recall\nVersion: 3.1.0rc28\n" +
                                     "".join(f"Requires-Dist: {s}\n" for s in specs), encoding="utf-8")
    versions = {"PyYAML": "6.0.3", "jsonschema": "4.25.1", "packaging": "25.0", "tzdata": "2026.1",
                "lancedb": "0.37.1", "pyarrow": "24.0.0", "mcp": "2.0.0", "pydantic": "2.13.4"}
    for name, version in versions.items():
        info = site / f"{name}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    dist = metadata.Distribution.at(dist_dir)
    assert record_integrity(dist)["status"] == "ok"
    payload.write_bytes(raw.replace(b"1", b"2"))  # Same length, different byte.
    assert record_integrity(dist)["mismatch_count"] == 1
    payload.write_bytes(raw.replace(b"\r\n", b"\n"))
    assert record_integrity(dist)["status"] == "mismatch", "newline normalization must not mask drift"
    payload.write_bytes(raw)
    assert record_integrity(dist)["status"] == "ok"
    good = dependency_health(dist.requires)
    assert good["status"] == "ok", good
    mcp_metadata = site / "mcp-2.0.0.dist-info" / "METADATA"
    original = mcp_metadata.read_text(encoding="utf-8")
    mcp_metadata.write_text(original.replace("Version: 2.0.0", "Version: 99.0"), encoding="utf-8")
    bad = dependency_health(dist.requires)
    assert bad["status"] == "mismatch"
    assert any(row["name"] == "mcp" and not row["ok"] for row in bad["requirements"])
    mcp_metadata.write_text(original, encoding="utf-8")
    assert dependency_health(dist.requires)["status"] == "ok"
    print(json.dumps({"record_bytes": "clean / same-size edit / CRLF edit / restored",
                      "dependencies": {"clean": good, "defect": bad}}, ensure_ascii=False))


def test_package_health_doctor_three_way_versions(tmp_path, monkeypatch):
    from scope_recall.maintenance import package_health
    from scope_recall.runtime.running_code import record_running_code

    instance, plugin, project = _install_paths(tmp_path, host="hermes")
    apply_install(plan_install(host="hermes", target_plugin_dir=plugin, instance_root=instance,
                              project_root=project, agent_id="TEST-health", python_executable=Path(sys.executable)))
    record_path = record_running_code(instance / "scope-recall", host_adapter="hermes")
    assert record_path is not None
    original = record_path.read_text(encoding="utf-8")
    probe = {"source": "installed", "version": __version__, "distribution_version": __version__,
             "hot_patched": {"status": "ok"}, "dependency_drift": {"status": "ok"}}
    monkeypatch.setattr(package_health, "package_probe", lambda: probe)

    def check():
        report = run_doctor(host="hermes", instance_root=instance)
        assert all(name in report.to_dict()["package_health"] for name in
                   ("hot_patched", "dependency_drift", "version_mismatch"))
        return report

    assert check().package_health["version_mismatch"]["status"] == "ok"
    for component in ("receipt", "distribution", "running"):
        receipt_path = instance / ".scope-recall-install-receipt.json"
        receipt_raw = receipt_path.read_text(encoding="utf-8")
        if component == "receipt":
            receipt = json.loads(receipt_raw)
            receipt["package_version"] = "0.0.1"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        elif component == "distribution":
            probe["distribution_version"] = "0.0.1"
        else:
            record = json.loads(original)
            record["version"] = "0.0.1"
            record_path.write_text(json.dumps(record), encoding="utf-8")
        report = check()
        assert "version_mismatch" in report.capability_gaps
        print(json.dumps({"component": component, "check": report.package_health["version_mismatch"]}))
        receipt_path.write_text(receipt_raw, encoding="utf-8")
        record_path.write_text(original, encoding="utf-8")
        probe["distribution_version"] = __version__
        assert "version_mismatch" not in check().capability_gaps
    for gap in ("hot_patched", "dependency_drift"):
        probe[gap] = {"status": "mismatch"}
        assert gap in check().capability_gaps
        probe[gap] = {"status": "ok"}
        assert gap not in check().capability_gaps
    record_path.unlink()
    assert check().package_health["version_mismatch"]["status"] == "incomplete"
