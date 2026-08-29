"""Final local validation runner boundary and coverage contracts."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run.release_validation.py"
REHEARSAL_SPEC = ROOT / "scripts" / "release.candidate_rehearsals.json"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_release_validation_runner_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_maps_every_canonical_rehearsal_node_once() -> None:
    module = _load_module()
    spec = json.loads(REHEARSAL_SPEC.read_text(encoding="utf-8"))
    expected = {
        node
        for gate in spec["gates"]
        for node in gate["node_ids"]
    }
    actual = [
        node
        for node_ids in module.REHEARSAL_RECEIPTS.values()
        for node in node_ids
    ]

    assert set(actual) == expected
    assert len(actual) == len(set(actual))


def test_runner_environment_keeps_every_write_target_isolated(
    tmp_path: Path,
) -> None:
    module = _load_module()
    boundary = tmp_path / "boundary"
    active = tmp_path / "active"
    real_home = tmp_path / "real-home"

    environment = module._isolated_environment(
        boundary,
        active_hermes_home=active,
        real_home=real_home,
    )

    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "SCOPE_RECALL_TEST_BOUNDARY_PARENT",
        "HERMES_HOME",
        "SCOPE_RECALL_DB",
        "SCOPE_RECALL_LOG_DIR",
        "SCOPE_RECALL_LEASE_DIR",
        "SCOPE_RECALL_PLUGIN_DIR",
    ):
        Path(environment[name]).resolve().relative_to(boundary.resolve())
    plugin_dir = Path(environment["SCOPE_RECALL_PLUGIN_DIR"])
    assert plugin_dir.parent.is_dir()
    assert not plugin_dir.exists()
    assert environment["SCOPE_RECALL_REAL_HOME"] == str(real_home)
    assert environment["SCOPE_RECALL_ACTIVE_HERMES_HOME"] == str(active)
    assert Path(environment["SCOPE_RECALL_TEST_BOUNDARY_PARENT"]).name == "p"


def test_runner_pytest_basetemp_uses_declared_short_boundary(tmp_path: Path) -> None:
    module = _load_module()
    parent = tmp_path / "p"

    target = module._pytest_basetemp(
        {"SCOPE_RECALL_TEST_BOUNDARY_PARENT": str(parent)},
        "f",
    )

    assert target == parent.resolve() / "f"


def test_validation_boundary_cleanup_retries_transient_windows_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    boundary = tmp_path / "srv.retry"
    boundary.mkdir()
    attempts: list[Path] = []

    def flaky_rmtree(path: Path, *, onerror=None) -> None:
        assert callable(onerror)
        target = Path(path)
        attempts.append(target)
        if len(attempts) < 3:
            raise OSError(145, "directory is not empty")
        target.rmdir()

    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(module.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    module._cleanup_validation_boundary(boundary)

    assert attempts == [module._validation_cleanup_path(boundary.resolve())] * 3
    assert not boundary.exists()


def test_validation_boundary_cleanup_repairs_readonly_descendant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    boundary = tmp_path / "srv.readonly"
    locked = boundary / "locked"
    locked.mkdir(parents=True)
    marker = locked / "marker.txt"
    marker.write_text("isolated\n", encoding="utf-8")
    marker.chmod(stat.S_IREAD)
    locked.chmod(stat.S_IREAD)
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))

    module._cleanup_validation_boundary(boundary)

    assert not boundary.exists()
    assert not os.path.lexists(boundary)


def test_validation_boundary_cleanup_retries_child_file_not_found(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    boundary = tmp_path / "srv.child-race"
    boundary.mkdir()
    attempts: list[Path] = []

    def child_race_rmtree(path: Path, *, onerror=None) -> None:
        assert callable(onerror)
        target = Path(path)
        attempts.append(target)
        if len(attempts) == 1:
            raise FileNotFoundError("a child disappeared during traversal")
        target.rmdir()

    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(module.shutil, "rmtree", child_race_rmtree)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    module._cleanup_validation_boundary(boundary)

    assert attempts == [module._validation_cleanup_path(boundary.resolve())] * 2
    assert not boundary.exists()


def test_validation_boundary_cleanup_refuses_false_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    boundary = tmp_path / "srv.false-success"
    boundary.mkdir()
    attempts: list[Path] = []

    def false_success_rmtree(path: Path, *, onerror=None) -> None:
        assert callable(onerror)
        target = Path(path)
        attempts.append(target)
        if len(attempts) > 1:
            target.rmdir()

    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(module.shutil, "rmtree", false_success_rmtree)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    module._cleanup_validation_boundary(boundary)

    assert attempts == [module._validation_cleanup_path(boundary.resolve())] * 2
    assert not boundary.exists()


def test_validation_run_timeout_terminates_tree_and_records_124(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    calls: list[int] = []
    terminated: list[int] = []

    class FakeProcess:
        pid = 4321
        returncode: int | None = None
        stdout = None

        def communicate(self, *, timeout: int):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(
                    cmd=["python"],
                    timeout=timeout,
                    output=b"partial output\n",
                )
            return ("partial output\n", None)

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate_tree(target) -> None:
        terminated.append(target.pid)
        target.returncode = -9

    monkeypatch.setattr(module, "_terminate_process_tree", terminate_tree)
    ledger: list[dict[str, object]] = []
    log = tmp_path / "timeout.log"

    with pytest.raises(module.ReleaseValidationError, match="exit code 124"):
        module._run(
            ["python", "slow.py"],
            display_command=["python", "<slow-stage>"],
            cwd=tmp_path,
            environment={},
            timeout_seconds=7,
            log_path=log,
            ledger=ledger,
        )

    assert terminated == [4321]
    assert calls == [7, module.PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS]
    assert log.read_text(encoding="utf-8") == "partial output\n"
    assert ledger[0]["exit_code"] == 124
    assert ledger[0]["timeout_seconds"] == 7


def test_windows_timeout_targets_exact_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    captured: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 2468
        returncode: int | None = None
        killed = False

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = FakeProcess()

    def fake_run(command, **kwargs):
        captured.append(([str(item) for item in command], dict(kwargs)))
        process.returncode = 1
        return None

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._terminate_process_tree(process, platform="nt")

    assert captured[0][0] == ["taskkill", "/PID", "2468", "/T", "/F"]
    assert captured[0][1]["timeout"] == (
        module.PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS
    )
    assert process.killed is False


def test_validation_receipt_binds_source_artifact_and_isolation() -> None:
    module = _load_module()
    context = module.ValidationContext(
        source_commit="a" * 40,
        source_tree="b" * 40,
        wheel_sha256="c" * 64,
        sdist_sha256="d" * 64,
    )

    receipt = module._receipt(
        context,
        stage={
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:01+00:00",
            "exit_code": 0,
            "log_sha256": "e" * 64,
        },
        command=["python", "isolated-check.py"],
        database_kind="fixture-copy",
    )

    assert receipt["source_commit"] == "a" * 40
    assert receipt["source_tree"] == "b" * 40
    assert receipt["artifact_sha256"] == "c" * 64
    assert receipt["result"] == "passed"
    assert receipt["environment_boundary"] == {
        "hermes_home_kind": "isolated",
        "database_kind": "fixture-copy",
        "active_instance_touched": False,
    }


def test_issue_51_receipt_embeds_content_free_rehearsal_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    staging = tmp_path / "staging"
    temp = tmp_path / "temp"
    basetemp_parent = tmp_path / "p"
    for path in (staging, temp, basetemp_parent):
        path.mkdir()
    context = module.ValidationContext(
        source_commit="a" * 40,
        source_tree="b" * 40,
        wheel_sha256="c" * 64,
        sdist_sha256="d" * 64,
    )
    install_receipt_sha256 = "e" * 64
    environment = {
        "TEMP": str(temp),
        "HERMES_HOME": str(tmp_path / "home"),
        "SCOPE_RECALL_DB": str(tmp_path / "truth.sqlite3"),
        "SCOPE_RECALL_TEST_BOUNDARY_PARENT": str(basetemp_parent),
    }

    def fake_run(
        _command,
        *,
        display_command,
        cwd,
        environment,
        timeout_seconds,
        log_path,
        ledger,
    ):
        del display_command, cwd, timeout_seconds, ledger
        guard = {
            "result": "passed",
            "artifact_sha256": context.wheel_sha256,
            "install_receipt_sha256": install_receipt_sha256,
            "source_worktree_imported": False,
            "source_worktree_on_sys_path": False,
        }
        guard_path = temp / "ISSUE_51_REGRESSION.import-guard.json"
        guard_path.write_text(json.dumps(guard), encoding="utf-8")
        details_path = Path(environment[module.ISSUE_51_DETAILS_OUTPUT_ENV])
        details_path.write_text(
            json.dumps(
                {"schema_version": module.ISSUE_51_DETAILS_SCHEMA_VERSION}
            ),
            encoding="utf-8",
        )
        Path(log_path).write_text("passed\n", encoding="utf-8")
        return {
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:01+00:00",
            "exit_code": 0,
            "log_sha256": "f" * 64,
        }

    monkeypatch.setattr(module, "_run", fake_run)
    node_ids = module.REHEARSAL_RECEIPTS["ISSUE_51_REGRESSION.json"]
    module._run_pytest_receipt(
        root=tmp_path / "source",
        harness=tmp_path / "harness",
        python=tmp_path / "python.exe",
        staging=staging,
        environment=environment,
        context=context,
        install_receipt={
            "environment_id": "1" * 64,
            "installed_distribution": "hermes-scope-recall==2.0.0",
            "direct_url_sha256": "2" * 64,
            "record_sha256": "3" * 64,
        },
        install_receipt_sha256=install_receipt_sha256,
        hermes_source=tmp_path / "hermes",
        hermes_source_identity={
            "commit": "4" * 40,
            "tree": "5" * 40,
            "clean": True,
        },
        receipt_name="ISSUE_51_REGRESSION.json",
        node_ids=node_ids,
        ledger=[],
    )

    receipt = json.loads(
        (staging / "ISSUE_51_REGRESSION.json").read_text(encoding="utf-8")
    )
    assert receipt["details"]["node_ids"] == list(node_ids)
    assert receipt["details"]["issue_51_regression"] == {
        "schema_version": module.ISSUE_51_DETAILS_SCHEMA_VERSION
    }
    assert str(tmp_path) not in json.dumps(receipt, sort_keys=True)


def test_validation_script_path_entrypoint_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert "--hermes-0-20-6-source" in result.stdout
    assert "--quarantine-path" in result.stdout
    assert "--n-minus-one-wheel" in result.stdout


def test_runner_requires_explicit_quarantine_and_n_minus_one_artifact() -> None:
    module = _load_module()
    common = [
        "--expected-sha",
        "a" * 40,
        "--active-hermes-home",
        "active",
        "--hermes-0-19-1-source",
        "hermes-0191",
        "--hermes-0-20-6-source",
        "hermes-0206",
    ]

    with pytest.raises(SystemExit):
        module.parse_args(common)
    with pytest.raises(SystemExit):
        module.parse_args([*common, "--quarantine-path", "known-quarantine"])

    parsed = module.parse_args(
        [
            *common,
            "--quarantine-path",
            "known-quarantine",
            "--n-minus-one-wheel",
            "scope-recall-1.10.3.whl",
        ]
    )
    assert parsed.quarantine_path == Path("known-quarantine")
    assert parsed.n_minus_one_wheel == Path("scope-recall-1.10.3.whl")


def test_full_suite_environment_installs_candidate_and_writable_pinned_hermes_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "candidate"
    constraints = root / "constraints"
    constraints.mkdir(parents=True)
    (constraints / "release.txt").write_text("pytest==9.1.1\n", encoding="utf-8")
    candidate_wheel = tmp_path / "candidate.whl"
    candidate_wheel.write_bytes(b"wheel")
    hermes = tmp_path / "pinned-hermes"
    hermes.mkdir()
    (hermes / "pyproject.toml").write_text(
        """[project]
