"""Draft-PR metadata must remain bound to the exact frozen candidate."""

from __future__ import annotations

import copy
import hashlib
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_shareable_entry(evidence: Path, filename: str) -> None:
    index_path = evidence / "SHAREABLE_EVIDENCE_INDEX.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    entry = next(item for item in payload["files"] if item["path"] == filename)
    source = evidence / filename
    entry["sha256"] = _sha256(source)
    entry["size_bytes"] = source.stat().st_size
    if filename == "CANDIDATE_MANIFEST.json":
        payload["candidate_manifest_sha256"] = entry["sha256"]
    if filename == "BUILD_PROVENANCE.json":
        payload["build_provenance_sha256"] = entry["sha256"]
    _write(index_path, payload)


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
            "candidate_mode": module.FINAL_CANDIDATE_MODE,
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
            "hermes": {
                "commit": module.SUPPORTED_HERMES_COMMIT,
                "tree": module.SUPPORTED_HERMES_TREE,
                "clean": True,
                "version": module.SUPPORTED_HERMES_VERSION,
            },
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
            "source_commit": commit,
            "source_tree": tree,
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
        evidence / "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
        {
            "expected_hermes_version": module.SUPPORTED_HERMES_VERSION,
            "observed_hermes_version": module.SUPPORTED_HERMES_VERSION,
            "result": "compatible",
            "support_matrix_changed": False,
            "active_instance_touched": False,
            "hermes_source": {
                "commit": module.SUPPORTED_HERMES_COMMIT,
                "tree": module.SUPPORTED_HERMES_TREE,
                "clean": True,
            },
        },
    )
    _write(
        evidence / "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
        {
            "expected_hermes_version": "0.20.6",
            "observed_hermes_version": "0.20.6",
            "result": "incompatible",
            "support_matrix_changed": False,
            "active_instance_touched": False,
            "reason": "provider_load",
            "stages": {
                "candidate_install": "compatible",
                "provider_load": "incompatible",
            },
            "hermes_source": {
                "commit": "d" * 40,
                "tree": "e" * 40,
                "clean": True,
            },
        },
    )
    _write(
        evidence / "ACTIVE_ISOLATION.json",
        {
            "environment_boundary": {"active_instance_touched": False},
            "result": "passed",
        },
    )
    receipt_base = {
        "source_commit": commit,
        "source_tree": tree,
        "artifact_sha256": wheel["sha256"],
        "exit_code": 0,
        "environment_boundary": {"active_instance_touched": False},
        "result": "passed",
    }
    _write(
        evidence / module.ISSUE_51_RECEIPT,
        {
            **receipt_base,
            "schema_version": "scope-recall.test-receipt.v1",
            "details": {
                "node_ids": ["tests/test_issue_51_regression.py::test_issue_51_regression"],
                "issue_51_regression": {
                    "visible_memory_count": 2_046,
                    "legacy_pending_count": 1_136,
                    "legacy_sql_mutation_count": 0,
                    "scope_wide_fanout_count": 0,
                    "operator_action_required": True,
                    "backup_verified": True,
                    "cleanup_idempotent_replay": True,
                },
            },
        },
    )
    _write(
        evidence / module.ISSUE_58_RECEIPT,
        {
            **receipt_base,
            "schema_version": "scope-recall.writer-lease-handoff.v1",
            "details": {
                "node_ids": [
                    "tests/test_writer_idle_handoff.py::test_process_wide_idle_handoff_allows_real_second_process_commit"
                ],
                "writer_lease_handoff": {
                    "idle_release_seconds": 1800.0,
                    "process_count": 2,
                    "same_process_provider_count": 2,
                    "simultaneous_writer_observed": False,
                    "accepted_work_lost": False,
                    "holder_count_after_release": 0,
                    "connection_pin_count_after_release": 0,
                    "result": "passed",
                },
            },
        },
    )
    indexed_files = []
    for filename in module.CONSUMED_EVIDENCE_FILES:
        path = evidence / filename
        indexed_files.append(
            {
                "path": filename,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "classification": "shareable",
            }
        )
    _write(
        evidence / "SHAREABLE_EVIDENCE_INDEX.json",
        {
            "schema_version": "scope-recall.shareable-evidence-index.v1",
            "source_commit": commit,
            "source_tree": tree,
            "build_provenance_sha256": _sha256(
                evidence / "BUILD_PROVENANCE.json"
            ),
            "candidate_manifest_sha256": _sha256(
                evidence / "CANDIDATE_MANIFEST.json"
            ),
            "artifact_sha256": sorted((wheel["sha256"], sdist["sha256"])),
            "private_path_finding_count": 0,
            "private_path_match_count": 0,
            "secret_finding_count": 0,
            "secret_match_count": 0,
            "missing_indexed_file_count": 0,
            "unexpected_shareable_file_count": 0,
            "file_count": len(indexed_files),
            "files": indexed_files,
        },
    )
    repository = {
        "full_name": module.EXPECTED_REPOSITORY_FULL_NAME,
        "id": module.EXPECTED_REPOSITORY_ID,
    }
    snapshot = {
        "number": module.EXPECTED_PR_NUMBER,
        "state": "open",
        "draft": True,
        "updated_at": "2026-08-29T12:34:56Z",
        "head": {
            "ref": "scope-recall/2.0.0-rc-audit-remediation",
            "sha": commit,
            "repo": dict(repository),
        },
        "base": {"ref": "main", "sha": "1" * 40, "repo": dict(repository)},
        "body": "pending",
    }
    marker = module.expected_marker(snapshot, evidence)
    snapshot["body"] = module.render_pr_body(marker)
    raw_path = tmp_path / module.RAW_SNAPSHOT_NAME
    _write(raw_path, snapshot)
    return module, evidence, snapshot, marker, raw_path


