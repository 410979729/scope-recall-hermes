#!/usr/bin/env python3
"""Generate final local validation evidence for one built release candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import ModuleType
from typing import Mapping, NamedTuple, Sequence

try:
    from scripts.execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.execution_boundary"}:
        raise
    from execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )


SCHEMA_VERSION = "scope-recall.release-validation.v1"
RECEIPT_SCHEMA_VERSION = "scope-recall.validation-receipt.v1"
FULL_TEST_TIMEOUT_SECONDS = 900
STAGE_TIMEOUT_SECONDS = 300
REHEARSAL_RECEIPTS: dict[str, tuple[str, ...]] = {
    "MIGRATION_N_MINUS_ONE.json": (
        "tests/test_release_candidate_rehearsals.py::test_activity_snapshot_upgrade_preserves_source_and_payload",
        "tests/test_sqlite_backup.py::test_verified_online_backup_healthy_path_records_health_and_logical_equivalence",
        "tests/test_upgrade_compatibility.py::test_n_minus_one_isolation_key_passes_read_only_upgrade_preflight",
    ),
    "MIGRATION_N.json": (
        "tests/test_schema_migrations.py::test_relation_policy_generation_migration_upgrades_pre_0014_in_place",
        "tests/test_fact_executor.py::test_add_applies_all_mandatory_surfaces_in_one_committed_transaction",
    ),
    "DOWNGRADE_N_MINUS_ONE.json": (
        "tests/test_fact_authority_router.py::test_missing_unknown_legacy_aliases_and_projection_marker_never_authorize_claim",
    ),
    "PURGE_RESTORE_REPLAY.json": (
        "tests/test_privacy_purge.py::test_restore_replay_reinstates_deny_before_writer_use",
    ),
    "READONLY_CANARY.json": (
        "tests/test_readonly_follower_tools.py::test_readonly_follower_default_denies_writes_and_unknown_tools",
    ),
    "WRITER_CANARY.json": (
        "tests/test_installer.py::test_installer_runtime_verify_loads_provider_tools_and_schema",
        "tests/test_installer.py::test_installed_plugin_loads_through_hermes_memory_discovery",
    ),
    "ROLLBACK_REHEARSAL.json": (
        "tests/test_installer.py::test_installer_rollback_restores_backup_and_backs_up_current_plugin",
    ),
}
ISOLATION_NODES = (
    "tests/test_execution_boundary.py",
    "tests/test_home_cleanup_receipt.py",
)


class ReleaseValidationError(RuntimeError):
    """Raised when final local validation cannot produce honest evidence."""


class ValidationContext(NamedTuple):
    source_commit: str
    source_tree: str
    wheel_sha256: str
    sdist_sha256: str


def _load_script(path: Path, name: str) -> ModuleType:
    project_root = path.resolve(strict=True).parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseValidationError(f"cannot load validation helper: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(
            f"cannot read validation input {path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError(f"validation input is not an object: {path.name}")
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_sha(provenance: Mapping[str, object], kind: str) -> str:
    raw = provenance.get(kind)
    if not isinstance(raw, dict):
        raise ReleaseValidationError(f"build provenance {kind} is missing")
    value = str(raw.get("sha256") or "")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ReleaseValidationError(f"build provenance {kind} digest is invalid")
    return value


def _validation_context(
    root: Path,
    evidence_dir: Path,
    expected_sha: str,
) -> ValidationContext:
    if evidence_dir.name != expected_sha:
        raise ReleaseValidationError("evidence directory is not the exact SHA directory")
    candidate = _load_script(
        root / "scripts" / "report.candidate_manifest.py",
        "scope_recall_validation_candidate_manifest",
    )
    identity = candidate._repository_identity(root, require_clean=True)
    if not isinstance(identity, dict):
        raise ReleaseValidationError("candidate source identity is invalid")
    commit = str(identity.get("commit") or "")
    tree = str(identity.get("tree") or "")
    if commit != expected_sha:
        raise ReleaseValidationError("candidate source does not match expected SHA")
    source = _load_json(evidence_dir / "SOURCE_IDENTITY.json")
    provenance = _load_json(evidence_dir / "BUILD_PROVENANCE.json")
    if source.get("source_commit") != commit or source.get("source_tree") != tree:
        raise ReleaseValidationError("source identity evidence differs from current source")
    if provenance.get("source_commit") != commit or provenance.get("source_tree") != tree:
        raise ReleaseValidationError("build provenance differs from current source")
    if source.get("source_dirty") is not False or provenance.get("source_dirty") is not False:
        raise ReleaseValidationError("source evidence is not clean")
    return ValidationContext(
        source_commit=commit,
        source_tree=tree,
        wheel_sha256=_artifact_sha(provenance, "wheel"),
        sdist_sha256=_artifact_sha(provenance, "sdist"),
    )


def _isolated_environment(
    boundary: Path,
    *,
    active_hermes_home: Path,
    real_home: Path,
) -> dict[str, str]:
    targets = {
        "HOME": boundary / "user-home",
        "USERPROFILE": boundary / "user-home",
        "APPDATA": boundary / "appdata",
        "LOCALAPPDATA": boundary / "local-appdata",
        "TEMP": boundary / "temp",
        "TMP": boundary / "temp",
        "XDG_CONFIG_HOME": boundary / "xdg-config",
        "XDG_CACHE_HOME": boundary / "xdg-cache",
        "PIP_CACHE_DIR": boundary / "pip-cache",
        "SCOPE_RECALL_TEST_BOUNDARY_PARENT": boundary / "pytest",
        "HERMES_HOME": boundary / "hermes-home",
        "SCOPE_RECALL_DB": boundary / "truth" / "memory.sqlite3",
        "SCOPE_RECALL_LOG_DIR": boundary / "logs",
        "SCOPE_RECALL_LEASE_DIR": boundary / "leases",
        "SCOPE_RECALL_PLUGIN_DIR": boundary
        / "hermes-home"
        / "plugins"
        / "scope-recall",
    }
    validate_execution_boundary(
        isolated_root=boundary,
        targets=targets,
        active_hermes_home=active_hermes_home,
        real_home=real_home,
    )
    for name, path in targets.items():
        if name in {"SCOPE_RECALL_DB", "SCOPE_RECALL_PLUGIN_DIR"}:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update({name: str(path) for name, path in targets.items()})
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "SCOPE_RECALL_ACTIVE_HERMES_HOME": str(active_hermes_home),
            "SCOPE_RECALL_REAL_HOME": str(real_home),
        }
    )
    return environment


def _run(
    command: Sequence[str],
    *,
    display_command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    log_path: Path,
    ledger: list[dict[str, object]],
) -> dict[str, object]:
    started_at = _utc_now()
    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
            creationflags=creationflags,
        )
        output = result.stdout or ""
        exit_code = int(result.returncode)
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "")
        exit_code = 124
    except OSError as exc:
        output = f"{type(exc).__name__}\n"
        exit_code = 125
    finished_at = _utc_now()
    duration = round(time.monotonic() - started, 3)
    log_path.write_text(output, encoding="utf-8", newline="\n")
    stage = {
        "command": list(display_command),
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "log": log_path.name,
        "log_sha256": _sha256(log_path),
    }
    ledger.append(stage)
    if exit_code != 0:
        raise ReleaseValidationError(
            f"validation command failed with exit code {exit_code}: {display_command[0]}"
        )
    return stage


def _receipt(
    context: ValidationContext,
    *,
    stage: Mapping[str, object],
    command: Sequence[str],
    database_kind: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_commit": context.source_commit,
        "source_tree": context.source_tree,
        "artifact_sha256": context.wheel_sha256,
        "started_at": stage["started_at"],
        "finished_at": stage["finished_at"],
        "command": list(command),
        "exit_code": stage["exit_code"],
        "environment_boundary": {
            "hermes_home_kind": "isolated",
            "database_kind": database_kind,
            "active_instance_touched": False,
        },
        "result": "passed",
        "raw_log_sha256": stage.get("log_sha256", "not-applicable"),
    }
    if details:
        payload["details"] = dict(details)
    return payload


def _run_pytest_receipt(
    *,
    root: Path,
    staging: Path,
    environment: Mapping[str, str],
    context: ValidationContext,
    receipt_name: str,
    node_ids: Sequence[str],
    ledger: list[dict[str, object]],
) -> None:
    stem = Path(receipt_name).stem
    log_path = staging / f"{stem}.log"
    actual = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--basetemp",
        str(staging / f"pytest-{stem.lower()}"),
        *node_ids,
    ]
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--basetemp",
        f"<isolated>/{stem.lower()}",
        *node_ids,
    ]
    stage = _run(
        actual,
        display_command=display,
        cwd=root,
        environment=environment,
        timeout_seconds=STAGE_TIMEOUT_SECONDS,
        log_path=log_path,
        ledger=ledger,
    )
    _write_json(
        staging / receipt_name,
        _receipt(
            context,
            stage=stage,
            command=display,
            database_kind="fixture-copy",
            details={"node_ids": list(node_ids)},
        ),
    )


def _run_full_suite(
    *,
    root: Path,
    staging: Path,
    environment: dict[str, str],
    context: ValidationContext,
    ledger: list[dict[str, object]],
) -> None:
    junit = staging / "PYTEST_JUNIT.xml"
    honesty = staging / "PYTEST_SKIP_REPORT.json"
    log = staging / "PYTEST_STDOUT.log"
    environment.update(
        {
            "SCOPE_RECALL_TEST_HONESTY_OUTPUT": str(honesty),
            "SCOPE_RECALL_SOURCE_COMMIT": context.source_commit,
            "SCOPE_RECALL_SOURCE_TREE": context.source_tree,
            "SCOPE_RECALL_TEST_TIMEOUTS_JSON": json.dumps(
                [
                    {
                        "stage": "full_pytest",
                        "seconds": FULL_TEST_TIMEOUT_SECONDS,
                        "reason": "bounded final local Windows suite ceiling",
                    }
                ]
            ),
        }
    )
    environment.setdefault("SCOPE_RECALL_FIRST_FAILURE_FIXES_JSON", "[]")
    actual = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.release_test_honesty",
        "-ra",
        f"--junitxml={junit}",
        "--basetemp",
        str(staging / "pytest-full"),
    ]
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.release_test_honesty",
        "-ra",
        "--junitxml=<evidence>/PYTEST_JUNIT.xml",
        "--basetemp=<isolated>/pytest-full",
    ]
    _run(
        actual,
        display_command=display,
        cwd=root,
        environment=environment,
        timeout_seconds=FULL_TEST_TIMEOUT_SECONDS,
        log_path=log,
        ledger=ledger,
    )
    if not junit.is_file() or not honesty.is_file():
        raise ReleaseValidationError("full pytest did not produce JUnit and honesty evidence")
    evidence = _load_script(
        root / "scripts" / "report.evidence_package.py",
        "scope_recall_validation_evidence_contract",
    )
    honesty_payload = _load_json(honesty)
    evidence.validate_test_honesty(honesty_payload)
    if honesty_payload.get("source_commit") != context.source_commit:
        raise ReleaseValidationError("pytest honesty source commit mismatch")
    if honesty_payload.get("source_tree") != context.source_tree:
        raise ReleaseValidationError("pytest honesty source tree mismatch")


def _run_static_validation(
    *,
    root: Path,
    staging: Path,
    environment: Mapping[str, str],
    ledger: list[dict[str, object]],
) -> None:
    commands = (
        (
            [sys.executable, "-B", "-m", "ruff", "check", "--no-cache", "."],
            ["python", "-B", "-m", "ruff", "check", "--no-cache", "."],
            staging / "RUFF.log",
        ),
        (
            [sys.executable, "-B", "-m", "pyright"],
            ["python", "-B", "-m", "pyright"],
            staging / "PYRIGHT.log",
        ),
        (
            ["git", "diff", "--check"],
            ["git", "diff", "--check"],
            staging / "GIT_DIFF_CHECK.log",
        ),
    )
    for actual, display, log in commands:
        _run(
            actual,
            display_command=display,
            cwd=root,
            environment=environment,
            timeout_seconds=STAGE_TIMEOUT_SECONDS,
            log_path=log,
            ledger=ledger,
        )


def _active_isolation_evidence(
    *,
    root: Path,
    staging: Path,
    environment: Mapping[str, str],
    context: ValidationContext,
    active_hermes_home: Path,
    hermes_0191_source: Path,
    hermes_0206_source: Path,
    accidental_home_path: Path,
    quarantine_path: Path,
    ledger: list[dict[str, object]],
) -> None:
    log = staging / "ACTIVE_ISOLATION.log"
    display = [
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        *ISOLATION_NODES,
    ]
    stage = _run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--basetemp",
            str(staging / "pytest-active-isolation"),
            *ISOLATION_NODES,
        ],
        display_command=display,
        cwd=root,
        environment=environment,
        timeout_seconds=STAGE_TIMEOUT_SECONDS,
        log_path=log,
        ledger=ledger,
    )
    probe = _load_script(
        root / "scripts" / "probe.hermes_compatibility.py",
        "scope_recall_validation_hermes_probe",
    )
    probe_0191 = probe.build_probe_receipt(
        candidate_source=root,
        hermes_source=hermes_0191_source,
        expected_hermes_version="0.19.1",
        active_hermes_home=active_hermes_home,
    )
    probe_0206 = probe.build_probe_receipt(
        candidate_source=root,
        hermes_source=hermes_0206_source,
        expected_hermes_version="0.20.6",
        active_hermes_home=active_hermes_home,
    )
    if probe_0191.get("result") != "compatible":
        raise ReleaseValidationError("pinned Hermes 0.19.1 probe is not compatible")
    if probe_0206.get("result") not in {"compatible", "incompatible"}:
        raise ReleaseValidationError("Hermes 0.20.6 probe is not conclusively classified")
    if probe_0206.get("support_matrix_changed") is not False:
        raise ReleaseValidationError("Hermes 0.20.6 probe changed support policy")
    _write_json(staging / "HERMES_COMPATIBILITY_PROBE.0.19.1.json", probe_0191)
    _write_json(staging / "HERMES_COMPATIBILITY_PROBE.0.20.6.json", probe_0206)
    cleanup = _load_script(
        root / "scripts" / "report.home_cleanup.py",
        "scope_recall_validation_home_cleanup",
    )
    cleanup_receipt = cleanup.build_cleanup_receipt(
        accidental_path=accidental_home_path,
        active_plugin_path=active_hermes_home / "plugins" / "scope-recall",
        quarantine_path=quarantine_path,
    )
    _write_json(staging / "ACCIDENTAL_HOME_CLEANUP_RECEIPT.json", cleanup_receipt)
    combined_stage = dict(stage)
    combined_stage["finished_at"] = _utc_now()
    _write_json(
        staging / "ACTIVE_ISOLATION.json",
        _receipt(
            context,
            stage=combined_stage,
            command=[
                "release-validation",
                "active-isolation",
                "pytest-boundary",
                "hermes-0.19.1-probe",
                "hermes-0.20.6-probe",
                "home-cleanup-inventory",
            ],
            database_kind="fixture-copy",
            details={
                "hermes_0_19_1": probe_0191["result"],
                "hermes_0_20_6": probe_0206["result"],
                "support_matrix_changed": False,
                "home_cleanup_deletion_performed": False,
            },
        ),
    )


def _repository_evidence(
    *,
    root: Path,
    staging: Path,
    context: ValidationContext,
) -> None:
    census_module = _load_script(
        root / "scripts" / "report.repository_census.py",
        "scope_recall_validation_repository_census",
    )
    started = _utc_now()
    census = census_module.build_census(root, tracked_only=True)
    delta = census_module.repository_delta(root)
    finished = _utc_now()
    stage = {
        "started_at": started,
        "finished_at": finished,
        "exit_code": 0,
        "log_sha256": "not-applicable",
    }
    _write_json(
        staging / "REPOSITORY_CENSUS.json",
        _receipt(
            context,
            stage=stage,
            command=[
                "python",
                "scripts/report.repository_census.py",
                "--tracked-only",
            ],
            database_kind="not-used",
            details={"census": census},
        ),
    )
    _write_json(
        staging / "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
        _receipt(
            context,
            stage=stage,
            command=[
                "git",
                "diff",
                "--name-status",
                "-z",
                "-M",
                f"{census_module.PUBLIC_BASE_COMMIT}...HEAD",
            ],
            database_kind="not-used",
            details={"delta": delta},
        ),
    )


def run_release_validation(
    *,
    root: Path,
    expected_sha: str,
    evidence_dir: Path,
    active_hermes_home: Path,
    hermes_0191_source: Path,
    hermes_0206_source: Path,
    accidental_home_path: Path,
    quarantine_path: Path,
) -> Path:
    resolved = root.resolve(strict=True)
    evidence = evidence_dir.resolve(strict=True)
    active = active_hermes_home.resolve(strict=False)
    real_home = Path.home().resolve(strict=False)
    context = _validation_context(resolved, evidence, expected_sha)
    validation_targets = {
        "TEST_COMMANDS.json",
        "PYTEST_JUNIT.xml",
        "PYTEST_STDOUT.log",
        "PYTEST_SKIP_REPORT.json",
        "RUFF.log",
        "PYRIGHT.log",
        "MIGRATION_N_MINUS_ONE.json",
        "MIGRATION_N.json",
        "DOWNGRADE_N_MINUS_ONE.json",
        "PURGE_RESTORE_REPLAY.json",
        "READONLY_CANARY.json",
        "WRITER_CANARY.json",
        "ROLLBACK_REHEARSAL.json",
        "ACTIVE_ISOLATION.json",
        "REPOSITORY_CENSUS.json",
        "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
    }
    existing = sorted(name for name in validation_targets if (evidence / name).exists())
    if existing:
        raise ReleaseValidationError(
            f"refusing to overwrite existing final validation evidence: {', '.join(existing)}"
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".validation.{expected_sha}.",
            dir=evidence.parent,
        )
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="scope.recall.release-validation."
        ) as boundary_text:
            boundary = Path(boundary_text)
            environment = _isolated_environment(
                boundary,
                active_hermes_home=active,
                real_home=real_home,
            )
            ledger: list[dict[str, object]] = []
            _run_full_suite(
                root=resolved,
                staging=staging,
                environment=environment,
                context=context,
                ledger=ledger,
            )
            _run_static_validation(
                root=resolved,
                staging=staging,
                environment=environment,
                ledger=ledger,
            )
            for receipt_name, node_ids in REHEARSAL_RECEIPTS.items():
                _run_pytest_receipt(
                    root=resolved,
                    staging=staging,
                    environment=environment,
                    context=context,
                    receipt_name=receipt_name,
                    node_ids=node_ids,
                    ledger=ledger,
                )
            _active_isolation_evidence(
                root=resolved,
                staging=staging,
                environment=environment,
                context=context,
                active_hermes_home=active,
                hermes_0191_source=hermes_0191_source,
                hermes_0206_source=hermes_0206_source,
                accidental_home_path=accidental_home_path,
                quarantine_path=quarantine_path,
                ledger=ledger,
            )
            _repository_evidence(root=resolved, staging=staging, context=context)
            _write_json(
                staging / "TEST_COMMANDS.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_commit": context.source_commit,
                    "source_tree": context.source_tree,
                    "active_instance_touched": False,
                    "commands": ledger,
                },
            )
    except Exception as exc:
        raise ReleaseValidationError(
            f"{exc}; raw validation logs retained in {staging.name}"
        ) from exc
    for path in sorted(staging.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            raise ReleaseValidationError(
                f"unexpected non-file validation output: {path.name}"
            )
        target = evidence / path.name
        if target.exists():
            raise ReleaseValidationError(
                f"refusing to overwrite evidence output: {path.name}"
            )
        path.replace(target)
    staging.rmdir()
    evidence_module = _load_script(
        resolved / "scripts" / "report.evidence_package.py",
        "scope_recall_validation_evidence_index",
    )
    index = evidence_module.build_evidence_index(evidence, expected_sha=expected_sha)
    return evidence_module.write_evidence_index(evidence, index)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--active-hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-0-19-1-source", type=Path, required=True)
    parser.add_argument("--hermes-0-20-6-source", type=Path, required=True)
    parser.add_argument("--accidental-home-path", type=Path)
    parser.add_argument("--quarantine-path", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve(strict=True)
    expected_sha = str(args.expected_sha)
    evidence = (
        args.evidence_dir
        if args.evidence_dir is not None
        else root / ".execution" / "evidence" / expected_sha
    )
    active = args.active_hermes_home.resolve(strict=False)
    index = run_release_validation(
        root=root,
        expected_sha=expected_sha,
        evidence_dir=evidence,
        active_hermes_home=active,
        hermes_0191_source=args.hermes_0_19_1_source,
        hermes_0206_source=args.hermes_0_20_6_source,
        accidental_home_path=args.accidental_home_path
        or Path.home() / "plugins" / "scope-recall",
        quarantine_path=args.quarantine_path
        or active / "quarantine" / "scope-recall",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_commit": expected_sha,
                "evidence_index_sha256": _sha256(index),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
