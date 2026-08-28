#!/usr/bin/env python3
"""Validate and index one raw, source-bound release-candidate evidence package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Mapping, Sequence


SCHEMA_VERSION = "scope-recall.evidence-index.v1"
SHAREABLE_SCHEMA_VERSION = "scope-recall.shareable-evidence-index.v1"
EVIDENCE_SCAN_SCHEMA_VERSION = "scope-recall.evidence-content-scan.v1"
TEST_HONESTY_SCHEMA_VERSION = "scope-recall.test-honesty.v1"
REQUIRED_INPUT_FILES = (
    "SOURCE_IDENTITY.json",
    "BUILD_PROVENANCE.json",
    "CANDIDATE_MANIFEST.json",
    "ARTIFACT_MEMBERS_WHEEL.json",
    "ARTIFACT_MEMBERS_SDIST.json",
    "ARTIFACT_SCAN.json",
    "TEST_COMMANDS.json",
    "PYTEST_JUNIT.xml",
    "PYTEST_STDOUT.log",
    "PYTEST_SKIP_REPORT.json",
    "RUFF.log",
    "PYRIGHT.log",
    "DOCTOR.json",
    "MIGRATION_N_MINUS_ONE.json",
    "MIGRATION_N.json",
    "DOWNGRADE_N_MINUS_ONE.json",
    "PURGE_RESTORE_REPLAY.json",
    "READONLY_CANARY.json",
    "WRITER_CANARY.json",
    "ROLLBACK_REHEARSAL.json",
    "INSTALL_CANDIDATE_RECEIPT.json",
    "INSTALL_N_MINUS_ONE_RECEIPT.json",
    "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
    "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
    "ACTIVE_ISOLATION.json",
    "REPOSITORY_CENSUS.json",
    "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
)
INSTALL_RECEIPT_FILES = (
    "INSTALL_CANDIDATE_RECEIPT.json",
    "INSTALL_N_MINUS_ONE_RECEIPT.json",
)
SHAREABLE_EXPLICIT_FILES = frozenset(
    name
    for name in REQUIRED_INPUT_FILES
    if Path(name).suffix == ".json"
) | frozenset(
    {
        "ACCIDENTAL_HOME_CLEANUP_RECEIPT.json",
        "BUILD_STAGES.json",
        "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
        "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
        "SDIST_TEST_STAGES.json",
        "REMOTE_CI_BINDING.json",
    }
)
PRIVATE_PATH_PATTERNS = (
    re.compile(
        r"(?i)(?:\\\\\?\\)?[a-z]:[\\/]+(?:Users|Agents)[\\/]+[^\s\"']+"
    ),
    re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|tmp)/[^\s\"']+"),
)
SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret)"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9_./+\-=]{12,}"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
RECEIPT_FILES = (
    "DOCTOR.json",
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
)


class EvidencePackageError(RuntimeError):
    """Raised when raw evidence is incomplete, inconsistent, or unbound."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidencePackageError(
            f"invalid JSON evidence file {path.name}: {type(exc).__name__}"
        ) from exc


def _load_object(path: Path) -> dict[str, object]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise EvidencePackageError(f"JSON evidence root must be an object: {path.name}")
    return payload


def _require_sha(value: object, *, field: str) -> str:
    rendered = str(value or "")
    if len(rendered) != 64 or any(ch not in "0123456789abcdef" for ch in rendered):
        raise EvidencePackageError(f"{field} must be a lowercase SHA-256")
    return rendered


def _require_git_oid(value: object, *, field: str) -> str:
    rendered = str(value or "")
    if len(rendered) not in {40, 64} or any(
        ch not in "0123456789abcdef" for ch in rendered
    ):
        raise EvidencePackageError(f"{field} must be a lowercase Git object ID")
    return rendered


def _require_git_sha(value: object, *, field: str) -> str:
    rendered = str(value or "")
    if len(rendered) != 40 or any(ch not in "0123456789abcdef" for ch in rendered):
        raise EvidencePackageError(f"{field} must be a full lowercase Git SHA")
    return rendered