def test_exact_pr_marker_matches_all_candidate_evidence(tmp_path: Path):
    module, evidence, _snapshot, marker, raw_path = _fixture(tmp_path)

    receipt = module.verify_snapshot_file(
        raw_path,
        evidence,
        expected_head_sha=marker["candidate_commit"],
    )

    assert receipt["result"] == "passed"
    assert receipt["candidate_commit"] == marker["candidate_commit"]
    assert receipt["repository"] == {
        "full_name": module.EXPECTED_REPOSITORY_FULL_NAME,
        "id": module.EXPECTED_REPOSITORY_ID,
    }
    assert receipt["pr"]["updated_at"] == "2026-08-29T12:34:56Z"
    assert receipt["raw_source"]["sha256"] == _sha256(raw_path)
    assert marker["hermes_support"]["probe_result"] == "incompatible"
    assert marker["hermes_support"]["probe_support_claim"] is False
    assert marker["hermes_support"]["supported_tree"] == module.SUPPORTED_HERMES_TREE
    assert marker["hermes_support"]["supported_clean"] is True
    assert marker["hermes_support"]["probe_commit"] == "d" * 40
    assert marker["hermes_support"]["probe_tree"] == "e" * 40
    assert marker["hermes_support"]["probe_clean"] is True
    assert marker["issue_disposition"]["51"]["receipt_sha256"] == _sha256(
        evidence / module.ISSUE_51_RECEIPT
    )
    assert marker["issue_disposition"]["58"]["receipt_sha256"] == _sha256(
        evidence / module.ISSUE_58_RECEIPT
    )


def test_public_review_binding_is_a_non_circular_second_layer(tmp_path: Path):
    module, evidence, _snapshot, marker, raw_path = _fixture(tmp_path)
    receipt = module.verify_snapshot_file(
        raw_path,
        evidence,
        expected_head_sha=marker["candidate_commit"],
    )
    receipt_path = tmp_path / module.RECEIPT_NAME
    _write(receipt_path, receipt)

    public_binding = module.public_review_binding_index(
        receipt,
        receipt_sha256=_sha256(receipt_path),
    )

    assert public_binding["result"] == "passed"
    assert public_binding["repository"]["id"] == module.EXPECTED_REPOSITORY_ID
    assert public_binding["pr"]["number"] == module.EXPECTED_PR_NUMBER
    assert public_binding["pr"]["updated_at"] == "2026-08-29T12:34:56Z"
    assert public_binding["remote_pr_raw_source"]["sha256"] == _sha256(raw_path)
    assert public_binding["pr_candidate_metadata"]["sha256"] == _sha256(
        receipt_path
    )
    assert public_binding["core_shareable_evidence_index"]["sha256"] == _sha256(
        evidence / "SHAREABLE_EVIDENCE_INDEX.json"
    )
    assert module.PUBLIC_BINDING_NAME not in json.dumps(public_binding)
    core = json.loads(
        (evidence / "SHAREABLE_EVIDENCE_INDEX.json").read_text(encoding="utf-8")
    )
    assert not set(module.PUBLIC_BINDING_ONLY_FILES).intersection(
        entry["path"] for entry in core["files"]
    )


