"""Single-command release-candidate build boundary tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
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
    wheel = tmp_path / "hermes_scope_recall-2.0.0-py3-none-any.whl"
    sdist = tmp_path / "hermes_scope_recall-2.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    assert build._select_distribution_artifacts(tmp_path, "2.0.0") == (
        wheel,
        sdist,
    )
    (tmp_path / "unexpected.whl").write_bytes(b"other")
    with pytest.raises(build.ReleaseCandidateBuildError, match="exactly"):
        build._select_distribution_artifacts(tmp_path, "2.0.0")


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


def test_candidate_build_retains_failed_staging_evidence(tmp_path: Path) -> None:
    build = _load_module()

    with pytest.raises(build.ReleaseCandidateBuildError, match="retained"):
        with build._retained_staging(tmp_path, prefix=".candidate.") as staging:
            (staging / "ARTIFACT_SCAN.json").write_text("{}\n", encoding="utf-8")
            raise build.ReleaseCandidateBuildError("artifact scan failed")

    retained = list(tmp_path.glob(".candidate.*"))
    assert len(retained) == 1
    assert (retained[0] / "ARTIFACT_SCAN.json").is_file()


def test_sdist_module_runner_precreates_basetemp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_module()
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    monkeypatch.setattr(
        build,
        "read_archive_members",
        lambda _path: {
            "hermes_scope_recall-2.0.0/tests/test_fixture.py": b"def test_ok(): pass\n"
        },
    )
    monkeypatch.setattr(build, "_isolated_environment", lambda *_args, **_kwargs: {})

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
