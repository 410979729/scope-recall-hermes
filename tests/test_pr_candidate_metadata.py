"""Draft-PR metadata must remain bound to the exact frozen candidate."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check.pr_candidate_metadata.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_pr_candidate_metadata_test",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path):
    module = _module()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    commit = "a" * 40
    tree = "b" * 40
    source_manifest_sha = "c" * 64
    wheel = {"name": "hermes_scope_recall-2.0.0.whl", "sha256": "d" * 64}
    sdist = {"name": "hermes_scope_recall-2.0.0.tar.gz", "sha256": "e" * 64}
    _write(
        evidence / "CANDIDATE_MANIFEST.json",
        {
            "candidate_version": "2.0.0",
            "source": {
                "commit": commit,
                "tree": tree,
                "manifest": {"manifest_sha256": source_manifest_sha},
            },
            "artifacts": [
                {"kind": "wheel", **wheel},
                {"kind": "sdist", **sdist},
            ],
            "ci_run_ids": ["123456"],
            "private_artifacts_included": False,
            "authorization": {
                "merge": False,
                "tag": False,
                "release": False,
                "deploy": False,
            },
        },
    )
    _write(
        evidence / "BUILD_PROVENANCE.json",
        {
            "source_commit": commit,
            "source_tree": tree,
            "source_manifest_sha256": source_manifest_sha,
            "wheel": wheel,
            "sdist": sdist,
        },
    )
    _write(
        evidence / "REMOTE_CI_BINDING.json",
        {
            "schema_version": "scope-recall.remote-ci-binding.v1",
            "repository": "410979729/scope-recall-hermes",
            "workflow_path": ".github/workflows/ci.yml",
            "run_status": "completed",
            "head_sha": commit,
            "head_branch": "scope-recall/2.0.0-rc-audit-remediation",
            "run_id": "123456",
            "run_attempt": 1,
            "run_conclusion": "success",
            "required_job": "ci-required",
            "required_job_conclusion": "success",
        },
    )
    _write(
        evidence / "PYTEST_SKIP_REPORT.json",
        {
            "schema_version": "scope-recall.test-honesty.v1",
            "collected": 12,
            "passed": 10,
            "failed": 0,
            "errors": 0,
            "skipped": [
                {"node_id": "tests/test_a.py::test_one", "reason": "platform"},
                {"node_id": "tests/test_b.py::test_two", "reason": "privilege"},
            ],
            "xfail": 0,
            "xpass": 0,
            "rerun_count": 0,
        },
    )
    _write(
        evidence / "SHAREABLE_EVIDENCE_INDEX.json",
        {
            "source_commit": commit,
            "source_tree": tree,
            "private_path_match_count": 0,
            "secret_match_count": 0,
            "missing_indexed_file_count": 0,
            "unexpected_shareable_file_count": 0,
        },
    )
    _write(
        evidence / "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
        {
            "expected_hermes_version": "0.19.1",
            "result": "compatible",
            "support_matrix_changed": False,
            "active_instance_touched": False,
            "hermes_source": {"commit": "f" * 40},
        },
    )
    _write(
        evidence / "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
        {
            "expected_hermes_version": "0.20.6",
            "result": "incompatible",
            "support_matrix_changed": False,
            "active_instance_touched": False,
            "reason": "provider_load",
            "stages": {
                "candidate_install": "compatible",
                "provider_load": "incompatible",
            },
        },
    )
    _write(
        evidence / "ACTIVE_ISOLATION.json",
        {"active_instance_touched": False, "result": "passed"},
    )
    snapshot = {
        "number": 57,
        "state": "open",
        "draft": True,
        "head": {"ref": "scope-recall/2.0.0-rc-audit-remediation", "sha": commit},
        "base": {"ref": "main", "sha": "1" * 40},
        "body": "pending",
    }
    marker = module.expected_marker(snapshot, evidence)
    snapshot["body"] = module.render_pr_body(marker)
    return module, evidence, snapshot, marker


def test_exact_pr_marker_matches_all_candidate_evidence(tmp_path: Path):
    module, evidence, snapshot, marker = _fixture(tmp_path)

    receipt = module.verify_snapshot(snapshot, evidence)

    assert receipt["result"] == "passed"
    assert receipt["candidate_commit"] == marker["candidate_commit"]
    assert marker["hermes_support"]["probe_result"] == "incompatible"
    assert marker["hermes_support"]["probe_support_claim"] is False


@pytest.mark.parametrize(
    ("field", "nested", "value"),
    [
        ("candidate_commit", None, "9" * 40),
        ("candidate_tree", None, "8" * 40),
        ("artifacts", ("wheel", "sha256"), "7" * 64),
        ("ci", ("run_id",), "999999"),
        ("local_tests", ("passed",), 9),
        ("hermes_support", ("probe_result",), "compatible"),
    ],
)
def test_pr_marker_rejects_obsolete_candidate_metadata(
    tmp_path: Path,
    field: str,
    nested: tuple[str, ...] | None,
    value: object,
):
    module, evidence, snapshot, marker = _fixture(tmp_path)
    stale = copy.deepcopy(marker)
    if nested is None:
        stale[field] = value
    else:
        target = stale[field]
        for key in nested[:-1]:
            target = target[key]
        target[nested[-1]] = value
    snapshot["body"] = module.render_pr_body(stale)

    with pytest.raises(module.PRCandidateMetadataError, match="differs"):
        module.verify_snapshot(snapshot, evidence)


def test_pr_marker_requires_one_unambiguous_structured_block(tmp_path: Path):
    module, evidence, snapshot, marker = _fixture(tmp_path)
    snapshot["body"] += "\n" + module.render_pr_body(marker)

    with pytest.raises(module.PRCandidateMetadataError, match="exactly one"):
        module.verify_snapshot(snapshot, evidence)


def test_pr_metadata_check_refuses_non_draft_pr(tmp_path: Path):
    module, evidence, snapshot, _marker = _fixture(tmp_path)
    snapshot["draft"] = False

    with pytest.raises(module.PRCandidateMetadataError, match="Draft"):
        module.verify_snapshot(snapshot, evidence)


@pytest.mark.parametrize(
    ("filename", "mutate", "message"),
    [
        (
            "CANDIDATE_MANIFEST.json",
            lambda payload: payload["artifacts"][0].update({"sha256": "9" * 64}),
            "artifacts differ",
        ),
        (
            "PYTEST_SKIP_REPORT.json",
            lambda payload: payload.update({"failed": 1}),
            "test accounting",
        ),
        (
            "SHAREABLE_EVIDENCE_INDEX.json",
            lambda payload: payload.update({"private_path_match_count": 1}),
            "closure is not clean",
        ),
        (
            "REMOTE_CI_BINDING.json",
            lambda payload: payload.update({"workflow_path": ".github/workflows/other.yml"}),
            "does not match candidate",
        ),
    ],
)
def test_pr_metadata_check_refuses_internally_inconsistent_evidence(
    tmp_path: Path,
    filename: str,
    mutate,
    message: str,
):
    module, evidence, snapshot, _marker = _fixture(tmp_path)
    path = evidence / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    _write(path, payload)

    with pytest.raises(module.PRCandidateMetadataError, match=message):
        module.expected_marker(snapshot, evidence)