def test_cli_writes_sanitized_receipt_and_public_binding(tmp_path: Path):
    module, evidence, _snapshot, marker, raw_path = _fixture(tmp_path)
    receipt_path = tmp_path / module.RECEIPT_NAME
    public_path = tmp_path / module.PUBLIC_BINDING_NAME

    result = module.main(
        [
            "--pr-snapshot",
            str(raw_path),
            "--evidence-dir",
            str(evidence),
            "--expected-head-sha",
            marker["candidate_commit"],
            "--output",
            str(receipt_path),
            "--public-binding-output",
            str(public_path),
        ]
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    public_binding = json.loads(public_path.read_text(encoding="utf-8"))
    assert result == 0
    assert receipt["raw_source"]["classification"] == "local-restricted"
    assert "body" not in json.dumps(receipt)
    assert public_binding["pr_candidate_metadata"]["sha256"] == _sha256(
        receipt_path
    )


def test_rendered_pr_body_has_issue_closure_dispositions(tmp_path: Path):
    module, _evidence, snapshot, marker, _raw_path = _fixture(tmp_path)

    assert "Closes #51" in snapshot["body"]
    assert "Closes #58" in snapshot["body"]
    assert "#59 by @tutan0558" in snapshot["body"]
    assert marker["issue_disposition"]["51"]["closes_on_merge"] is True
    assert marker["issue_disposition"]["58"]["closes_on_merge"] is True


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
    module, evidence, snapshot, marker, raw_path = _fixture(tmp_path)
    stale = copy.deepcopy(marker)
    if nested is None:
        stale[field] = value
    else:
        target = stale[field]
        for key in nested[:-1]:
            target = target[key]
        target[nested[-1]] = value
    snapshot["body"] = module.render_pr_body(stale)
    _write(raw_path, snapshot)

    with pytest.raises(module.PRCandidateMetadataError, match="differs"):
        module.verify_snapshot_file(
            raw_path,
            evidence,
            expected_head_sha=marker["candidate_commit"],
        )


def test_pr_marker_requires_one_unambiguous_structured_block(tmp_path: Path):
    module, evidence, snapshot, marker, raw_path = _fixture(tmp_path)
    snapshot["body"] += "\n" + module.render_pr_body(marker)
    _write(raw_path, snapshot)

    with pytest.raises(module.PRCandidateMetadataError, match="exactly one"):
        module.verify_snapshot_file(
            raw_path,
            evidence,
            expected_head_sha=marker["candidate_commit"],
        )


def test_pr_metadata_check_refuses_non_draft_pr(tmp_path: Path):
    module, evidence, snapshot, marker, raw_path = _fixture(tmp_path)
    snapshot["draft"] = False
    _write(raw_path, snapshot)

    with pytest.raises(module.PRCandidateMetadataError, match="Draft"):
        module.verify_snapshot_file(
            raw_path,
            evidence,
            expected_head_sha=marker["candidate_commit"],
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda module, snapshot: snapshot["base"]["repo"].update(
                {"id": module.EXPECTED_REPOSITORY_ID + 1}
            ),
            "repository identity",
        ),
        (
            lambda module, snapshot: snapshot.update(
                {"number": module.EXPECTED_PR_NUMBER + 1}
            ),
            "must be #57",
        ),
        (
            lambda _module, snapshot: snapshot.update(
                {"updated_at": "2026-08-29T12:34:56"}
            ),
            "lacks timezone",
        ),
        (
            lambda _module, snapshot: snapshot["head"].update(
                {"ref": "scope-recall/unrelated"}
            ),
            "head must remain",
        ),
    ],
)
def test_remote_pr_identity_is_exact_and_timestamped(
    tmp_path: Path,
    mutate,
    message: str,
):
    module, evidence, snapshot, marker, raw_path = _fixture(tmp_path)
    mutate(module, snapshot)
    _write(raw_path, snapshot)

    with pytest.raises(module.PRCandidateMetadataError, match=message):
        module.verify_snapshot_file(
            raw_path,
            evidence,
            expected_head_sha=marker["candidate_commit"],
        )


def test_explicit_expected_head_sha_cannot_drift_from_candidate(tmp_path: Path):
    module, evidence, _snapshot, _marker, raw_path = _fixture(tmp_path)

    with pytest.raises(module.PRCandidateMetadataError, match="explicit expected"):
        module.verify_snapshot_file(
            raw_path,
            evidence,
            expected_head_sha="9" * 40,
        )


