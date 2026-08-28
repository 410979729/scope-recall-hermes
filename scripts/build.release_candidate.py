#!/usr/bin/env python3
"""Build one source-bound Scope Recall release candidate and local evidence set."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from types import ModuleType
from typing import Mapping, Sequence

try:
    from scripts.execution_boundary import (  # pyright: ignore[reportMissingImports]
        ambient_active_hermes_home,
        validate_execution_boundary,
    )
    from scripts.release_candidate_artifacts import (  # pyright: ignore[reportMissingImports]
        ArtifactVerificationError,
        archive_member_manifest,
        artifact_name_findings,
        read_archive_members,
        sha256_file,
        verify_sdist_source_correspondence,
        verify_wheel_source_correspondence,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {
        "scripts",
        "scripts.execution_boundary",
        "scripts.release_candidate_artifacts",
    }:
        raise
    from execution_boundary import (  # pyright: ignore[reportMissingImports]
        ambient_active_hermes_home,
        validate_execution_boundary,
    )
    from release_candidate_artifacts import (  # pyright: ignore[reportMissingImports]
        ArtifactVerificationError,
        archive_member_manifest,
        artifact_name_findings,
        read_archive_members,
        sha256_file,
        verify_sdist_source_correspondence,
        verify_wheel_source_correspondence,
    )


PROVENANCE_SCHEMA_VERSION = "scope-recall.build-provenance.v1"
DEFAULT_OUTPUT_ROOT = Path(".execution/evidence")
BUILD_TIMEOUT_SECONDS = 300
INSTALL_TIMEOUT_SECONDS = 300
SDIST_TEST_TIMEOUT_SECONDS = 300


class ReleaseCandidateBuildError(RuntimeError):
    """Raised when a candidate cannot be proven from one clean source epoch."""


def _load_script(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ReleaseCandidateBuildError(f"cannot load release helper: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            detail = getattr(exc, "stdout", "") or ""
            log_path.write_text(str(detail), encoding="utf-8", errors="replace")
        raise ReleaseCandidateBuildError(
            f"command failed before completion: {type(exc).__name__}"
        ) from exc
    duration = time.monotonic() - started
    finished_at = datetime.now(timezone.utc).isoformat()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout or "", encoding="utf-8", newline="\n")
    if completed.returncode != 0:
        raise ReleaseCandidateBuildError(
            f"command exited {completed.returncode}: {Path(command[0]).name}"
        )
    return {
        "exit_code": completed.returncode,
        "duration_seconds": round(duration, 3),
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _repository_identity(candidate: ModuleType, root: Path) -> dict[str, object]:
    identity = candidate._repository_identity(root, require_clean=True)
    if not isinstance(identity, dict):
        raise ReleaseCandidateBuildError("repository identity helper returned invalid data")
    return identity


def _require_expected_source(identity: Mapping[str, object], expected_sha: str) -> None:
    actual = str(identity.get("commit") or "")
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise ReleaseCandidateBuildError("--expected-sha must be a full lowercase Git SHA")
    if actual != expected_sha:
        raise ReleaseCandidateBuildError(
            f"candidate source SHA mismatch: expected {expected_sha}, got {actual}"
        )
    if identity.get("clean") is not True:
        raise ReleaseCandidateBuildError("candidate source worktree is not clean")


def _select_distribution_artifacts(directory: Path, version: str) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    expected_wheel = f"hermes_scope_recall-{version}-py3-none-any.whl"
    expected_sdist = f"hermes_scope_recall-{version}.tar.gz"
    if len(wheels) != 1 or wheels[0].name != expected_wheel:
        raise ReleaseCandidateBuildError(
            f"expected exactly {expected_wheel}, found {[path.name for path in wheels]}"
        )
    if len(sdists) != 1 or sdists[0].name != expected_sdist:
        raise ReleaseCandidateBuildError(
            f"expected exactly {expected_sdist}, found {[path.name for path in sdists]}"
        )
    return wheels[0], sdists[0]


def _normalized_build_command() -> list[str]:
    return [
        "python",
        "-m",
        "build",
        "--no-isolation",
        "--wheel",
        "--sdist",
        "--outdir",
        "<isolated-build-dir>",
    ]


def _blocking_scan(scan: Mapping[str, object]) -> bool:
    return any(bool(value) for value in scan.values())


def _verify_artifacts(
    *,
    release_check: ModuleType,
    source_manifest: Mapping[str, object],
    wheel: Path,
    sdist: Path,
    version: str,
    evidence_dir: Path,
) -> dict[str, object]:
    wheel_members = read_archive_members(wheel)
    sdist_members = read_archive_members(sdist)
    sdist_root = f"hermes_scope_recall-{version}"
    wheel_names = set(wheel_members)
    sdist_names = set(sdist_members)
    wheel_missing = sorted(set(release_check.REQUIRED_WHEEL) - wheel_names)
    sdist_missing = release_check.missing_sdist_members(sdist_names)
    forbidden = {
        "wheel": release_check.forbidden_distribution_entries(wheel_names),
        "sdist": release_check.forbidden_distribution_entries(sdist_names),
    }
    allowed_sdist_tests = sorted(release_check.REQUIRED_SOURCE_RESTORE_SDIST_TESTS)
    name_findings = {
        "wheel": artifact_name_findings(wheel_members, kind="wheel"),
        "sdist": artifact_name_findings(
            sdist_members,
            kind="sdist",
            sdist_root=sdist_root,
            allowed_sdist_tests=allowed_sdist_tests,
        ),
    }
    content_scan = {
        "wheel": release_check.scan_distribution_artifact(wheel),
        "sdist": release_check.scan_distribution_artifact(sdist),
    }
    correspondence = {
        "wheel": verify_wheel_source_correspondence(wheel_members, source_manifest),
        "sdist": verify_sdist_source_correspondence(
            sdist_members,
            source_manifest,
            expected_root=sdist_root,
        ),
    }
    scan: dict[str, object] = {
        "schema_version": "scope-recall.artifact-scan.v1",
        "wheel_missing_required": wheel_missing,
        "sdist_missing_required": sdist_missing,
        "forbidden_distribution_entries": forbidden,
        "name_policy_findings": name_findings,
        "content_findings": content_scan,
        "source_correspondence": correspondence,
    }
    _write_json(evidence_dir / "ARTIFACT_MEMBERS_WHEEL.json", archive_member_manifest(wheel))
    _write_json(evidence_dir / "ARTIFACT_MEMBERS_SDIST.json", archive_member_manifest(sdist))
    _write_json(evidence_dir / "ARTIFACT_SCAN.json", scan)
    if (
        wheel_missing
        or sdist_missing
        or any(forbidden.values())
        or any(name_findings.values())
        or any(_blocking_scan(item) for item in content_scan.values())
    ):
        raise ReleaseCandidateBuildError("distribution artifact policy scan failed")
    return scan


def _venv_python(venv_root: Path) -> Path:
    return (
        venv_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_root / "bin" / "python"
    )


def _isolated_environment(
    boundary: Path,
    *,
    active_hermes_home: Path | None = None,
) -> dict[str, str]:
    boundary.mkdir(parents=True, exist_ok=True)
    home = boundary / "home"
    hermes_home = boundary / "hermes-home"
    values = {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": boundary / "appdata",
        "LOCALAPPDATA": boundary / "local-appdata",
        "TEMP": boundary / "temp",
        "TMP": boundary / "temp",
        "XDG_CONFIG_HOME": boundary / "xdg-config",
        "XDG_CACHE_HOME": boundary / "xdg-cache",
        "HERMES_HOME": hermes_home,
        "SCOPE_RECALL_DB": boundary / "truth.sqlite3",
        "SCOPE_RECALL_LOG_DIR": boundary / "logs",
        "SCOPE_RECALL_LEASE_DIR": boundary / "leases",
        "SCOPE_RECALL_PLUGIN_DIR": hermes_home / "plugins" / "scope-recall",
    }
    validate_execution_boundary(
        isolated_root=boundary,
        targets=values,
        active_hermes_home=active_hermes_home or ambient_active_hermes_home(),
    )
    for name, path in values.items():
        if name in {"SCOPE_RECALL_DB", "SCOPE_RECALL_PLUGIN_DIR"}:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({key: str(value) for key, value in values.items()})
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


_INSTALL_SMOKE = r"""
import importlib.metadata
import json
import os
from pathlib import Path
import scope_recall
from scope_recall import installer

