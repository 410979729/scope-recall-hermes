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


def _valid_honesty(
    module,
    *,
    source_commit: str = "a" * 40,
    source_tree: str = "b" * 40,
) -> dict[str, object]:
    return {
        "schema_version": module.TEST_HONESTY_SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_tree": source_tree,
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


def _valid_issue_51_details(module) -> dict[str, object]:
    return {
        "schema_version": module.ISSUE_51_DETAILS_SCHEMA_VERSION,
        "visible_memory_count": 2_046,
        "legacy_pending_count": 1_136,
        "legacy_attempts_total": 658_038,
        "old_revision_distinct_count": 1,
        "initialization_legacy_mutations": 0,
        "idle_legacy_mutations": 0,
        "legacy_attempts_unchanged": True,
        "legacy_status_unchanged": True,
        "legacy_available_at_unchanged": True,
        "legacy_lease_fields_unchanged": True,
        "simulated_monotonic_seconds": 60.0,
        "simulated_idle_tick_count": 61,
        "legacy_claim_calls": 0,
        "legacy_drain_calls": 0,
        "legacy_sql_transaction_count": 0,
        "legacy_sql_mutation_count": 0,
        "exact_focus_work_count": 1,
        "exact_focus_scope_count": 1,
        "scope_wide_fanout_count": 0,
        "candidate_cap": 1,
        "candidate_affected_count": 2,
        "candidate_cap_refused": True,
        "partial_relation_mutation_count": 0,
        "operator_action_required": True,
        "cleanup_plan_sha256": "a" * 64,
        "cleanup_repeated_plan_sha256": "a" * 64,
        "cleanup_cas_refused": True,
        "backup_verified": True,
        "backup_visible_memory_count": 2_046,
        "backup_legacy_pending_count": 1_136,
        "cleanup_deleted_legacy_count": 1_136,
        "cleanup_disposition_count": 1_136,
        "cleanup_receipt_state": "mirrored",
        "cleanup_receipt_present": True,
        "cleanup_idempotent_replay": True,
        "cleanup_replay_backup_stable": True,
        "cleanup_remaining_legacy_count": 0,
    }


def _valid_writer_handoff_details(module, wheel_sha: str) -> dict[str, object]:
    return {
        "schema_version": module.WRITER_HANDOFF_DETAILS_SCHEMA_VERSION,
        "writer_artifact_sha256": wheel_sha,
        "idle_release_seconds": 1800.0,
        "process_count": 2,
        "same_process_provider_count": 2,
        "stages": list(module.WRITER_HANDOFF_STAGES),
        "simultaneous_writer_observed": False,
        "accepted_work_lost": False,
        "holder_count_after_release": 0,
        "connection_pin_count_after_release": 0,
        "result": "passed",
    }


def _complete_fixture(module, tmp_path: Path) -> tuple[Path, str, str]:
    commit = "a" * 40
    tree = "b" * 40
    wheel_sha = "c" * 64
    sdist_sha = "d" * 64
    source_manifest_sha = "5" * 64
    evidence = tmp_path / commit
    evidence.mkdir()
    provenance = {
        "schema_version": "scope-recall.build-provenance.v1",
        "source_commit": commit,
        "source_tree": tree,
        "source_manifest_sha256": source_manifest_sha,
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
            "source_manifest_sha256": source_manifest_sha,
        },
    )
    runner_sha = "4" * 64
    hermes_identity = {
        "commit": module.SUPPORTED_HERMES_COMMIT,
        "tree": module.SUPPORTED_HERMES_TREE,
        "clean": True,
    }
    _write_json(
        evidence / "CANDIDATE_MANIFEST.json",
        {
            "schema_version": "scope-recall.candidate-manifest.v1",
            "candidate_mode": module.FINAL_CANDIDATE_MODE,
            "candidate_version": "2.0.0",
            "source": {
                "commit": commit,
                "tree": tree,
                "manifest": {
                    "manifest_sha256": source_manifest_sha,
                    "files": [
                        {
                            "path": "scripts/rehearse_n_minus_one_window.py",
                            "sha256": runner_sha,
                        }
                    ]
                },
            },
            "hermes": {
                **hermes_identity,
                "version": module.SUPPORTED_HERMES_VERSION,
            },
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
    for version, result in (("0.19.1", "compatible"), ("0.20.6", "incompatible")):
        probe_identity = (
            hermes_identity
            if version == "0.19.1"
            else {"commit": "d" * 40, "tree": "e" * 40, "clean": True}
        )
        _write_json(
            evidence / f"HERMES_COMPATIBILITY_PROBE.{version}.json",
            {
                "schema_version": "scope-recall.hermes-compatibility-probe.v1",
                "expected_hermes_version": version,
                "observed_hermes_version": version,
                "hermes_source": probe_identity,
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
    honesty = _valid_honesty(
        module,
        source_commit=commit,
        source_tree=tree,
    )
    _write_json(evidence / "PYTEST_SKIP_REPORT.raw.json", honesty)
    _write_json(evidence / "PYTEST_SKIP_REPORT.json", honesty)
    created_database_sha = "0" * 64
    upgraded_database_sha = "9" * 64
    database_lineage_id = hashlib.sha256(
        json.dumps(
            {
                "n_minus_one_created": created_database_sha,
                "candidate_upgraded": upgraded_database_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stage_log_hashes: dict[str, dict[str, str]] = {}
    for stage_name in (
        "n_minus_one_create",
        "candidate_upgrade_write",
        "n_minus_one_read_after_n",
        "candidate_final_verify",
    ):
        hashes: dict[str, str] = {}
        for stream in ("stdout", "stderr"):
            log = evidence / f"N_MINUS_ONE_WINDOW_{stage_name.upper()}.{stream}.log"
            log.write_text(f"{stage_name} {stream}\n", encoding="utf-8")
            hashes[f"{stream}_sha256"] = _sha256(log)
        stage_log_hashes[stage_name] = hashes
    common_stage = {
        "command_sha256": "1" * 64,
        "source_worktree_on_sys_path": False,
        "source_worktree_imported": False,
        "returncode": 0,
    }
    _write_json(
        evidence / "N_MINUS_ONE_WINDOW.json",
        {
            "schema_version": "scope-recall.n-minus-one-window.v1",
            "candidate_source_commit": commit,
            "candidate_source_tree": tree,
            "candidate_install_receipt_sha256": candidate_install_sha,
            "n_minus_one_install_receipt_sha256": n_minus_one_install_sha,
            "neutral_runner_sha256": runner_sha,
            "database_lineage_id": database_lineage_id,
            "candidate_n_minus_one_environment_mixed": False,
            "active_instance_touched": False,
            "result": "passed",
            "stages": [
                {
                    **common_stage,
                    **stage_log_hashes["n_minus_one_create"],
                    "stage": "n_minus_one_create",
                    "python_environment_id": "7" * 64,
                    "installed_distribution": "hermes-scope-recall==1.10.3",
                    "artifact_sha256": "6" * 64,
                    "python_executable_sha256": "f" * 64,
                    "database_before_sha256": "8" * 64,
                    "database_after_sha256": created_database_sha,
                    "details": {
                        "memory_count": 2,
                        "config_isolation_key_present": True,
                        "vector_enabled": False,
                    },
                },
                {
                    **common_stage,
                    **stage_log_hashes["candidate_upgrade_write"],
                    "stage": "candidate_upgrade_write",
                    "python_environment_id": "e" * 64,
                    "installed_distribution": "hermes-scope-recall==2.0.0",
                    "artifact_sha256": wheel_sha,
                    "python_executable_sha256": "f" * 64,
                    "database_before_sha256": created_database_sha,
                    "database_after_sha256": upgraded_database_sha,
                    "details": {
                        "n_minus_one_rows_preserved": 2,
                        "claim_count": 1,
                        "evidence_count": 1,
                    },
                },
                {
                    **common_stage,
                    **stage_log_hashes["n_minus_one_read_after_n"],
                    "stage": "n_minus_one_read_after_n",
                    "python_environment_id": "7" * 64,
                    "installed_distribution": "hermes-scope-recall==1.10.3",
                    "artifact_sha256": "6" * 64,
                    "python_executable_sha256": "f" * 64,
                    "database_before_sha256": upgraded_database_sha,
                    "database_after_sha256": upgraded_database_sha,
                    "details": {
                        "query_only": 1,
                        "candidate_projection_readable": True,
                    },
                },
                {
                    **common_stage,
                    **stage_log_hashes["candidate_final_verify"],
                    "stage": "candidate_final_verify",
                    "python_environment_id": "e" * 64,
                    "installed_distribution": "hermes-scope-recall==2.0.0",
                    "artifact_sha256": wheel_sha,
                    "python_executable_sha256": "f" * 64,
                    "database_before_sha256": upgraded_database_sha,
                    "database_after_sha256": upgraded_database_sha,
                    "details": {
                        "claim_only_count": 0,
                        "evidence_count": 1,
                        "legacy_projection_count": 2,
                    },
                },
            ],
        },
    )
    n_minus_one_window_sha = _sha256(evidence / "N_MINUS_ONE_WINDOW.json")
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
                        "n_minus_one_window_receipt_sha256": n_minus_one_window_sha,
                        "n_minus_one_window_evidence": "real-cross-interpreter",
                    }
                )
            if name == "ISSUE_51_REGRESSION.json":
                extra["details"] = {
                    "node_ids": sorted(module.ISSUE_51_REQUIRED_NODE_IDS),
                    "issue_51_regression": _valid_issue_51_details(module),
                }
            receipt_schema = "scope-recall.test-receipt.v1"
            if name == "WRITER_LEASE_HANDOFF_REHEARSAL.json":
                handoff = _valid_writer_handoff_details(module, wheel_sha)
                receipt_schema = module.WRITER_HANDOFF_SCHEMA_VERSION
                extra.update(
                    {
                        key: value
                        for key, value in handoff.items()
                        if key != "schema_version"
                    }
                )
                extra["details"] = {
                    "node_ids": sorted(module.WRITER_HANDOFF_REQUIRED_NODE_IDS),
                    "writer_lease_handoff": handoff,
                }
            _write_json(
                path,
                {
                    "schema_version": receipt_schema,
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
    assert "source_commit" not in payload["test_honesty"]
    assert "source_tree" not in payload["test_honesty"]
    issue_51_entry = next(
        item
        for item in payload["files"]
        if item["path"] == "ISSUE_51_REGRESSION.json"
    )
    assert issue_51_entry["classification"] == "shareable"
    serialized = json.dumps(payload, sort_keys=True)
    assert str(tmp_path) not in serialized
    (evidence / "RUFF.log").unlink()
    with pytest.raises(module.EvidencePackageError, match="incomplete"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_issue_51_mutation_or_scale_drift(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    path = evidence / "ISSUE_51_REGRESSION.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["details"]["issue_51_regression"]["legacy_sql_mutation_count"] = 1
    _write_json(path, payload)

    with pytest.raises(
        module.EvidencePackageError,
        match="legacy_sql_mutation_count mismatch",
    ):
        module.build_evidence_index(evidence, expected_sha=commit)


@pytest.mark.parametrize(
    ("field", "stale_value", "message"),
    [
        ("source_commit", "f" * 40, "test honesty source_commit mismatch"),
        ("source_tree", "e" * 40, "test honesty source_tree mismatch"),
    ],
)
def test_evidence_index_rejects_stale_test_honesty_source_identity(
    tmp_path: Path,
    field: str,
    stale_value: str,
    message: str,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    for name in ("PYTEST_SKIP_REPORT.raw.json", "PYTEST_SKIP_REPORT.json"):
        path = evidence / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[field] = stale_value
        _write_json(path, payload)

    with pytest.raises(module.EvidencePackageError, match=message):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_unpaired_raw_test_honesty_source_drift(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    path = evidence / "PYTEST_SKIP_REPORT.raw.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_commit"] = "f" * 40
    _write_json(path, payload)

    with pytest.raises(
        module.EvidencePackageError,
        match="shareable test honesty is not an exact path-only redaction",
    ):
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", None),
        ("source_tree", "not-a-git-object-id"),
    ],
)
def test_test_honesty_requires_exact_source_identity(
    field: str,
    value: object,
) -> None:
    module = _load_module()
    payload = _valid_honesty(module)
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    with pytest.raises(
        module.EvidencePackageError,
        match=rf"test_honesty\.{field} must be a full lowercase Git SHA",
    ):
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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.update({"candidate_mode": "development-snapshot"}),
            "not a final release candidate",
        ),
        (
            lambda payload: payload.update(
                {"hermes": {"commit": "unbound", "tree": "unbound", "version": "unbound"}}
            ),
            "does not match the supported baseline",
        ),
        (
            lambda payload: payload["hermes"].update({"tree": "7" * 40}),
            "does not match the supported baseline",
        ),
    ],
)
def test_evidence_index_rejects_nonfinal_or_drifted_candidate_hermes(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    manifest_path = evidence / "CANDIDATE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    _write_json(manifest_path, manifest)

    with pytest.raises(module.EvidencePackageError, match=message):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_supported_hermes_probe_identity_drift(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    probe_path = evidence / "HERMES_COMPATIBILITY_PROBE.0.19.1.json"
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    probe["hermes_source"]["commit"] = "7" * 40
    _write_json(probe_path, probe)

    with pytest.raises(module.EvidencePackageError, match="supported source"):
        module.build_evidence_index(evidence, expected_sha=commit)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("hermes_source"),
        lambda payload: payload.update(
            {"hermes_source": {"commit": "unbound", "tree": "unbound", "clean": True}}
        ),
        lambda payload: payload["hermes_source"].update({"clean": False}),
    ],
)
def test_evidence_index_rejects_unbound_additive_hermes_probe(
    tmp_path: Path,
    mutation,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    probe_path = evidence / "HERMES_COMPATIBILITY_PROBE.0.20.6.json"
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    mutation(probe)
    _write_json(probe_path, probe)

    with pytest.raises(module.EvidencePackageError, match="hermes_source"):
        module.build_evidence_index(evidence, expected_sha=commit)


def test_evidence_index_rejects_unbound_n_minus_one_stage_log(
    tmp_path: Path,
) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    log = evidence / "N_MINUS_ONE_WINDOW_N_MINUS_ONE_CREATE.stdout.log"
    log.write_text("tampered stage output\n", encoding="utf-8")

    with pytest.raises(module.EvidencePackageError, match="stdout log hash mismatch"):
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


def test_raw_skip_report_is_local_restricted(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)

    payload = module.build_evidence_index(evidence, expected_sha=commit)

    raw = next(
        item
        for item in payload["files"]
        if item["path"] == "PYTEST_SKIP_REPORT.raw.json"
    )
    assert raw["classification"] == "local-restricted"
    assert "PYTEST_SKIP_REPORT.raw.json" not in module.SHAREABLE_EXPLICIT_FILES


def test_shareable_index_transitive_closure_has_zero_private_paths(tmp_path: Path) -> None:
    module = _load_module()
    evidence, commit, _tree = _complete_fixture(module, tmp_path)
    payload = module.build_evidence_index(evidence, expected_sha=commit)

    module.write_evidence_index(evidence, payload)

    shareable = json.loads(
        (evidence / "SHAREABLE_EVIDENCE_INDEX.json").read_text(encoding="utf-8")
    )
    paths = {entry["path"] for entry in shareable["files"]}
    assert shareable["private_path_match_count"] == 0
    assert shareable["secret_match_count"] == 0
    assert shareable["missing_indexed_file_count"] == 0
    assert shareable["unexpected_shareable_file_count"] == 0
    assert "PYTEST_SKIP_REPORT.json" in paths
    assert "PYTEST_SKIP_REPORT.raw.json" not in paths


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