name = "hermes-agent"
version = "0.19.1"
readme = "README.md"
license-files = ["LICENSE"]

[tool.setuptools]
py-modules = ["run_agent"]

[tool.setuptools.packages.find]
include = ["hermes_cli", "hermes_cli.*"]
""",
        encoding="utf-8",
    )
    for name in ("README.md", "LICENSE", "setup.py", "run_agent.py"):
        (hermes / name).write_text(f"{name}\n", encoding="utf-8")
    package = hermes / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    excluded = hermes / "website" / "deep" / "translated"
    excluded.mkdir(parents=True)
    (excluded / "irrelevant.md").write_text("not packaged\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    commands: list[list[str]] = []
    timeouts: list[int] = []

    monkeypatch.setattr(
        module,
        "_isolated_environment",
        lambda *_args, **_kwargs: {},
    )

    def fake_run(command, **kwargs):
        commands.append([str(item) for item in command])
        timeouts.append(int(kwargs["timeout_seconds"]))
        return {"log_sha256": "a" * 64}

    monkeypatch.setattr(module, "_run", fake_run)

    python = module._prepare_full_suite_environment(
        root=root,
        candidate_wheel=candidate_wheel,
        hermes_source=hermes,
        workspace=workspace,
        staging=staging,
        active_hermes_home=tmp_path / "active-hermes",
        ledger=[],
    )

    assert python == module._venv_python(workspace / "venv")
    assert timeouts == [
        module.VENV_TIMEOUT_SECONDS,
        module.INSTALL_TIMEOUT_SECONDS,
        module.INSTALL_TIMEOUT_SECONDS,
    ]
    assert module.VENV_TIMEOUT_SECONDS >= 1800
    assert module.INSTALL_TIMEOUT_SECONDS >= 1800
    assert str(candidate_wheel) + "[lancedb,dev]" in commands[1]
    hermes_install_target = Path(commands[2][-1])
    assert hermes_install_target == workspace / "hermes-source-copy"
    assert hermes_install_target != hermes
    assert (hermes_install_target / "pyproject.toml").is_file()
    assert (hermes_install_target / "run_agent.py").is_file()
    assert (hermes_install_target / "hermes_cli" / "__init__.py").is_file()
    assert not (hermes_install_target / "website").exists()