def test_consumed_evidence_requires_exact_shareable_sha(tmp_path: Path):
    module, evidence, snapshot, _marker, _raw_path = _fixture(tmp_path)
    path = evidence / module.ISSUE_51_RECEIPT
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["details"]["issue_51_regression"]["visible_memory_count"] = 2_047
    _write(path, payload)

    with pytest.raises(module.PRCandidateMetadataError, match="SHA differs"):
        module.expected_marker(snapshot, evidence)


def test_issue_58_disposition_requires_positive_default_receipt(tmp_path: Path):
    module, evidence, snapshot, _marker, _raw_path = _fixture(tmp_path)
    path = evidence / module.ISSUE_58_RECEIPT
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["details"]["writer_lease_handoff"]["idle_release_seconds"] = 0.0
    _write(path, payload)
    _refresh_shareable_entry(evidence, module.ISSUE_58_RECEIPT)

    with pytest.raises(module.PRCandidateMetadataError, match="Issue #58"):
        module.expected_marker(snapshot, evidence)


def test_core_index_refuses_public_binding_layer_files(tmp_path: Path):
    module, evidence, snapshot, _marker, _raw_path = _fixture(tmp_path)
    index_path = evidence / "SHAREABLE_EVIDENCE_INDEX.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["files"].append(
        {
            "path": module.RECEIPT_NAME,
            "sha256": "9" * 64,
            "size_bytes": 1,
            "classification": "shareable",
        }
    )
    index["file_count"] += 1
    _write(index_path, index)

    with pytest.raises(module.PRCandidateMetadataError, match="public binding layer"):
        module.expected_marker(snapshot, evidence)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda index: index["files"].__setitem__(
                slice(None),
                [
                    entry
                    for entry in index["files"]
                    if entry["path"] != "REMOTE_CI_BINDING.json"
                ],
            ),
            "not bound",
        ),
        (
            lambda index: index["files"].append(dict(index["files"][0])),
            "duplicate path",
        ),
        (
            lambda index: next(
                entry
                for entry in index["files"]
                if entry["path"] == "ACTIVE_ISOLATION.json"
            ).update({"classification": "local-restricted"}),
            "not classified shareable",
        ),
    ],
)
def test_every_consumed_file_is_uniquely_shareable_bound(
    tmp_path: Path,
    mutate,
    message: str,
):
    module, evidence, snapshot, _marker, _raw_path = _fixture(tmp_path)
    index_path = evidence / "SHAREABLE_EVIDENCE_INDEX.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    mutate(index)
    index["file_count"] = len(index["files"])
    _write(index_path, index)

    with pytest.raises(module.PRCandidateMetadataError, match=message):
        module.expected_marker(snapshot, evidence)


@pytest.mark.parametrize(
    ("filename", "mutate", "message"),
    [
        (
            "CANDIDATE_MANIFEST.json",
            lambda payload: payload["artifacts"][0].update({"sha256": "9" * 64}),
            "artifacts differ",
        ),
        (
            "CANDIDATE_MANIFEST.json",
            lambda payload: payload["hermes"].update({"tree": "9" * 40}),
            "Hermes identity does not match",
        ),
        (
            "CANDIDATE_MANIFEST.json",
            lambda payload: payload.update({"candidate_mode": "development-snapshot"}),
            "not a final release candidate",
        ),
        (
            "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
            lambda payload: payload["hermes_source"].update({"commit": "9" * 40}),
            "not bound to the supported source",
        ),
        (
            "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
            lambda payload: payload.pop("hermes_source"),
            "hermes_source",
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
        (
            "ACTIVE_ISOLATION.json",
            lambda payload: payload["environment_boundary"].update(
                {"active_instance_touched": True}
            ),
            "active isolation boundary changed",
        ),
        (
            "PYTEST_SKIP_REPORT.json",
            lambda payload: payload.update({"source_commit": "f" * 40}),
            "test honesty source identity differs from candidate",
        ),
        (
            "PYTEST_SKIP_REPORT.json",
            lambda payload: payload.update({"source_tree": "f" * 40}),
            "test honesty source identity differs from candidate",
        ),
    ],
)
def test_pr_metadata_check_refuses_internally_inconsistent_evidence(
    tmp_path: Path,
    filename: str,
    mutate,
    message: str,
):
    module, evidence, snapshot, _marker, _raw_path = _fixture(tmp_path)
    path = evidence / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    _write(path, payload)
    if filename != "SHAREABLE_EVIDENCE_INDEX.json":
        _refresh_shareable_entry(evidence, filename)

    with pytest.raises(module.PRCandidateMetadataError, match=message):
        module.expected_marker(snapshot, evidence)