def validate_test_honesty(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate exact final test accounting without allowing hidden green paths."""

    if payload.get("schema_version") != TEST_HONESTY_SCHEMA_VERSION:
        raise EvidencePackageError("unsupported test honesty schema")
    numeric_fields = (
        "collected",
        "passed",
        "failed",
        "errors",
        "xfail",
        "xpass",
        "rerun_count",
    )
    counts: dict[str, int] = {}
    for field in numeric_fields:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidencePackageError(f"test honesty {field} must be non-negative")
        counts[field] = value
    skipped = payload.get("skipped")
    if not isinstance(skipped, list):
        raise EvidencePackageError("test honesty skipped must be an array")
    node_ids: list[str] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            raise EvidencePackageError("each skipped test must be an object")
        node_id = str(entry.get("node_id") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if not node_id or not reason:
            raise EvidencePackageError("every skipped test requires node_id and reason")
        node_ids.append(node_id)
    if len(node_ids) != len(set(node_ids)):
        raise EvidencePackageError("test honesty contains duplicate skipped node IDs")
    timeout_overrides = payload.get("timeout_overrides")
    first_failure_fixes = payload.get("first_failure_fixes")
    if not isinstance(timeout_overrides, list):
        raise EvidencePackageError("timeout_overrides must be an array")
    if not isinstance(first_failure_fixes, list):
        raise EvidencePackageError("first_failure_fixes must be an array")
    first_failure_status = str(
        payload.get("first_failure_fixes_status") or ""
    )
    if first_failure_status not in {"not_provided", "declared"}:
        raise EvidencePackageError(
            "first_failure_fixes_status must be not_provided or declared"
        )
    if first_failure_status == "not_provided" and first_failure_fixes:
        raise EvidencePackageError(
            "not_provided first_failure evidence cannot contain declarations"
        )
    for entry in first_failure_fixes:
        if not isinstance(entry, dict):
            raise EvidencePackageError("first_failure_fixes entries must be objects")
        _require_git_sha(
            entry.get("first_failure_commit"),
            field="first_failure_commit",
        )
        _require_git_sha(entry.get("fix_commit"), field="fix_commit")
        if not str(entry.get("node_id") or "").strip():
            raise EvidencePackageError("first_failure_fixes requires node_id")
    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise EvidencePackageError("duration_seconds must be non-negative")
    accounted = counts["passed"] + len(skipped) + counts["xfail"] + counts["xpass"]
    if accounted != counts["collected"]:
        raise EvidencePackageError(
            "test honesty collected count does not equal passed/skipped/xfail/xpass"
        )
    if counts["failed"] or counts["errors"]:
        raise EvidencePackageError("final test evidence contains failures or errors")
    if counts["rerun_count"]:
        raise EvidencePackageError("final test evidence used retries or reruns")
    return {
        "collected": counts["collected"],
        "passed": counts["passed"],
        "skipped": len(skipped),
        "skipped_node_ids": sorted(node_ids),
        "xfail": counts["xfail"],
        "xpass": counts["xpass"],
        "rerun_count": counts["rerun_count"],
        "timeout_override_count": len(timeout_overrides),
        "duration_seconds": duration,
        "first_failure_fix_count": len(first_failure_fixes),
        "first_failure_fixes_status": first_failure_status,
    }


def _validate_install_receipt(
    name: str,
    payload: Mapping[str, object],
    *,
    source_commit: str,
    source_tree: str,
    wheel_sha256: str,
) -> None:
    if payload.get("schema_version") != "scope-recall.artifact-install-receipt.v1":
        raise EvidencePackageError(f"{name} install receipt schema mismatch")
    if payload.get("artifact_kind") != "wheel":
        raise EvidencePackageError(f"{name} did not install a wheel")
    _require_sha(payload.get("artifact_sha256"), field=f"{name}.artifact_sha256")
    for field in (
        "environment_id",
        "python_executable_sha256",
        "installed_package_manifest_sha256",
        "environment_distribution_manifest_sha256",
        "record_sha256",
        "direct_url_sha256",
        "venv_stage_sha256",
        "install_stage_sha256",
        "probe_stage_sha256",
    ):
        _require_sha(payload.get(field), field=f"{name}.{field}")
    if payload.get("source_worktree_imported") is not False:
        raise EvidencePackageError(f"{name} imported the source worktree")
    if payload.get("source_worktree_on_sys_path") is not False:
        raise EvidencePackageError(f"{name} exposed the source worktree on sys.path")
    if payload.get("imported_module_path_class") != "isolated-site-packages":
        raise EvidencePackageError(f"{name} import origin is not isolated site-packages")
    if payload.get("result") != "passed":
        raise EvidencePackageError(f"{name} installation did not pass")
    if name == "INSTALL_CANDIDATE_RECEIPT.json":
        if payload.get("source_commit") != source_commit:
            raise EvidencePackageError("candidate install source_commit mismatch")
        if payload.get("source_tree") != source_tree:
            raise EvidencePackageError("candidate install source_tree mismatch")
        if payload.get("artifact_sha256") != wheel_sha256:
            raise EvidencePackageError("candidate install wheel digest mismatch")
    elif payload.get("installed_distribution") != "hermes-scope-recall==1.10.3":
        raise EvidencePackageError("N-1 install is not hermes-scope-recall==1.10.3")


def _validate_install_command_stages(
    root: Path,
    install_receipts: Mapping[str, Mapping[str, object]],
) -> None:
    commands = _load_object(root / "TEST_COMMANDS.json").get("commands")
    if not isinstance(commands, list) or not all(
        isinstance(item, dict) for item in commands
    ):
        raise EvidencePackageError("TEST_COMMANDS install-stage ledger is invalid")
    by_log: dict[str, Mapping[str, object]] = {}
    for item in commands:
        assert isinstance(item, dict)
        log = str(item.get("log") or "")
        if not log:
            continue
        if log in by_log:
            raise EvidencePackageError(f"duplicate TEST_COMMANDS log stage: {log}")
        by_log[log] = item
    for name, receipt in install_receipts.items():
        label = "CANDIDATE" if name.startswith("INSTALL_CANDIDATE") else "N_MINUS_ONE"
        expected = {
            f"INSTALL_{label}_VENV.log": "venv_stage_sha256",
            f"INSTALL_{label}.log": "install_stage_sha256",
            f"INSTALL_{label}_PROBE.log": "probe_stage_sha256",
        }
        for log_name, field in expected.items():
            stage = by_log.get(log_name)
            if stage is None or stage.get("exit_code") != 0:
                raise EvidencePackageError(f"{name} lacks a successful {log_name} stage")
            digest = _require_sha(receipt.get(field), field=f"{name}.{field}")
            if stage.get("log_sha256") != digest:
                raise EvidencePackageError(f"{name} {log_name} ledger digest mismatch")
            log_path = root / log_name
            if not log_path.is_file() or _sha256_file(log_path) != digest:
                raise EvidencePackageError(f"{name} {log_name} file digest mismatch")


def _validated_hermes_identity(
    payload: Mapping[str, object],
    *,
    field: str,
) -> dict[str, object]:
    identity = payload.get(field)
    if not isinstance(identity, dict) or identity.get("clean") is not True:
        raise EvidencePackageError(f"{field} is not a clean Git identity")
    _require_git_oid(identity.get("commit"), field=f"{field}.commit")
    _require_git_oid(identity.get("tree"), field=f"{field}.tree")
    return identity


def _validate_hermes_probes(root: Path) -> dict[str, object]:
    probe_0191 = _load_object(root / "HERMES_COMPATIBILITY_PROBE.0.19.1.json")
    probe_0206 = _load_object(root / "HERMES_COMPATIBILITY_PROBE.0.20.6.json")
    if (
        probe_0191.get("expected_hermes_version") != "0.19.1"
        or probe_0191.get("observed_hermes_version") != "0.19.1"
        or probe_0191.get("result") != "compatible"
        or probe_0191.get("active_instance_touched") is not False
    ):
        raise EvidencePackageError("Hermes 0.19.1 compatibility probe is invalid")
    if (
        probe_0206.get("expected_hermes_version") != "0.20.6"
        or probe_0206.get("observed_hermes_version") != "0.20.6"
        or probe_0206.get("result") not in {"compatible", "incompatible"}
        or probe_0206.get("support_matrix_changed") is not False
        or probe_0206.get("active_instance_touched") is not False
    ):
        raise EvidencePackageError("Hermes 0.20.6 compatibility probe is invalid")
    return _validated_hermes_identity(
        probe_0191,
        field="hermes_source",
    )


def _validate_receipt(
    name: str,
    payload: Mapping[str, object],
    *,
    source_commit: str,
    source_tree: str,
    artifact_hashes: set[str],
    candidate_install_sha256: str,
    candidate_install: Mapping[str, object],
) -> None:
    if not str(payload.get("schema_version") or ""):
        raise EvidencePackageError(f"{name} schema_version is missing")
    if payload.get("source_commit") != source_commit:
        raise EvidencePackageError(f"{name} source_commit mismatch")
    if payload.get("source_tree") != source_tree:
        raise EvidencePackageError(f"{name} source_tree mismatch")
    if payload.get("artifact_sha256") not in artifact_hashes:
        raise EvidencePackageError(f"{name} artifact_sha256 mismatch")
    for field in ("started_at", "finished_at"):
        value = str(payload.get(field) or "")
        if not value:
            raise EvidencePackageError(f"{name} is missing {field}")
    command = payload.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise EvidencePackageError(f"{name} command must be a non-empty string array")
    if payload.get("exit_code") != 0 or payload.get("result") != "passed":
        raise EvidencePackageError(f"{name} is not a passing receipt")
    boundary = payload.get("environment_boundary")
    if not isinstance(boundary, dict):
        raise EvidencePackageError(f"{name} environment_boundary is missing")
    if boundary.get("hermes_home_kind") != "isolated":
        raise EvidencePackageError(f"{name} did not use an isolated Hermes home")
    if boundary.get("active_instance_touched") is not False:
        raise EvidencePackageError(f"{name} touched or failed to exclude the active instance")
    if not str(boundary.get("database_kind") or ""):
        raise EvidencePackageError(f"{name} database_kind is missing")
    if name in RECEIPT_FILES and name not in {
        "DOCTOR.json",
        "ACTIVE_ISOLATION.json",
        "REPOSITORY_CENSUS.json",
        "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
    }:
        if payload.get("artifact_consumed") is not True:
            raise EvidencePackageError(f"{name} did not consume the artifact")
        if payload.get("artifact_kind") != "wheel":
            raise EvidencePackageError(f"{name} did not consume the wheel")
        if payload.get("install_receipt_sha256") != candidate_install_sha256:
            raise EvidencePackageError(f"{name} install receipt link mismatch")
        if payload.get("artifact_sha256") != candidate_install.get(
            "artifact_sha256"
        ):
            raise EvidencePackageError(f"{name} artifact differs from install receipt")
        if payload.get("direct_url_sha256") != candidate_install.get(
            "direct_url_sha256"
        ):
            raise EvidencePackageError(f"{name} direct_url link mismatch")
        if payload.get("record_sha256") != candidate_install.get("record_sha256"):
            raise EvidencePackageError(f"{name} RECORD link mismatch")
        if payload.get("source_worktree_imported") is not False:
            raise EvidencePackageError(f"{name} imported source-only product code")
        if payload.get("source_worktree_on_sys_path") is not False:
            raise EvidencePackageError(f"{name} exposed source root on sys.path")
        if payload.get("imported_module_path_class") != "isolated-site-packages":
            raise EvidencePackageError(f"{name} import origin is not installed")
        _require_sha(payload.get("environment_id"), field=f"{name}.environment_id")
        boundary = payload.get("environment_boundary")
        assert isinstance(boundary, dict)
        if boundary.get("identity_scheme") != "sha256-local-path-v1":
            raise EvidencePackageError(f"{name} environment identity scheme mismatch")
        for field in ("hermes_home_id", "database_id", "pytest_basetemp_id"):
            _require_sha(boundary.get(field), field=f"{name}.{field}")
        _validated_hermes_identity(payload, field="hermes_source_identity")


def _source_identity(evidence_dir: Path, expected_sha: str) -> tuple[str, str]:
    identity = _load_object(evidence_dir / "SOURCE_IDENTITY.json")
    source_commit = _require_git_sha(identity.get("source_commit"), field="source_commit")
    source_tree = _require_git_sha(identity.get("source_tree"), field="source_tree")
    if source_commit != expected_sha:
        raise EvidencePackageError("evidence source commit differs from expected SHA")
    if identity.get("source_dirty") is not False:
        raise EvidencePackageError("evidence source identity is dirty")
    return source_commit, source_tree


def _validate_remote_ci_binding(
    payload: Mapping[str, object],
    *,
    source_commit: str,
) -> str:
    if payload.get("schema_version") != "scope-recall.remote-ci-binding.v1":
        raise EvidencePackageError("remote CI binding schema mismatch")
    if payload.get("repository") != "410979729/scope-recall-hermes":
        raise EvidencePackageError("remote CI repository mismatch")
    if payload.get("head_sha") != source_commit:
        raise EvidencePackageError("remote CI source SHA mismatch")
    if payload.get("workflow_path") != ".github/workflows/ci.yml":
        raise EvidencePackageError("remote CI workflow path mismatch")
    if payload.get("run_status") != "completed" or payload.get(
        "run_conclusion"
    ) != "success":
        raise EvidencePackageError("remote CI run is not successful")
    if payload.get("required_job") != "ci-required" or payload.get(
        "required_job_conclusion"
    ) != "success":
        raise EvidencePackageError("remote CI required job is not successful")
    for field in ("run_response_sha256", "jobs_response_sha256"):
        _require_sha(payload.get(field), field=f"remote_ci.{field}")
    if payload.get("authentication_material_recorded") is not False:
        raise EvidencePackageError("remote CI receipt recorded authentication material")
    run_id = str(payload.get("run_id") or "")
    if not run_id.isdigit():
        raise EvidencePackageError("remote CI run ID is invalid")
    return run_id


def build_evidence_index(evidence_dir: Path, *, expected_sha: str) -> dict[str, object]:
    requested = Path(evidence_dir)
    if requested.is_symlink():
        raise EvidencePackageError("evidence directory must not be a symlink")
    root = requested.resolve(strict=True)
    if root.name != expected_sha:
        raise EvidencePackageError("evidence directory must be a real full-SHA directory")
    missing = [name for name in REQUIRED_INPUT_FILES if not (root / name).is_file()]
    if missing:
        raise EvidencePackageError(f"evidence package is incomplete: {', '.join(missing)}")
    source_commit, source_tree = _source_identity(root, expected_sha)
    provenance = _load_object(root / "BUILD_PROVENANCE.json")
    candidate = _load_object(root / "CANDIDATE_MANIFEST.json")
    if provenance.get("source_commit") != source_commit:
        raise EvidencePackageError("build provenance source_commit mismatch")
    if provenance.get("source_tree") != source_tree:
        raise EvidencePackageError("build provenance source_tree mismatch")
    candidate_source = candidate.get("source")
    if not isinstance(candidate_source, dict):
        raise EvidencePackageError("candidate manifest source is missing")
    if candidate_source.get("commit") != source_commit:
        raise EvidencePackageError("candidate manifest source_commit mismatch")
    if candidate_source.get("tree") != source_tree:
        raise EvidencePackageError("candidate manifest source_tree mismatch")
    candidate_provenance = candidate.get("provenance")
    if not isinstance(candidate_provenance, dict):
        raise EvidencePackageError("candidate manifest provenance link is missing")
    provenance_hash = _sha256_file(root / "BUILD_PROVENANCE.json")
    if candidate_provenance.get("sha256") != provenance_hash:
        raise EvidencePackageError("candidate manifest provenance hash mismatch")
    ci_run_ids = candidate.get("ci_run_ids")
    if not isinstance(ci_run_ids, list) or not all(
        isinstance(item, str) and item.isdigit() for item in ci_run_ids
    ):
        raise EvidencePackageError("candidate manifest CI run IDs are invalid")
    remote_path = root / "REMOTE_CI_BINDING.json"
    if ci_run_ids or remote_path.is_file():
        if not remote_path.is_file():
            raise EvidencePackageError("candidate CI run IDs lack a binding receipt")
        remote_run_id = _validate_remote_ci_binding(
            _load_object(remote_path),
            source_commit=source_commit,
        )
        if sorted(set(ci_run_ids)) != [remote_run_id]:
            raise EvidencePackageError("candidate manifest CI run ID is not bound")
    artifact_hashes: set[str] = set()
    wheel_sha256 = ""
    for kind in ("wheel", "sdist"):
        artifact = provenance.get(kind)
        if isinstance(artifact, dict):
            artifact_sha = _require_sha(
                artifact.get("sha256"), field=f"{kind}.sha256"
            )
            artifact_hashes.add(artifact_sha)
            if kind == "wheel":
                wheel_sha256 = artifact_sha
    if len(artifact_hashes) != 2:
        raise EvidencePackageError("build provenance must bind distinct wheel and sdist hashes")
    honesty = validate_test_honesty(_load_object(root / "PYTEST_SKIP_REPORT.json"))
    install_receipts = {
        name: _load_object(root / name) for name in INSTALL_RECEIPT_FILES
    }
    for name, payload in install_receipts.items():
        _validate_install_receipt(
            name,
            payload,
            source_commit=source_commit,
            source_tree=source_tree,
            wheel_sha256=wheel_sha256,
        )
    if install_receipts["INSTALL_CANDIDATE_RECEIPT.json"].get(
        "environment_id"
    ) == install_receipts["INSTALL_N_MINUS_ONE_RECEIPT.json"].get("environment_id"):
        raise EvidencePackageError("candidate and N-1 install environments are not distinct")
    _validate_install_command_stages(root, install_receipts)
    rehearsal_hermes_identity = _validate_hermes_probes(root)
    candidate_install = install_receipts["INSTALL_CANDIDATE_RECEIPT.json"]
    candidate_install_sha256 = _sha256_file(
        root / "INSTALL_CANDIDATE_RECEIPT.json"
    )
    n_minus_one_install_sha256 = _sha256_file(
        root / "INSTALL_N_MINUS_ONE_RECEIPT.json"
    )
    for name in RECEIPT_FILES:
        receipt = _load_object(root / name)
        _validate_receipt(
            name,
            receipt,
            source_commit=source_commit,
            source_tree=source_tree,
            artifact_hashes=artifact_hashes,
            candidate_install_sha256=candidate_install_sha256,
            candidate_install=candidate_install,
        )
        if name not in {
            "DOCTOR.json",
            "ACTIVE_ISOLATION.json",
            "REPOSITORY_CENSUS.json",
            "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
        } and receipt.get("hermes_source_identity") != rehearsal_hermes_identity:
            raise EvidencePackageError(f"{name} Hermes source identity mismatch")
        if name in {"MIGRATION_N_MINUS_ONE.json", "DOWNGRADE_N_MINUS_ONE.json"}:
            if receipt.get("n_minus_one_install_receipt_sha256") != (
                n_minus_one_install_sha256
            ):
                raise EvidencePackageError(f"{name} N-1 install link mismatch")
            if receipt.get("candidate_n_minus_one_environment_mixed") is not False:
                raise EvidencePackageError(f"{name} mixed candidate and N-1 environments")

    files: list[dict[str, object]] = []
    evidence_paths = sorted(
        path
        for path in root.rglob("*")
        if path.name != "EVIDENCE_INDEX.json" and not path.is_dir()
    )
    for path in evidence_paths:
        name = path.relative_to(root).as_posix()
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or name != pure.as_posix():
            raise EvidencePackageError(f"unsafe evidence path: {name}")
        if path.is_symlink():
            raise EvidencePackageError(f"evidence file must not be a symlink: {name}")
        entry: dict[str, object] = {
            "path": name,
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
            "classification": (
                "shareable"
                if name in SHAREABLE_EXPLICIT_FILES
                or name
                in {
                    "EVIDENCE_PRIVATE_PATH_SCAN.json",
                    "EVIDENCE_SECRET_SCAN.json",
                    "SHAREABLE_EVIDENCE_INDEX.json",
                }
                else "local-restricted"
            ),
        }
        if path.suffix == ".json":
            payload = _load_json(path)
            if isinstance(payload, dict):
                entry["json_root"] = "object"
                entry["schema_version"] = str(
                    payload.get("schema_version") or "unversioned"
                )
            elif isinstance(payload, list) and all(
                isinstance(item, dict) for item in payload
            ):
                entry["json_root"] = "array"
                entry["item_count"] = len(payload)
                entry["schema_version"] = "unversioned"
            else:
                raise EvidencePackageError(
                    "JSON evidence root must be an object or an array of objects: "
                    f"{path.name}"
                )
        files.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit,
        "source_tree": source_tree,
        "build_provenance_sha256": provenance_hash,
        "candidate_manifest_sha256": _sha256_file(root / "CANDIDATE_MANIFEST.json"),
        "artifact_sha256": sorted(artifact_hashes),
        "test_honesty": honesty,
        "file_count": len(files),
        "files": files,
        "environment_boundary": {
            "active_instance_touched": False,
            "evidence_paths": "relative-only",
            "raw_logs_committed": False,
            "index_classification": "local-restricted",
        },
    }


def _scan_shareable_files(
    root: Path,
    entries: Sequence[Mapping[str, object]],
    *,
    scan_kind: str,
    patterns: Sequence[re.Pattern[str]],
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    scanned: list[dict[str, object]] = []
    for entry in entries:
        name = str(entry.get("path") or "")
        path = root.joinpath(*PurePosixPath(name).parts)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvidencePackageError(
                f"shareable evidence is not UTF-8 text: {name}"
            ) from exc
        scanned.append({"path": name, "sha256": _sha256_file(path)})
        for rule_index, pattern in enumerate(patterns, 1):
            count = len(pattern.findall(text))
            if count:
                findings.append(
                    {
                        "path": name,
                        "rule_id": f"{scan_kind}-{rule_index}",
                        "match_count": count,
                    }
                )
    return {
        "schema_version": EVIDENCE_SCAN_SCHEMA_VERSION,
        "scan_kind": scan_kind,
        "classification": "shareable",
        "scanned_file_count": len(scanned),
        "scanned_manifest_sha256": hashlib.sha256(
            json.dumps(
                scanned,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "finding_count": len(findings),
        "findings": findings,
        "status": "passed" if not findings else "failed",
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _shareable_evidence_index(
    root: Path,
    full_index: Mapping[str, object],
) -> dict[str, object]:
    raw_files = full_index.get("files")
    if not isinstance(raw_files, list):
        raise EvidencePackageError("full evidence index files are missing")
    shareable = [
        dict(entry)
        for entry in raw_files
        if isinstance(entry, dict)
        and entry.get("classification") == "shareable"
        and entry.get("path") in SHAREABLE_EXPLICIT_FILES
    ]
    expected_present = {
        name for name in SHAREABLE_EXPLICIT_FILES if (root / name).is_file()
    }
    actual = {str(entry.get("path") or "") for entry in shareable}
    if actual != expected_present:
        raise EvidencePackageError("shareable evidence allowlist coverage mismatch")
    private_scan = _scan_shareable_files(
        root,
        shareable,
        scan_kind="private-path",
        patterns=PRIVATE_PATH_PATTERNS,
    )
    secret_scan = _scan_shareable_files(
        root,
        shareable,
        scan_kind="secret",
        patterns=SECRET_PATTERNS,
    )
    if private_scan["status"] != "passed" or secret_scan["status"] != "passed":
        raise EvidencePackageError("shareable evidence content scan failed")
    private_path = root / "EVIDENCE_PRIVATE_PATH_SCAN.json"
    secret_path = root / "EVIDENCE_SECRET_SCAN.json"
    _write_json(private_path, private_scan)
    _write_json(secret_path, secret_scan)
    for path in (private_path, secret_path):
        shareable.append(
            {
                "path": path.name,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
                "classification": "shareable",
                "json_root": "object",
                "schema_version": EVIDENCE_SCAN_SCHEMA_VERSION,
            }
        )
    shareable.sort(key=lambda item: str(item["path"]))
    return {
        "schema_version": SHAREABLE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification": "shareable",
        "source_commit": full_index["source_commit"],
        "source_tree": full_index["source_tree"],
        "build_provenance_sha256": full_index["build_provenance_sha256"],
        "candidate_manifest_sha256": full_index["candidate_manifest_sha256"],
        "artifact_sha256": full_index["artifact_sha256"],
        "private_path_scan_sha256": _sha256_file(private_path),
        "secret_scan_sha256": _sha256_file(secret_path),
        "private_path_finding_count": 0,
        "secret_finding_count": 0,
        "file_count": len(shareable),
        "files": shareable,
        "excluded_classification": "local-restricted",
    }


def write_evidence_index(evidence_dir: Path, payload: Mapping[str, object]) -> Path:
    root = evidence_dir.resolve(strict=True)
    shareable = _shareable_evidence_index(root, payload)
    shareable_output = root / "SHAREABLE_EVIDENCE_INDEX.json"
    rendered = json.dumps(
        shareable,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if any(pattern.search(rendered) for pattern in PRIVATE_PATH_PATTERNS):
        raise EvidencePackageError("shareable evidence index contains a private path")
    if any(pattern.search(rendered) for pattern in SECRET_PATTERNS):
        raise EvidencePackageError("shareable evidence index contains secret-like content")
    shareable_output.write_text(
        rendered,
        encoding="utf-8",
        newline="\n",
    )
    final_payload = build_evidence_index(
        root,
        expected_sha=str(payload.get("source_commit") or ""),
    )
    output = root / "EVIDENCE_INDEX.json"
    _write_json(output, final_payload)
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    expected_sha = _require_git_sha(args.expected_sha, field="expected_sha")
    payload = build_evidence_index(args.evidence_dir, expected_sha=expected_sha)
    output = write_evidence_index(args.evidence_dir, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "source_commit": payload["source_commit"],
                "file_count": payload["file_count"],
                "index_sha256": _sha256_file(output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidencePackageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
