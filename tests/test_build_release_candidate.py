"""Single-command release-candidate build boundary tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build.release_candidate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_build_release_candidate_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_build_command_is_single_and_content_free() -> None:
    build = _load_module()

    command = build._normalized_build_command()

    assert command == [
        "python",
        "-m",
        "build",
        "--no-isolation",
        "--wheel",
        "--sdist",
        "--outdir",
        "<isolated-build-dir>",
    ]
    assert str(Path.home()) not in " ".join(command)


def test_candidate_build_requires_full_exact_source_sha() -> None:
    build = _load_module()
    exact = "a" * 40

    build._require_expected_source(
        {"commit": exact, "tree": "b" * 40, "clean": True},
        exact,
    )
    with pytest.raises(build.ReleaseCandidateBuildError, match="full lowercase"):
        build._require_expected_source(
            {"commit": exact, "tree": "b" * 40, "clean": True},
            "a" * 12,
        )
    with pytest.raises(build.ReleaseCandidateBuildError, match="SHA mismatch"):
        build._require_expected_source(
            {"commit": "b" * 40, "tree": "c" * 40, "clean": True},
            exact,
        )


def test_candidate_build_selects_exactly_one_versioned_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    build = _load_module()
    wheel = tmp_path / "hermes_scope_recall-2.0.1-py3-none-any.whl"
    sdist = tmp_path / "hermes_scope_recall-2.0.1.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    assert build._select_distribution_artifacts(tmp_path, "2.0.1") == (
        wheel,
        sdist,
    )
    (tmp_path / "unexpected.whl").write_bytes(b"other")
    with pytest.raises(build.ReleaseCandidateBuildError, match="exactly"):
        build._select_distribution_artifacts(tmp_path, "2.0.1")


def test_candidate_build_environment_is_bounded_to_isolated_directory(
    tmp_path: Path,
) -> None:
    build = _load_module()
    boundary = tmp_path / "candidate-boundary"

    env = build._isolated_environment(
        boundary,
        active_hermes_home=tmp_path / "active-hermes-do-not-touch",
    )

    for key in (
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
        Path(env[key]).resolve().relative_to(boundary.resolve())
    assert env["PYTHONNOUSERSITE"] == "1"
    assert Path(env["SCOPE_RECALL_TEST_BOUNDARY_PARENT"]).name == "pytest"
    plugin_dir = Path(env["SCOPE_RECALL_PLUGIN_DIR"])
    assert plugin_dir.parent.is_dir()
    assert not plugin_dir.exists()


def test_candidate_build_script_path_entrypoint_is_importable() -> None:
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
    assert "--expected-sha" in result.stdout
    assert "--hermes-root" in result.stdout


def test_final_candidate_build_requires_hermes_root(tmp_path: Path) -> None:
    build = _load_module()
    common = [
        "--expected-sha",
        "a" * 40,
        "--active-hermes-home",
        str(tmp_path / "active-hermes"),
    ]

    with pytest.raises(SystemExit):
        build.parse_args(common)

    parsed = build.parse_args(
        [*common, "--hermes-root", str(tmp_path / "pinned-hermes")]
    )
    assert parsed.hermes_root == tmp_path / "pinned-hermes"


def test_candidate_build_retains_failed_staging_evidence(tmp_path: Path) -> None:
    build = _load_module()

    with pytest.raises(build.ReleaseCandidateBuildError, match="retained"):
        with build._retained_staging(tmp_path, prefix=".candidate.") as staging:
            (staging / "ARTIFACT_SCAN.json").write_text("{}\n", encoding="utf-8")
            raise build.ReleaseCandidateBuildError("artifact scan failed")

    retained = list(tmp_path.glob(".candidate.*"))
    assert len(retained) == 1
    assert (retained[0] / "ARTIFACT_SCAN.json").is_file()


def test_candidate_run_timeout_terminates_tree_and_retains_partial_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
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
    monkeypatch.setattr(build.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate_tree(target) -> None:
        terminated.append(target.pid)
        target.returncode = -9

    monkeypatch.setattr(build, "_terminate_process_tree", terminate_tree)
    log = tmp_path / "timeout.log"

    with pytest.raises(build.ReleaseCandidateBuildError, match="TimeoutExpired"):
        build._run(
            ["python", "slow.py"],
            cwd=tmp_path,
            timeout=7,
            log_path=log,
        )

    assert terminated == [4321]
    assert calls == [7, build.PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS]
    assert log.read_text(encoding="utf-8") == "partial output\n"


def test_candidate_windows_timeout_targets_exact_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
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

    monkeypatch.setattr(build.subprocess, "run", fake_run)

    build._terminate_process_tree(process, platform="nt")

    assert captured[0][0] == ["taskkill", "/PID", "2468", "/T", "/F"]
    assert captured[0][1]["timeout"] == (
        build.PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS
    )
    assert process.killed is False


def test_candidate_posix_timeout_targets_exact_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
    captured: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 1357
        returncode: int | None = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = FakeProcess()

    def fake_killpg(pid: int, signum: int) -> None:
        captured.append((pid, signum))
        process.returncode = -9

    monkeypatch.setattr(build.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(build.signal, "SIGKILL", 9, raising=False)

    build._terminate_process_tree(process, platform="posix")

    assert captured == [(1357, 9)]


def test_candidate_install_stages_use_bounded_stage_specific_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
    timeouts: list[int] = []

    monkeypatch.setattr(build, "_isolated_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        build,
        "_venv_python",
        lambda _root: tmp_path / "venv" / "Scripts" / "python.exe",
    )

    def fake_run(_command, **kwargs):
        timeouts.append(int(kwargs["timeout"]))
        log_path = kwargs.get("log_path")
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.name.endswith("_SMOKE.log"):
                log_path.write_text(
                    '{"ok": true, "plugin_dir": "'
                    + str(tmp_path / "plugin").replace("\\", "\\\\")
                    + '"}\n',
                    encoding="utf-8",
                )
            elif log_path.name.endswith("_DOCTOR.log"):
                log_path.write_text(
                    '{"ok": true, "schema_version": "doctor_report.v1", '
                    '"source": {"pyproject_version": "2.0.1"}}\n',
                    encoding="utf-8",
                )
            else:
                log_path.write_text("", encoding="utf-8")
        return {"exit_code": 0}

    monkeypatch.setattr(build, "_run", fake_run)
    build._verify_install(
        artifact=tmp_path / "candidate.whl",
        kind="wheel",
        workspace=tmp_path / "workspace",
        evidence_dir=tmp_path / "evidence",
        active_hermes_home=tmp_path / "active-hermes",
    )

    assert timeouts == [
        build.VENV_TIMEOUT_SECONDS,
        build.INSTALL_TIMEOUT_SECONDS,
        build.STAGE_TIMEOUT_SECONDS,
        build.STAGE_TIMEOUT_SECONDS,
        build.STAGE_TIMEOUT_SECONDS,
    ]
    assert build.BUILD_TIMEOUT_SECONDS == 600
    assert build.VENV_TIMEOUT_SECONDS == 1800
    assert build.INSTALL_TIMEOUT_SECONDS == 1800
    assert build.SDIST_TEST_TIMEOUT_SECONDS == 900
    assert build.STAGE_TIMEOUT_SECONDS == 300
    assert build.PROCESS_TREE_TERMINATION_TIMEOUT_SECONDS == 30


def test_candidate_windows_nested_package_path_budget() -> None:
    build = _load_module()
    deep_suffix = (
        "pytest",
        "sr-package-work-12345678",
        "builder-venv",
        "Lib",
        "site-packages",
        "pkg_resources",
        "tests",
        "data",
        "my-test-package_unpacked-egg",
        "my_test_package-1.0-py3.7.egg",
        "EGG-INFO",
    )
    current = PureWindowsPath(
        f"{build.BUILD_WORKSPACE_PREFIX}12345678",
        build.SDIST_TEST_BOUNDARY_DIRNAME,
        *deep_suffix,
    )
    legacy = PureWindowsPath(
        "scope.recall.candidate.build.12345678",
        "boundary-sdist-tests",
        *deep_suffix,
    )

    assert build.BUILD_WORKSPACE_PREFIX == "srb."
    assert build.SDIST_TEST_BOUNDARY_DIRNAME == "s"
    assert len(str(current)) <= 175
    assert len(str(legacy)) - len(str(current)) >= 40


def test_sdist_module_runner_precreates_basetemp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    boundaries: list[Path] = []
    monkeypatch.setattr(
        build,
        "read_archive_members",
        lambda _path: {
            "hermes_scope_recall-2.0.1/tests/test_fixture.py": b"def test_ok(): pass\n"
        },
    )
    def fake_environment(boundary: Path, **_kwargs):
        boundaries.append(boundary)
        return {}

    monkeypatch.setattr(build, "_isolated_environment", fake_environment)

    def fake_run(command, **_kwargs):
        basetemp = Path(command[command.index("--basetemp") + 1])
        assert basetemp.parent.is_dir()
        return {
            "exit_code": 0,
            "duration_seconds": 0.01,
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:00+00:00",
        }

    monkeypatch.setattr(build, "_run", fake_run)
    receipts = build._run_sdist_tests(
        python=Path(sys.executable),
        sdist=tmp_path / "candidate.tar.gz",
        release_check=SimpleNamespace(
            REQUIRED_SOURCE_RESTORE_SDIST_TESTS={"tests/test_fixture.py"}
        ),
        workspace=workspace,
        evidence_dir=evidence,
        active_hermes_home=tmp_path / "active",
    )

    assert receipts == [
        {
            "module": "tests/test_fixture.py",
            "timeout_seconds": build.SDIST_TEST_TIMEOUT_SECONDS,
            "exit_code": 0,
            "duration_seconds": 0.01,
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:00+00:00",
        }
    ]
    assert boundaries == [workspace / build.SDIST_TEST_BOUNDARY_DIRNAME]
