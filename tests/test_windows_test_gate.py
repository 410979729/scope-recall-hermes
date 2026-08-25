"""P1-11: Windows no-symlink privilege must still run product paths."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from plugin_source import (
    ASSUME_NO_SYMLINK_ENV,
    FORCE_COPY_PLUGIN_ENV,
    assert_same_source,
    install_plugin_tree,
    probe_symlink_privilege,
    require_symlink_privilege,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
PEER_RECOVERY_TESTS = PLUGIN_ROOT / "tests" / "test_initialize_peer_recovery.py"
INSTALLER_TESTS = PLUGIN_ROOT / "tests" / "test_installer.py"


def _ci() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _windows_no_symlink_job() -> dict:
    workflow = _ci()
    job = workflow["jobs"].get("windows-no-symlink")
    assert job is not None, "CI must define a windows-no-symlink job"
    return job


def test_copy_fallback_validates_content_identity_not_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv(FORCE_COPY_PLUGIN_ENV, "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "provider.py").write_text("MARKER = 'workspace'\n", encoding="utf-8")
    plugin_dir = tmp_path / "hermes-home" / "plugins" / "scope-recall"

    kind = install_plugin_tree(plugin_dir, repo)

    assert kind == "copy"
    assert plugin_dir.resolve() != repo.resolve()
    assert not plugin_dir.is_symlink()
    assert_same_source(plugin_dir / "provider.py", repo / "provider.py", label="provider.py")
    with pytest.raises(AssertionError, match="content identity"):
        (plugin_dir / "provider.py").write_text("MARKER = 'stale-copy'\n", encoding="utf-8")
        assert_same_source(plugin_dir / "provider.py", repo / "provider.py", label="provider.py")


def test_force_copy_lane_does_not_claim_symlink_privilege(tmp_path, monkeypatch):
    monkeypatch.setenv(ASSUME_NO_SYMLINK_ENV, "1")

    assert probe_symlink_privilege(tmp_path) is False
    with pytest.raises(pytest.skip.Exception, match="symlink privilege unavailable"):
        require_symlink_privilege(tmp_path)


def test_ci_defines_blocking_windows_no_symlink_lane():
    job = _windows_no_symlink_job()
    assert job.get("continue-on-error") is None
    assert job["runs-on"] == "windows-latest"
    python_step = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python")
    )
    assert python_step["with"]["python-version"] in {"3.11", "3.12"}
    product = next(
        step
        for step in job["steps"]
        if "no-symlink" in str(step.get("name", "")).lower()
        or "peer" in str(step.get("run", "")).lower()
    )
    env = product.get("env") or {}
    command = str(product.get("run") or "")
    assert env.get(FORCE_COPY_PLUGIN_ENV) == "1"
    assert env.get(ASSUME_NO_SYMLINK_ENV) == "1"
    assert "test_initialize_peer_recovery.py" in command
    assert "test_installer.py" in command
    assert "test_windows_test_gate.py" in command
    assert "continue-on-error" not in yaml.safe_dump(job)


def test_peer_recovery_tests_have_no_blanket_windows_skip():
    source = PEER_RECOVERY_TESTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_skips = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and "skipif" in ast.dump(node)
        and "win" in ast.dump(node).lower()
    ]
    assert module_skips == []
    assert "os.name == \"nt\"" in source
    assert "POSIX directory-symlink fail-closed contract" in source
    assert "Windows junctions are the supported same-file alias" in source
    assert source.count("@pytest.mark.skipif") == 2
    assert "assert_same_source" in source
    assert "Path(runtime_module.__file__).resolve() == _WORKSPACE_PROVIDER" not in source


def test_installer_keeps_ordinary_file_coverage_and_probes_symlink_tests():
    source = INSTALLER_TESTS.read_text(encoding="utf-8")
    assert "def test_atomic_config_replace_updates_ordinary_file(" in source
    assert "require_symlink_privilege" in source
    assert "def test_atomic_config_replace_preserves_symlink_identity_and_target_mode(" in source
    assert source.index("require_symlink_privilege") < source.index(
        "def test_atomic_config_replace_preserves_symlink_identity_and_target_mode("
    )
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "symlink" not in node.name:
            continue
        dumped = ast.dump(node)
        if node.name == "test_installer_excludes_local_secret_state_and_symlink_artifacts":
            assert "probe_symlink_privilege" in dumped or "symlink_to" in dumped
            continue
        assert "require_symlink_privilege" in dumped, node.name
    assert "@pytest.mark.skipif(os.name == \"nt\"" not in source
    assert "@pytest.mark.skipif(sys.platform == \"win32\"" not in source
