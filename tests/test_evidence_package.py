"""Raw evidence completeness, binding, and test-honesty contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report.evidence_package.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_evidence_package_test",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_honesty(module) -> dict[str, object]:
    return {
        "schema_version": module.TEST_HONESTY_SCHEMA_VERSION,
        "collected": 3,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": [{"node_id": "tests/test_x.py::test_skip", "reason": "platform"}],
        "xfail": 0,
        "xpass": 0,
        "rerun_count": 0,
        "timeout_overrides": [],
        "duration_seconds": 1.25,
        "first_failure_fixes": [],
        "first_failure_fixes_status": "not_provided",
    }


def _complete_fixture(module, tmp_path: Path) -> tuple[Path, str, str]:
    commit = "a" * 40
    tree = "b" * 40
    wheel_sha = "c" * 64
    sdist_sha = "d" * 64
    evidence = tmp_path / commit
    evidence.mkdir()
    provenance = {
        "schema_version": "scope-recall.build-provenance.v1",
        "source_commit": commit,
        "source_tree": tree,
        "wheel": {"sha256": wheel_sha},
        "sdist": {"sha256": sdist_sha},
    }
    _write_json(evidence / "BUILD_PROVENANCE.json", provenance)
    _write_json(
        evidence / "SOURCE_IDENTITY.json",
        {
            "schema_version": "scope-recall.source-identity.v1",
            "source_commit": commit,
            "source_tree": tree,
            "source_dirty": False,
        },
    )
    _write_json(
        evidence / "CANDIDATE_MANIFEST.json",
        {
            "schema_version": "scope-recall.candidate-manifest.v1",
            "source": {"commit": commit, "tree": tree},
            "provenance": {"sha256": _sha256(evidence / "BUILD_PROVENANCE.json")},
            "ci_run_ids": [],
        },
    )
    install_stage_ledger: list[dict[str, object]] = []
    install_hashes: dict[str, dict[str, str]] = {}
    for label in ("CANDIDATE", "N_MINUS_ONE"):
        label_hashes: dict[str, str] = {}
        for suffix, field in (
            ("VENV", "venv_stage_sha256"),
            ("", "install_stage_sha256"),
            ("PROBE", "probe_stage_sha256"),
        ):
            middle = f"_{suffix}" if suffix else ""
            log_name = f"INSTALL_{label}{middle}.log"
            log_path = evidence / log_name
            log_path.write_text(f"{label} {field} passed\n", encoding="utf-8")
            digest = _sha256(log_path)
            label_hashes[field] = digest
            install_stage_ledger.append(
                {"log": log_name, "log_sha256": digest, "exit_code": 0}
            )
        install_hashes[label] = label_hashes
    _write_json(
        evidence / "TEST_COMMANDS.json",
        {
            "schema_version": "scope-recall.release-validation.v1",
            "commands": install_stage_ledger,
        },
    )
    hermes_identity = {"commit": "9" * 40, "tree": "8" * 40, "clean": True}
    for version, result in (("0.19.1", "compatible"), ("0.20.6", "incompatible")):
        _write_json(
            evidence / f"HERMES_COMPATIBILITY_PROBE.{version}.json",
            {
                "schema_version": "scope-recall.hermes-compatibility-probe.v1",
                "expected_hermes_version": version,
                "observed_hermes_version": version,
                "hermes_source": hermes_identity,
                "result": result,
                "support_matrix_changed": False,
                "active_instance_touched": False,
            },
        )
    install_base = {
        "schema_version": "scope-recall.artifact-install-receipt.v1",
        "artifact_kind": "wheel",
        "python_executable_sha256": "f" * 64,
        "installed_package_manifest_sha256": "1" * 64,
        "environment_distribution_manifest_sha256": "8" * 64,
        "record_sha256": "2" * 64,
        "direct_url_sha256": "3" * 64,
        "source_worktree_imported": False,
        "source_worktree_on_sys_path": False,
        "imported_module_path_class": "isolated-site-packages",
        "result": "passed",
    }
    _write_json(
        evidence / "INSTALL_CANDIDATE_RECEIPT.json",
        {
            **install_base,
            **install_hashes["CANDIDATE"],
            "environment_id": "e" * 64,
            "artifact_sha256": wheel_sha,
            "installed_distribution": "hermes-scope-recall==2.0.0",
            "source_commit": commit,
            "source_tree": tree,
        },
    )
    _write_json(
        evidence / "INSTALL_N_MINUS_ONE_RECEIPT.json",
        {
            **install_base,
            **install_hashes["N_MINUS_ONE"],
            "environment_id": "7" * 64,
            "artifact_sha256": "6" * 64,
            "installed_distribution": "hermes-scope-recall==1.10.3",
        },
    )
    candidate_install_sha = _sha256(
        evidence / "INSTALL_CANDIDATE_RECEIPT.json"
    )
    n_minus_one_install_sha = _sha256(
        evidence / "INSTALL_N_MINUS_ONE_RECEIPT.json"
    )
    for name in module.REQUIRED_INPUT_FILES:
        path = evidence / name
        if path.exists():
            continue
        if name == "PYTEST_SKIP_REPORT.json":
            _write_json(path, _valid_honesty(module))
        elif name in module.RECEIPT_FILES:
            artifact_rehearsal = name not in {
                "DOCTOR.json",
                "ACTIVE_ISOLATION.json",
                "REPOSITORY_CENSUS.json",
                "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
            }
            extra = (
                {
                    "artifact_consumed": True,
                    "artifact_kind": "wheel",
                    "installed_distribution": "hermes-scope-recall==2.0.0",
                    "imported_module_path_class": "isolated-site-packages",
                    "source_worktree_imported": False,
                    "source_worktree_on_sys_path": False,
                    "install_receipt_sha256": candidate_install_sha,
                    "direct_url_sha256": "3" * 64,
                    "record_sha256": "2" * 64,
                    "environment_id": "7" * 64,
                    "hermes_source_identity": hermes_identity,
                }
                if artifact_rehearsal
                else {}
            )
            if name in {
                "MIGRATION_N_MINUS_ONE.json",
                "DOWNGRADE_N_MINUS_ONE.json",
            }:
                extra.update(
                    {
                        "n_minus_one_install_receipt_sha256": n_minus_one_install_sha,
                        "candidate_n_minus_one_environment_mixed": False,
                    }
                )
            _write_json(
                path,
                {
                    "schema_version": "scope-recall.test-receipt.v1",
                    "source_commit": commit,
                    "source_tree": tree,
                    "artifact_sha256": wheel_sha,
                    "started_at": "2026-08-28T00:00:00+00:00",
                    "finished_at": "2026-08-28T00:00:01+00:00",
                    "command": ["python", "isolated-check.py"],
                    "exit_code": 0,
                    "environment_boundary": {
                        "hermes_home_kind": "isolated",
                        "database_kind": "fixture-copy",
                        "active_instance_touched": False,
                        **(
                            {
                                "identity_scheme": "sha256-local-path-v1",
                                "hermes_home_id": "4" * 64,
                                "database_id": "5" * 64,
                                "pytest_basetemp_id": "6" * 64,
                            }
                            if artifact_rehearsal
                            else {}
                        ),
                    },
                    "result": "passed",
                    **extra,
                },
            )
        elif path.suffix == ".json":
            _write_json(path, {"schema_version": "scope-recall.fixture.v1"})
        else:
            path.write_text("raw local evidence\n", encoding="utf-8")
    return evidence, commit, tree


def test_evidence_index_requires_every_file_and_exact_source_binding(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, tree = _complete_fixture(module, tmp_path)

    payload = module.build_evidence_index(evidence, expected_sha=commit)

    assert payload["source_commit"] == commit
    assert payload["source_tree"] == tree
    assert payload["file_count"] >= len(module.REQUIRED_INPUT_FILES)
    assert payload["test_honesty"]["skipped_node_ids"] == [
        "tests/test_x.py::test_skip"
    ]
    serialized = json.dumps(payload, sort_keys=True)
    assert str(tmp_path) not in serialized
    (evidence / "RUFF.log").unlink()
    with pytest.raises(module.EvidencePackageError, match="incomplete"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_test_honesty_refuses_hidden_reruns_and_bad_accounting() -> None:
    module = _load_module()
    payload = _valid_honesty(module)
    payload["rerun_count"] = 1
    with pytest.raises(module.EvidencePackageError, match="retries or reruns"):
        module.validate_test_honesty(payload)

    payload = _valid_honesty(module)
    payload["collected"] = 4
    with pytest.raises(module.EvidencePackageError, match="collected count"):
        module.validate_test_honesty(payload)


def test_evidence_receipt_refuses_active_instance_contact(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    receipt_path = evidence / "DOCTOR.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["environment_boundary"]["active_instance_touched"] = True
    _write_json(receipt_path, receipt)

    with pytest.raises(module.EvidencePackageError, match="active instance"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_source_only_rehearsal_receipt(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    receipt_path = evidence / "WRITER_CANARY.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_consumed"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(module.EvidencePackageError, match="did not consume"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_unbound_install_stage_and_hermes_identity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    commands_path = evidence / "TEST_COMMANDS.json"
    commands = json.loads(commands_path.read_text(encoding="utf-8"))
    commands["commands"][0]["log_sha256"] = "0" * 64
    _write_json(commands_path, commands)

    with pytest.raises(module.EvidencePackageError, match="ledger digest mismatch"):
        module.build_evidence_index(evidence, expected_sha=commit)

    hermes_drift = tmp_path / "hermes-drift"
    hermes_drift.mkdir()
    evidence, commit, _tree = _complete_fixture(module, hermes_drift)
    receipt_path = evidence / "WRITER_CANARY.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["hermes_source_identity"]["commit"] = "7" * 40
    _write_json(receipt_path, receipt)

    with pytest.raises(module.EvidencePackageError, match="Hermes source identity mismatch"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_dual_index_excludes_raw_logs_and_rejects_private_shareable_path(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    payload = module.build_evidence_index(evidence, expected_sha=commit)
    raw = next(item for item in payload["files"] if item["path"] == "RUFF.log")
    assert raw["classification"] == "local-restricted"

    commands = evidence / "TEST_COMMANDS.json"
    command_payload = json.loads(commands.read_text(encoding="utf-8"))
    command_payload["operator_path"] = "\\".join(
        ("C:", "Users", "private-operator", "release")
    )
    _write_json(
        commands,
        command_payload,
    )
    payload = module.build_evidence_index(evidence, expected_sha=commit)
    with pytest.raises(module.EvidencePackageError, match="content scan failed"):
        module.write_evidence_index(evidence, payload)


def test_dual_index_writes_zero_finding_shareable_closure(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    payload = module.build_evidence_index(evidence, expected_sha=commit)

    full_path = module.write_evidence_index(evidence, payload)

    shareable = json.loads(
        (evidence / "SHAREABLE_EVIDENCE_INDEX.json").read_text(encoding="utf-8")
    )
    assert full_path.name == "EVIDENCE_INDEX.json"
    assert shareable["classification"] == "shareable"
    assert shareable["private_path_finding_count"] == 0
    assert shareable["secret_finding_count"] == 0
    assert "RUFF.log" not in {entry["path"] for entry in shareable["files"]}
    assert (evidence / "EVIDENCE_PRIVATE_PATH_SCAN.json").is_file()
    assert (evidence / "EVIDENCE_SECRET_SCAN.json").is_file()


def test_evidence_index_accepts_bounded_stage_array(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    stage_path = evidence / "SDIST_TEST_STAGES.json"
    _write_json(stage_path, [{"module": "tests/test_fixture.py", "exit_code": 0}])

    payload = module.build_evidence_index(evidence, expected_sha=commit)

    stage_entry = next(
        item for item in payload["files"] if item["path"] == stage_path.name
    )
    assert stage_entry["json_root"] == "array"
    assert stage_entry["item_count"] == 1

    _write_json(stage_path, ["not-an-evidence-object"])
    with pytest.raises(module.EvidencePackageError, match="array of objects"):
        module.build_evidence_index(evidence, expected_sha=commit)