assert importlib.metadata.version("hermes-scope-recall") == "2.0.0"
home = Path(os.environ["HERMES_HOME"])
installed = installer.install(home)
verified = installer.verify(home)
assert installed["ok"] is True, installed
assert verified["ok"] is True, verified
plugin_dir = home / "plugins" / "scope-recall"
assert (plugin_dir / "plugin.yaml").is_file()
assert (plugin_dir / "scripts" / "doctor.py").is_file()
print(json.dumps({"ok": True, "plugin_dir": str(plugin_dir)}, sort_keys=True))
"""


def _verify_install(
    *,
    artifact: Path,
    kind: str,
    workspace: Path,
    evidence_dir: Path,
    active_hermes_home: Path,
) -> tuple[Path, dict[str, object]]:
    venv_root = workspace / f"venv-{kind}"
    create_log = evidence_dir / f"INSTALL_{kind.upper()}_VENV.log"
    install_log = evidence_dir / f"INSTALL_{kind.upper()}.log"
    smoke_log = evidence_dir / f"INSTALL_{kind.upper()}_SMOKE.log"
    cli_log = evidence_dir / f"INSTALL_{kind.upper()}_CLI.log"
    boundary = workspace / f"boundary-{kind}"
    env = _isolated_environment(
        boundary,
        active_hermes_home=active_hermes_home,
    )
    stages: dict[str, object] = {}
    stages["venv"] = _run(
        [sys.executable, "-m", "venv", str(venv_root)],
        cwd=workspace,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=env,
        log_path=create_log,
    )
    python = _venv_python(venv_root)
    stages["install"] = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"{artifact}[dev]" if kind == "sdist" else str(artifact),
        ],
        cwd=workspace,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=env,
        log_path=install_log,
    )
    stages["import_version_installer"] = _run(
        [str(python), "-c", _INSTALL_SMOKE],
        cwd=workspace,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=env,
        log_path=smoke_log,
    )
    stages["cli"] = _run(
        [str(python), "-m", "scope_recall.cli", "--help"],
        cwd=workspace,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=env,
        log_path=cli_log,
    )
    smoke_payload = json.loads(smoke_log.read_text(encoding="utf-8"))
    plugin_dir = Path(str(smoke_payload["plugin_dir"]))
    doctor_log = evidence_dir / f"INSTALL_{kind.upper()}_DOCTOR.log"
    stages["doctor"] = _run(
        [
            str(python),
            str(plugin_dir / "scripts" / "doctor.py"),
            "--json",
            "--source-root",
            str(plugin_dir),
        ],
        cwd=workspace,
        timeout=INSTALL_TIMEOUT_SECONDS,
        env=env,
        log_path=doctor_log,
    )
    doctor = json.loads(doctor_log.read_text(encoding="utf-8"))
    if (
        doctor.get("ok") is not True
        or doctor.get("schema_version") != "doctor_report.v1"
        or doctor.get("source", {}).get("pyproject_version") != "2.0.0"
    ):
        raise ReleaseCandidateBuildError(f"{kind} installed Doctor verification failed")
    return python, stages


def _materialize_sdist(members: Mapping[str, bytes], destination: Path) -> Path:
    roots = {PurePosixPath(name).parts[0] for name in members}
    if len(roots) != 1:
        raise ReleaseCandidateBuildError("sdist does not have one archive root")
    for name, content in members.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return destination / next(iter(roots))


def _run_sdist_tests(
    *,
    python: Path,
    sdist: Path,
    release_check: ModuleType,
    workspace: Path,
    evidence_dir: Path,
    active_hermes_home: Path,
) -> list[dict[str, object]]:
    extracted = _materialize_sdist(
        read_archive_members(sdist),
        workspace / "sdist-source",
    )
    test_paths = sorted(
        path
        for path in release_check.REQUIRED_SOURCE_RESTORE_SDIST_TESTS
        if Path(path).name.startswith("test_")
    )
    env = _isolated_environment(
        workspace / "boundary-sdist-tests",
        active_hermes_home=active_hermes_home,
    )
    receipts: list[dict[str, object]] = []
    for index, relative in enumerate(test_paths, 1):
        log = evidence_dir / "sdist-tests" / f"{index:02d}-{Path(relative).stem}.log"
        result = _run(
            [
                str(python),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(workspace / "sdist-pytest-temp" / f"module-{index:02d}"),
                relative,
            ],
            cwd=extracted,
            timeout=SDIST_TEST_TIMEOUT_SECONDS,
            env=env,
            log_path=log,
        )
        receipts.append(
            {
                "module": relative,
                "timeout_seconds": SDIST_TEST_TIMEOUT_SECONDS,
                **result,
            }
        )
    _write_json(evidence_dir / "SDIST_TEST_STAGES.json", receipts)
    return receipts


def build_release_candidate(
    *,
    root: Path,
    expected_sha: str,
    output_root: Path,
    active_hermes_home: Path,
    hermes_root: Path | None = None,
    ci_run_ids: Sequence[str] = (),
) -> Path:
    resolved = root.resolve(strict=True)
    candidate = _load_script(
        resolved / "scripts" / "report.candidate_manifest.py",
        "scope_recall_candidate_manifest_build",
    )
    release_check = _load_script(
        resolved / "scripts" / "check.release.py",
        "scope_recall_release_check_build",
    )
    identity = _repository_identity(candidate, resolved)
    _require_expected_source(identity, expected_sha)
    source_manifest = candidate.source_manifest(resolved)
    project = tomllib.loads((resolved / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    build_backend = str(project["build-system"]["build-backend"])

    final_root = output_root if output_root.is_absolute() else resolved / output_root
    final_root = final_root.resolve(strict=False)
    final_dir = final_root / expected_sha
    if final_dir.exists():
        raise ReleaseCandidateBuildError(
            f"refusing to overwrite existing candidate evidence: {expected_sha}"
        )
    final_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{expected_sha}.",
        dir=final_root,
    ) as staging_text, tempfile.TemporaryDirectory(
        prefix="scope.recall.candidate.build."
    ) as workspace_text:
        staging = Path(staging_text)
        workspace = Path(workspace_text)
        build_dir = workspace / "dist"
        build_dir.mkdir()
        build_environment = _isolated_environment(
            workspace / "boundary-build",
            active_hermes_home=active_hermes_home,
        )
        stages: dict[str, object] = {}
        build_command = [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(build_dir),
        ]
        stages["build"] = _run(
            build_command,
            cwd=resolved,
            timeout=BUILD_TIMEOUT_SECONDS,
            env=build_environment,
            log_path=staging / "BUILD.log",
        )
        built_wheel, built_sdist = _select_distribution_artifacts(build_dir, version)
        artifact_dir = staging / "artifacts"
        artifact_dir.mkdir()
        wheel = artifact_dir / built_wheel.name
        sdist = artifact_dir / built_sdist.name
        shutil.copy2(built_wheel, wheel)
        shutil.copy2(built_sdist, sdist)

        stages["artifact_verification"] = _verify_artifacts(
            release_check=release_check,
            source_manifest=source_manifest,
            wheel=wheel,
            sdist=sdist,
            version=version,
            evidence_dir=staging,
        )
        _, wheel_install = _verify_install(
            artifact=wheel,
            kind="wheel",
            workspace=workspace,
            evidence_dir=staging,
            active_hermes_home=active_hermes_home,
        )
        sdist_python, sdist_install = _verify_install(
            artifact=sdist,
            kind="sdist",
            workspace=workspace,
            evidence_dir=staging,
            active_hermes_home=active_hermes_home,
        )
        sdist_tests = _run_sdist_tests(
            python=sdist_python,
            sdist=sdist,
            release_check=release_check,
            workspace=workspace,
            evidence_dir=staging,
            active_hermes_home=active_hermes_home,
        )
        stages["install_verification"] = {
            "wheel": wheel_install,
            "sdist": sdist_install,
            "sdist_test_modules": sdist_tests,
        }
        wheel_members = archive_member_manifest(wheel)
        sdist_members = archive_member_manifest(sdist)
        provenance: dict[str, object] = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "source_commit": identity["commit"],
            "source_tree": identity["tree"],
            "source_manifest_sha256": source_manifest["manifest_sha256"],
            "source_dirty": False,
            "python_version": sys.version.split()[0],
            "build_backend": build_backend,
            "build_command": _normalized_build_command(),
            "wheel": {
                "name": wheel.name,
                "relative_path": f"artifacts/{wheel.name}",
                "sha256": sha256_file(wheel),
                "member_manifest_sha256": wheel_members[
                    "member_manifest_sha256"
                ],
            },
            "sdist": {
                "name": sdist.name,
                "relative_path": f"artifacts/{sdist.name}",
                "sha256": sha256_file(sdist),
                "member_manifest_sha256": sdist_members[
                    "member_manifest_sha256"
                ],
            },
            "install_verification": {"wheel": "passed", "sdist": "passed"},
        }
        provenance_path = staging / "BUILD_PROVENANCE.json"
        _write_json(provenance_path, provenance)
        wheel_provenance = provenance.get("wheel")
        if not isinstance(wheel_provenance, dict):
            raise ReleaseCandidateBuildError("wheel provenance is missing")
        wheel_doctor = wheel_install.get("doctor")
        if not isinstance(wheel_doctor, dict):
            raise ReleaseCandidateBuildError("wheel Doctor stage receipt is missing")
        _write_json(
            staging / "DOCTOR.json",
            {
                "schema_version": "scope-recall.doctor-receipt.v1",
                "source_commit": identity["commit"],
                "source_tree": identity["tree"],
                "artifact_sha256": wheel_provenance["sha256"],
                "started_at": wheel_doctor["started_at"],
                "finished_at": wheel_doctor["finished_at"],
                "command": [
                    "python",
                    "<installed-wheel>/scripts/doctor.py",
                    "--json",
                    "--source-root",
                    "<installed-wheel>",
                ],
                "exit_code": wheel_doctor["exit_code"],
                "environment_boundary": {
                    "hermes_home_kind": "isolated",
                    "database_kind": "isolated",
                    "active_instance_touched": False,
                },
                "result": "passed",
                "raw_log_sha256": sha256_file(
                    staging / "INSTALL_WHEEL_DOCTOR.log"
                ),
            },
        )
        manifest = candidate.build_candidate_manifest(
            resolved,
            provenance_path=provenance_path,
            hermes_root=hermes_root,
            ci_run_ids=ci_run_ids,
            expected_version=version,
            require_clean=True,
        )
        candidate.write_manifest(
            resolved,
            staging / "CANDIDATE_MANIFEST.json",
            manifest,
        )
        _write_json(
            staging / "SOURCE_IDENTITY.json",
            {
                "schema_version": "scope-recall.source-identity.v1",
                "source_commit": identity["commit"],
                "source_tree": identity["tree"],
                "source_dirty": False,
                "source_manifest_sha256": source_manifest["manifest_sha256"],
                "source_file_count": source_manifest["file_count"],
            },
        )
        _write_json(staging / "BUILD_STAGES.json", stages)
        staging.rename(final_dir)
    return final_dir


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--active-hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-root", type=Path)
    parser.add_argument("--ci-run-id", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    final_dir = build_release_candidate(
        root=args.root,
        expected_sha=str(args.expected_sha),
        output_root=args.output_root,
        active_hermes_home=args.active_hermes_home,
        hermes_root=args.hermes_root,
        ci_run_ids=tuple(args.ci_run_id),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_commit": str(args.expected_sha),
                "evidence_directory": final_dir.name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ArtifactVerificationError, ReleaseCandidateBuildError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
