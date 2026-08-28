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
        },
    )
    for name in module.REQUIRED_INPUT_FILES:
        path = evidence / name
        if path.exists():
            continue
        if name == "PYTEST_SKIP_REPORT.json":
            _write_json(path, _valid_honesty(module))
        elif name in module.RECEIPT_FILES:
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
                    },
                    "result": "passed",
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
    assert payload["file_count"] == len(module.REQUIRED_INPUT_FILES)
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
