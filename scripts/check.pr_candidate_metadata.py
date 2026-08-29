#!/usr/bin/env python3
"""Verify a saved draft-PR body against exact local release-candidate evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Sequence


MARKER_SCHEMA_VERSION = "scope-recall.pr-candidate-metadata.v2"
RECEIPT_SCHEMA_VERSION = "scope-recall.pr-candidate-metadata-check.v2"
PUBLIC_BINDING_SCHEMA_VERSION = "scope-recall.public-review-binding-index.v1"
VERIFIER_NAME = "scripts/check.pr_candidate_metadata.py"
VERIFIER_VERSION = "2.0.0"
FINAL_CANDIDATE_MODE = "final-release-candidate"
SUPPORTED_HERMES_VERSION = "0.19.1"
SUPPORTED_HERMES_COMMIT = "cc4cab2f592e60a197e796506de9168f74baf3ea"
SUPPORTED_HERMES_TREE = "fcdc6093750ed0a3a556e20927799d7245ba65e4"
EXPECTED_REPOSITORY_FULL_NAME = "410979729/scope-recall-hermes"
EXPECTED_REPOSITORY_ID = 1239699520
EXPECTED_PR_NUMBER = 57
EXPECTED_BASE_BRANCH = "main"
EXPECTED_HEAD_BRANCH = "scope-recall/2.0.0-rc-audit-remediation"
RAW_SNAPSHOT_NAME = "PR_CANDIDATE_METADATA_SOURCE.raw.json"
RECEIPT_NAME = "PR_CANDIDATE_METADATA.json"
PUBLIC_BINDING_NAME = "PUBLIC_REVIEW_BINDING_INDEX.json"
ISSUE_51_RECEIPT = "ISSUE_51_REGRESSION.json"
ISSUE_58_RECEIPT = "WRITER_LEASE_HANDOFF_REHEARSAL.json"
CONSUMED_EVIDENCE_FILES = (
    "CANDIDATE_MANIFEST.json",
    "BUILD_PROVENANCE.json",
    "REMOTE_CI_BINDING.json",
    "PYTEST_SKIP_REPORT.json",
    "ACTIVE_ISOLATION.json",
    "HERMES_COMPATIBILITY_PROBE.0.19.1.json",
    "HERMES_COMPATIBILITY_PROBE.0.20.6.json",
    ISSUE_51_RECEIPT,
    ISSUE_58_RECEIPT,
)
PUBLIC_BINDING_ONLY_FILES = frozenset(
    {
        RAW_SNAPSHOT_NAME,
        RECEIPT_NAME,
        PUBLIC_BINDING_NAME,
        "PR_CANDIDATE_BODY.md",
    }
)
MARKER_START = "<!-- scope-recall-candidate-metadata:start -->"
MARKER_END = "<!-- scope-recall-candidate-metadata:end -->"
MARKER_RE = re.compile(
    re.escape(MARKER_START)
    + r"\s*```json\s*(?P<payload>\{.*?\})\s*```\s*"
    + re.escape(MARKER_END),
    re.DOTALL,
)


class PRCandidateMetadataError(RuntimeError):
    """Raised when the public review surface is stale or ambiguous."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _required_sha256(payload: Mapping[str, object], key: str) -> str:
    return _validated_sha256_value(_required_text(payload, key), field=key)


def _validated_sha256_value(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise PRCandidateMetadataError(f"required SHA-256 is invalid: {field}")
    return normalized


def _validated_git_sha(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise PRCandidateMetadataError(f"required Git SHA is invalid: {field}")
    return normalized


def _required_rfc3339(payload: Mapping[str, object], key: str) -> str:
    value = _required_text(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PRCandidateMetadataError(f"required RFC3339 timestamp is invalid: {key}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PRCandidateMetadataError(f"required RFC3339 timestamp lacks timezone: {key}")
    return value


def _load_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PRCandidateMetadataError(f"invalid JSON input: {path.name}") from exc
    if not isinstance(payload, dict):
        raise PRCandidateMetadataError(f"JSON input must be an object: {path.name}")
    return payload


def _required_object(payload: Mapping[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise PRCandidateMetadataError(f"required object is missing: {key}")
    return value


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise PRCandidateMetadataError(f"required text is missing: {key}")
    return value


def _artifact(provenance: Mapping[str, object], kind: str) -> dict[str, str]:
    payload = _required_object(provenance, kind)
    return {
        "name": _required_text(payload, "name"),
        "sha256": _required_sha256(payload, "sha256"),
    }


def _skip_count(honesty: Mapping[str, object]) -> int:
    skipped = honesty.get("skipped")
    if not isinstance(skipped, list):
        raise PRCandidateMetadataError("test honesty skipped array is missing")
    return len(skipped)


def _nonnegative_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PRCandidateMetadataError(f"required non-negative integer is invalid: {key}")
    return value


def _validated_local_tests(honesty: Mapping[str, object]) -> dict[str, int]:
    if honesty.get("schema_version") != "scope-recall.test-honesty.v1":
        raise PRCandidateMetadataError("test honesty schema is invalid")
    tests = {
        key: _nonnegative_int(honesty, key)
        for key in (
            "collected",
            "passed",
            "failed",
            "errors",
            "xfail",
            "xpass",
            "rerun_count",
        )
    }
    tests["skipped"] = _skip_count(honesty)
    if (
        tests["failed"]
        or tests["errors"]
        or tests["xfail"]
        or tests["xpass"]
        or tests["rerun_count"]
        or tests["collected"] != tests["passed"] + tests["skipped"]
    ):
        raise PRCandidateMetadataError("local final test accounting is not clean")
    return tests


def _candidate_artifact(candidate: Mapping[str, object], kind: str) -> dict[str, str]:
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list):
        raise PRCandidateMetadataError("Candidate Manifest artifacts are missing")
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("kind") == kind
    ]
    if len(matches) != 1:
        raise PRCandidateMetadataError(f"Candidate Manifest {kind} is ambiguous")
    return {
        "name": _required_text(matches[0], "name"),
        "sha256": _required_sha256(matches[0], "sha256"),
    }


def _repository_identity(payload: Mapping[str, object], *, role: str) -> dict[str, object]:
    repository = _required_object(payload, "repo")
    repository_id = repository.get("id")
    if isinstance(repository_id, bool) or not isinstance(repository_id, int):
        raise PRCandidateMetadataError(f"{role} repository ID is invalid")
    identity = {
        "full_name": _required_text(repository, "full_name"),
        "id": repository_id,
    }
    expected = {
        "full_name": EXPECTED_REPOSITORY_FULL_NAME,
        "id": EXPECTED_REPOSITORY_ID,
    }
    if identity != expected:
        raise PRCandidateMetadataError(
            f"{role} repository identity does not match the expected repository"
        )
    return identity


def _clean_git_identity(
    payload: Mapping[str, object],
    *,
    field: str,
) -> dict[str, object]:
    identity = _required_object(payload, field)
    if identity.get("clean") is not True:
        raise PRCandidateMetadataError(f"{field} is not a clean Git identity")
    return {
        "commit": _validated_git_sha(
            identity.get("commit"), field=f"{field}.commit"
        ),
        "tree": _validated_git_sha(identity.get("tree"), field=f"{field}.tree"),
        "clean": True,
    }


def _pr_identity(snapshot: Mapping[str, object]) -> dict[str, object]:
    head = _required_object(snapshot, "head")
    base = _required_object(snapshot, "base")
    number = snapshot.get("number")
    if number != EXPECTED_PR_NUMBER or isinstance(number, bool):
        raise PRCandidateMetadataError(
            f"release-candidate PR must be #{EXPECTED_PR_NUMBER}"
        )
    if snapshot.get("state") != "open" or snapshot.get("draft") is not True:
        raise PRCandidateMetadataError("release-candidate PR must remain open and Draft")
    if _required_text(base, "ref") != EXPECTED_BASE_BRANCH:
        raise PRCandidateMetadataError(
            f"release-candidate PR base must remain {EXPECTED_BASE_BRANCH}"
        )
    if _required_text(head, "ref") != EXPECTED_HEAD_BRANCH:
        raise PRCandidateMetadataError(
            f"release-candidate PR head must remain {EXPECTED_HEAD_BRANCH}"
        )
    base_repository = _repository_identity(base, role="base")
    head_repository = _repository_identity(head, role="head")
    if head_repository != base_repository:
        raise PRCandidateMetadataError("PR head and base repository identities differ")
    return {
        "number": number,
        "state": "open",
        "draft": True,
        "repository": base_repository,
        "branch": _required_text(head, "ref"),
        "head_sha": _validated_git_sha(head.get("sha"), field="head.sha"),
        "base_branch": _required_text(base, "ref"),
        "base_commit": _validated_git_sha(base.get("sha"), field="base.sha"),
        "updated_at": _required_rfc3339(snapshot, "updated_at"),
    }


def _evidence_path(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise PRCandidateMetadataError(f"required evidence file is missing or unsafe: {name}")
    if path.resolve(strict=True).parent != root:
        raise PRCandidateMetadataError(f"required evidence file escapes evidence root: {name}")
    return path


def _shareable_file_bindings(
    shareable: Mapping[str, object],
    *,
    root: Path,
) -> dict[str, str]:
    if shareable.get("schema_version") != "scope-recall.shareable-evidence-index.v1":
        raise PRCandidateMetadataError("shareable evidence index schema is invalid")
    files = shareable.get("files")
    if not isinstance(files, list):
        raise PRCandidateMetadataError("shareable evidence index files are missing")
    file_count = shareable.get("file_count")
    if isinstance(file_count, bool) or file_count != len(files):
        raise PRCandidateMetadataError("shareable evidence index file count is invalid")
    indexed: dict[str, Mapping[str, object]] = {}
    for raw_entry in files:
        if not isinstance(raw_entry, dict):
            raise PRCandidateMetadataError("shareable evidence index entry is invalid")
        name = str(raw_entry.get("path") or "").strip()
        if not name or Path(name).name != name:
            raise PRCandidateMetadataError("shareable evidence index path is unsafe")
        if name in indexed:
            raise PRCandidateMetadataError(
                f"shareable evidence index contains duplicate path: {name}"
            )
        indexed[name] = raw_entry
    forbidden = sorted(set(indexed).intersection(PUBLIC_BINDING_ONLY_FILES))
    if forbidden:
        raise PRCandidateMetadataError(
            "core shareable evidence index contains public binding layer files: "
            + ", ".join(forbidden)
        )
    bindings: dict[str, str] = {}
    for name in CONSUMED_EVIDENCE_FILES:
        entry = indexed.get(name)
        if entry is None:
            raise PRCandidateMetadataError(
                f"consumed evidence is not bound by shareable index: {name}"
            )
        if entry.get("classification") != "shareable":
            raise PRCandidateMetadataError(
                f"consumed evidence is not classified shareable: {name}"
            )
        path = _evidence_path(root, name)
        actual_sha256 = _sha256_file(path)
        if _required_sha256(entry, "sha256") != actual_sha256:
            raise PRCandidateMetadataError(
                f"consumed evidence SHA differs from shareable index: {name}"
            )
        size_bytes = entry.get("size_bytes")
        if isinstance(size_bytes, bool) or size_bytes != path.stat().st_size:
            raise PRCandidateMetadataError(
                f"consumed evidence size differs from shareable index: {name}"
            )
        bindings[name] = actual_sha256
    return bindings


def _validate_issue_receipt(
    receipt: Mapping[str, object],
    *,
    filename: str,
    candidate_commit: str,
    candidate_tree: str,
    artifact_sha256: str,
) -> None:
    expected_schema = (
        "scope-recall.test-receipt.v1"
        if filename == ISSUE_51_RECEIPT
        else "scope-recall.writer-lease-handoff.v1"
    )
    if receipt.get("schema_version") != expected_schema:
        raise PRCandidateMetadataError(f"{filename} schema is invalid")
    boundary = _required_object(receipt, "environment_boundary")
    if (
        receipt.get("result") != "passed"
        or receipt.get("exit_code") != 0
        or receipt.get("source_commit") != candidate_commit
        or receipt.get("source_tree") != candidate_tree
        or receipt.get("artifact_sha256") != artifact_sha256
        or boundary.get("active_instance_touched") is not False
    ):
        raise PRCandidateMetadataError(
            f"{filename} does not prove a passing isolated candidate rehearsal"
        )
    details = _required_object(receipt, "details")
    if filename == ISSUE_51_RECEIPT:
        regression = _required_object(details, "issue_51_regression")
        if (
            regression.get("visible_memory_count") != 2_046
            or regression.get("legacy_pending_count") != 1_136
            or regression.get("legacy_sql_mutation_count") != 0
            or regression.get("scope_wide_fanout_count") != 0
            or regression.get("operator_action_required") is not True
            or regression.get("backup_verified") is not True
            or regression.get("cleanup_idempotent_replay") is not True
        ):
            raise PRCandidateMetadataError(
                f"{filename} does not prove the Issue #51 accident-scale closure"
            )
    else:
        handoff = _required_object(details, "writer_lease_handoff")
        if (
            handoff.get("idle_release_seconds") != 1800.0
            or handoff.get("process_count") != 2
            or handoff.get("same_process_provider_count") != 2
            or handoff.get("simultaneous_writer_observed") is not False
            or handoff.get("accepted_work_lost") is not False
            or handoff.get("holder_count_after_release") != 0
            or handoff.get("connection_pin_count_after_release") != 0
            or handoff.get("result") != "passed"
        ):
            raise PRCandidateMetadataError(
                f"{filename} does not prove the Issue #58 process-wide closure"
            )


def expected_marker(
    pr_snapshot: Mapping[str, object],
    evidence_dir: Path,
) -> dict[str, object]:
    """Build the only acceptable public metadata marker for this PR snapshot."""

    root = evidence_dir.resolve(strict=True)
    candidate_path = _evidence_path(root, "CANDIDATE_MANIFEST.json")
    provenance_path = _evidence_path(root, "BUILD_PROVENANCE.json")
    remote_path = _evidence_path(root, "REMOTE_CI_BINDING.json")
    honesty_path = _evidence_path(root, "PYTEST_SKIP_REPORT.json")
    shareable_path = _evidence_path(root, "SHAREABLE_EVIDENCE_INDEX.json")
    active_isolation_path = _evidence_path(root, "ACTIVE_ISOLATION.json")
    hermes_supported_path = _evidence_path(
        root, "HERMES_COMPATIBILITY_PROBE.0.19.1.json"
    )
    hermes_probe_path = _evidence_path(
        root, "HERMES_COMPATIBILITY_PROBE.0.20.6.json"
    )
    issue_51_path = _evidence_path(root, ISSUE_51_RECEIPT)
    issue_58_path = _evidence_path(root, ISSUE_58_RECEIPT)
    candidate = _load_object(candidate_path)
    provenance = _load_object(provenance_path)
    remote = _load_object(remote_path)
    honesty = _load_object(honesty_path)
    shareable = _load_object(shareable_path)
    active_isolation = _load_object(active_isolation_path)
    hermes_supported = _load_object(hermes_supported_path)
    hermes_probe = _load_object(hermes_probe_path)
    issue_51 = _load_object(issue_51_path)
    issue_58 = _load_object(issue_58_path)
    active_isolation_boundary = _required_object(
        active_isolation, "environment_boundary"
    )
    source = _required_object(candidate, "source")
    candidate_hermes = _required_object(candidate, "hermes")
    pr = _pr_identity(pr_snapshot)
    candidate_commit = _validated_git_sha(source.get("commit"), field="source.commit")
    candidate_tree = _validated_git_sha(source.get("tree"), field="source.tree")
    if _required_text(candidate, "candidate_version") != "2.0.0":
        raise PRCandidateMetadataError("Candidate Manifest version is not 2.0.0")
    if candidate.get("candidate_mode") != FINAL_CANDIDATE_MODE:
        raise PRCandidateMetadataError(
            "Candidate Manifest is not a final release candidate"
        )
    expected_hermes = {
        "commit": SUPPORTED_HERMES_COMMIT,
        "tree": SUPPORTED_HERMES_TREE,
        "clean": True,
        "version": SUPPORTED_HERMES_VERSION,
    }
    if candidate_hermes != expected_hermes:
        raise PRCandidateMetadataError(
            "Candidate Manifest Hermes identity does not match the supported baseline"
        )
    if (
        candidate.get("private_artifacts_included") is not False
        or candidate.get("authorization")
        != {"merge": False, "tag": False, "release": False, "deploy": False}
    ):
        raise PRCandidateMetadataError("Candidate Manifest release boundary changed")
    if (
        provenance.get("source_commit") != candidate_commit
        or provenance.get("source_tree") != candidate_tree
    ):
        raise PRCandidateMetadataError("build provenance source differs from candidate")
    source_manifest = _required_object(source, "manifest")
    source_manifest_sha256 = _required_sha256(source_manifest, "manifest_sha256")
    provenance_manifest_sha256 = _required_sha256(
        provenance, "source_manifest_sha256"
    )
    if provenance_manifest_sha256 != source_manifest_sha256:
        raise PRCandidateMetadataError("source manifest differs from build provenance")
    provenance_artifacts = {
        kind: _artifact(provenance, kind) for kind in ("wheel", "sdist")
    }
    if any(
        _candidate_artifact(candidate, kind) != provenance_artifacts[kind]
        for kind in ("wheel", "sdist")
    ):
        raise PRCandidateMetadataError("Candidate Manifest artifacts differ from provenance")
    consumed_evidence_sha256 = _shareable_file_bindings(
        shareable,
        root=root,
    )
    _validate_issue_receipt(
        issue_51,
        filename=ISSUE_51_RECEIPT,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        artifact_sha256=provenance_artifacts["wheel"]["sha256"],
    )
    _validate_issue_receipt(
        issue_58,
        filename=ISSUE_58_RECEIPT,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        artifact_sha256=provenance_artifacts["wheel"]["sha256"],
    )
    if pr["head_sha"] != candidate_commit:
        raise PRCandidateMetadataError("PR head does not match Candidate Manifest")
    if (
        remote.get("schema_version") != "scope-recall.remote-ci-binding.v1"
        or remote.get("repository") != "410979729/scope-recall-hermes"
        or remote.get("workflow_path") != ".github/workflows/ci.yml"
        or remote.get("run_status") != "completed"
        or remote.get("head_sha") != candidate_commit
    ):
        raise PRCandidateMetadataError("remote CI binding does not match candidate")
    if remote.get("head_branch") != pr["branch"]:
        raise PRCandidateMetadataError("remote CI branch does not match PR branch")
    if remote.get("run_conclusion") != "success" or remote.get(
        "required_job_conclusion"
    ) != "success" or remote.get("required_job") != "ci-required":
        raise PRCandidateMetadataError("remote CI binding is not successful")
    run_attempt = remote.get("run_attempt")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt <= 0:
        raise PRCandidateMetadataError("remote CI run attempt is invalid")
    if hermes_supported.get("result") != "compatible":
        raise PRCandidateMetadataError("frozen supported Hermes probe is not compatible")
    if (
        hermes_supported.get("support_matrix_changed") is not False
        or hermes_probe.get("support_matrix_changed") is not False
        or hermes_supported.get("active_instance_touched") is not False
        or hermes_probe.get("active_instance_touched") is not False
        or active_isolation.get("result") != "passed"
        or active_isolation_boundary.get("active_instance_touched") is not False
    ):
        raise PRCandidateMetadataError("Hermes support or active isolation boundary changed")
    if (
        hermes_supported.get("expected_hermes_version") != SUPPORTED_HERMES_VERSION
        or hermes_supported.get("observed_hermes_version")
        != SUPPORTED_HERMES_VERSION
        or hermes_probe.get("expected_hermes_version") != "0.20.6"
        or hermes_probe.get("observed_hermes_version") != "0.20.6"
        or hermes_probe.get("result") not in {"compatible", "incompatible"}
    ):
        raise PRCandidateMetadataError("Hermes 0.20.6 probe result is invalid")
    supported_source = _required_object(hermes_supported, "hermes_source")
    probe_source = _clean_git_identity(hermes_probe, field="hermes_source")
    expected_supported_source = {
        "commit": SUPPORTED_HERMES_COMMIT,
        "tree": SUPPORTED_HERMES_TREE,
        "clean": True,
    }
    if supported_source != expected_supported_source:
        raise PRCandidateMetadataError(
            "Hermes 0.19.1 probe is not bound to the supported source"
        )
    if any(
        candidate_hermes.get(field) != supported_source.get(field)
        for field in ("commit", "tree", "clean")
    ):
        raise PRCandidateMetadataError(
            "Candidate Manifest Hermes identity differs from the 0.19.1 probe"
        )
    probe_stages = _required_object(hermes_probe, "stages")
    if probe_stages.get("provider_load") != hermes_probe.get("result"):
        raise PRCandidateMetadataError("Hermes 0.20.6 provider result is inconsistent")
    unknowns: list[str] = []
    ci_run_ids = candidate.get("ci_run_ids")
    if not isinstance(ci_run_ids, list) or ci_run_ids != [str(remote.get("run_id") or "")]:
        raise PRCandidateMetadataError("Candidate Manifest CI run does not match binding")
    local_tests = _validated_local_tests(honesty)
    if (
        shareable.get("source_commit") != candidate_commit
        or shareable.get("source_tree") != candidate_tree
        or shareable.get("build_provenance_sha256")
        != consumed_evidence_sha256["BUILD_PROVENANCE.json"]
        or shareable.get("candidate_manifest_sha256")
        != consumed_evidence_sha256["CANDIDATE_MANIFEST.json"]
        or shareable.get("artifact_sha256")
        != sorted(
            (
                provenance_artifacts["wheel"]["sha256"],
                provenance_artifacts["sdist"]["sha256"],
            )
        )
        or any(
            shareable.get(field) != 0
            for field in (
                "private_path_finding_count",
                "private_path_match_count",
                "secret_finding_count",
                "secret_match_count",
                "missing_indexed_file_count",
                "unexpected_shareable_file_count",
            )
        )
    ):
        raise PRCandidateMetadataError("shareable evidence closure is not clean")
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "repository": pr["repository"],
        "pr_number": pr["number"],
        "branch": pr["branch"],
        "base_branch": pr["base_branch"],
        "base_commit": pr["base_commit"],
        "version": _required_text(candidate, "candidate_version"),
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "ci": {
            "run_id": _required_text(remote, "run_id"),
            "run_attempt": remote.get("run_attempt"),
            "run_result": _required_text(remote, "run_conclusion"),
            "required_job": _required_text(remote, "required_job"),
            "required_job_result": _required_text(remote, "required_job_conclusion"),
        },
        "artifacts": provenance_artifacts,
        "source_manifest_sha256": provenance_manifest_sha256,
        "candidate_manifest_sha256": _sha256_file(candidate_path),
        "shareable_evidence_index_sha256": _sha256_file(shareable_path),
        "issue_disposition": {
            "51": {
                "disposition": "CLOSED_IN_CANDIDATE",
                "receipt": ISSUE_51_RECEIPT,
                "receipt_sha256": consumed_evidence_sha256[ISSUE_51_RECEIPT],
                "closes_on_merge": True,
            },
            "58": {
                "disposition": "CLOSED_IN_CANDIDATE",
                "receipt": ISSUE_58_RECEIPT,
                "receipt_sha256": consumed_evidence_sha256[ISSUE_58_RECEIPT],
                "closes_on_merge": True,
            },
        },
        "local_tests": local_tests,
        "hermes_support": {
            "supported_version": _required_text(
                hermes_supported, "expected_hermes_version"
            ),
            "supported_commit": _required_text(supported_source, "commit"),
            "supported_tree": _required_text(supported_source, "tree"),
            "supported_clean": supported_source.get("clean") is True,
            "supported_result": _required_text(hermes_supported, "result"),
            "probe_version": _required_text(hermes_probe, "expected_hermes_version"),
            "probe_commit": probe_source["commit"],
            "probe_tree": probe_source["tree"],
            "probe_clean": True,
            "probe_result": _required_text(hermes_probe, "result"),
            "probe_reason": _required_text(hermes_probe, "reason"),
            "provider_load_result": _required_text(probe_stages, "provider_load"),
            "probe_support_claim": False,
            "support_matrix_changed": False,
        },
        "active_environment_touched": False,
        "remaining_unknown": unknowns,
    }


def parse_marker(body: str) -> dict[str, object]:
    matches = list(MARKER_RE.finditer(str(body or "")))
    if len(matches) != 1:
        raise PRCandidateMetadataError("PR body must contain exactly one candidate marker")
    try:
        payload = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise PRCandidateMetadataError("PR candidate marker is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PRCandidateMetadataError("PR candidate marker must be an object")
    return payload


def render_pr_body(marker: Mapping[str, object]) -> str:
    support = _required_object(marker, "hermes_support")
    ci = _required_object(marker, "ci")
    tests = _required_object(marker, "local_tests")
    issues = _required_object(marker, "issue_disposition")
    issue_51 = _required_object(issues, "51")
    issue_58 = _required_object(issues, "58")
    return (
        "# Scope Recall 2.0 release candidate\n\n"
        "This pull request remains Draft pending an independent re-audit. "
        "It does not authorize merge, deploy, tag, or release.\n\n"
        "## Verified candidate\n\n"
        f"- Exact commit: `{marker['candidate_commit']}`\n"
        f"- Exact tree: `{marker['candidate_tree']}`\n"
        f"- Base: `{marker['base_branch']}` / `{marker['base_commit']}`\n"
        f"- Remote CI: run `{ci['run_id']}`, attempt `{ci['run_attempt']}`, "
        f"required result `{ci['required_job_result']}`\n"
        f"- Local final test: {tests['passed']} passed, {tests['skipped']} skipped, "
        f"{tests['xfail']} xfail, {tests['rerun_count']} reruns\n\n"
        "## Hermes support boundary\n\n"
        f"Release support remains pinned to Hermes {support['supported_version']} / "
        f"`{support['supported_commit']}`. The isolated Hermes "
        f"{support['probe_version']} probe is {support['probe_result']} at "
        f"`{support['probe_reason']}`. No {support['probe_version']} support claim "
        "is made by this candidate.\n\n"
        "## Issue dispositions\n\n"
        f"- #51: `{issue_51['disposition']}`; receipt "
        f"`{issue_51['receipt']}` / `{issue_51['receipt_sha256']}`\n"
        f"- #58: `{issue_58['disposition']}`; receipt "
        f"`{issue_58['receipt']}` / `{issue_58['receipt_sha256']}`\n\n"
        "Design reference for #58: #59 by @tutan0558. This candidate "
        "independently reimplements the handoff as a process-wide coordinator "
        "for the 2.0 writer-authority model.\n\n"
        "Closes #51\n\n"
        "Closes #58\n\n"
        f"{MARKER_START}\n```json\n"
        + json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True)
        + f"\n```\n{MARKER_END}\n"
    )


def verify_snapshot(
    pr_snapshot: Mapping[str, object],
    evidence_dir: Path,
    *,
    raw_snapshot_sha256: str,
    expected_head_sha: str,
) -> dict[str, object]:
    expected_head = _validated_git_sha(expected_head_sha, field="expected_head_sha")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_snapshot_sha256):
        raise PRCandidateMetadataError("raw PR snapshot SHA-256 is invalid")
    expected = expected_marker(pr_snapshot, evidence_dir)
    if expected["candidate_commit"] != expected_head:
        raise PRCandidateMetadataError(
            "explicit expected head SHA does not match the final candidate"
        )
    actual = parse_marker(_required_text(pr_snapshot, "body"))
    if actual != expected:
        differing = sorted(
            key for key in set(actual) | set(expected) if actual.get(key) != expected.get(key)
        )
        raise PRCandidateMetadataError(
            "PR candidate marker differs from exact evidence: " + ", ".join(differing)
        )
    pr = _pr_identity(pr_snapshot)
    root = evidence_dir.resolve(strict=True)
    shareable_path = _evidence_path(root, "SHAREABLE_EVIDENCE_INDEX.json")
    evidence_sha256 = _shareable_file_bindings(
        _load_object(shareable_path),
        root=root,
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verifier": {
            "name": VERIFIER_NAME,
            "version": VERIFIER_VERSION,
        },
        "repository": pr["repository"],
        "pr": {
            "number": pr["number"],
            "state": pr["state"],
            "draft": pr["draft"],
            "updated_at": pr["updated_at"],
            "head_branch": pr["branch"],
            "head_sha": pr["head_sha"],
            "base_branch": pr["base_branch"],
            "base_sha": pr["base_commit"],
        },
        "pr_number": expected["pr_number"],
        "branch": expected["branch"],
        "candidate_commit": expected["candidate_commit"],
        "candidate_tree": expected["candidate_tree"],
        "raw_source": {
            "path": RAW_SNAPSHOT_NAME,
            "classification": "local-restricted",
            "sha256": raw_snapshot_sha256,
        },
        "marker": {
            "schema_version": MARKER_SCHEMA_VERSION,
            "sha256": _canonical_sha256(actual),
            "verification": "passed",
        },
        "marker_sha256": _canonical_sha256(actual),
        "candidate_manifest_sha256": expected["candidate_manifest_sha256"],
        "core_shareable_evidence_index": {
            "path": "SHAREABLE_EVIDENCE_INDEX.json",
            "sha256": expected["shareable_evidence_index_sha256"],
        },
        "shareable_evidence_index_sha256": expected["shareable_evidence_index_sha256"],
        "consumed_evidence_sha256": evidence_sha256,
        "issue_disposition": expected["issue_disposition"],
        "active_instance_touched": False,
        "result": "passed",
    }


def verify_snapshot_file(
    pr_snapshot_path: Path,
    evidence_dir: Path,
    *,
    expected_head_sha: str,
) -> dict[str, object]:
    if pr_snapshot_path.name != RAW_SNAPSHOT_NAME or pr_snapshot_path.is_symlink():
        raise PRCandidateMetadataError(
            f"raw GitHub API response must be saved as {RAW_SNAPSHOT_NAME}"
        )
    snapshot_path = pr_snapshot_path.resolve(strict=True)
    return verify_snapshot(
        _load_object(snapshot_path),
        evidence_dir,
        raw_snapshot_sha256=_sha256_file(snapshot_path),
        expected_head_sha=expected_head_sha,
    )


def public_review_binding_index(
    receipt: Mapping[str, object],
    *,
    receipt_sha256: str,
) -> dict[str, object]:
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise PRCandidateMetadataError("PR candidate receipt schema is invalid")
    if receipt.get("result") != "passed":
        raise PRCandidateMetadataError("PR candidate receipt is not passing")
    repository = _required_object(receipt, "repository")
    pr = _required_object(receipt, "pr")
    raw_source = _required_object(receipt, "raw_source")
    marker = _required_object(receipt, "marker")
    core_index = _required_object(receipt, "core_shareable_evidence_index")
    if repository != {
        "full_name": EXPECTED_REPOSITORY_FULL_NAME,
        "id": EXPECTED_REPOSITORY_ID,
    }:
        raise PRCandidateMetadataError("PR candidate receipt repository is invalid")
    return {
        "schema_version": PUBLIC_BINDING_SCHEMA_VERSION,
        "generated_at": _required_rfc3339(receipt, "verified_at"),
        "classification": "shareable",
        "repository": dict(repository),
        "pr": dict(pr),
        "candidate_commit": _validated_git_sha(
            receipt.get("candidate_commit"), field="candidate_commit"
        ),
        "candidate_tree": _validated_git_sha(
            receipt.get("candidate_tree"), field="candidate_tree"
        ),
        "core_shareable_evidence_index": {
            "path": _required_text(core_index, "path"),
            "sha256": _required_sha256(core_index, "sha256"),
        },
        "remote_pr_raw_source": {
            "path": _required_text(raw_source, "path"),
            "classification": _required_text(raw_source, "classification"),
            "sha256": _required_sha256(raw_source, "sha256"),
        },
        "marker_verification": {
            "schema_version": _required_text(marker, "schema_version"),
            "sha256": _required_sha256(marker, "sha256"),
            "result": _required_text(marker, "verification"),
        },
        "pr_candidate_metadata": {
            "path": RECEIPT_NAME,
            "sha256": _validated_sha256_value(
                receipt_sha256,
                field="receipt_sha256",
            ),
        },
        "active_instance_touched": False,
        "result": "passed",
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-snapshot", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-binding-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output.name != RECEIPT_NAME:
        raise PRCandidateMetadataError(f"receipt output must be named {RECEIPT_NAME}")
    if args.public_binding_output.name != PUBLIC_BINDING_NAME:
        raise PRCandidateMetadataError(
            f"public binding output must be named {PUBLIC_BINDING_NAME}"
        )
    if args.output.is_symlink() or args.public_binding_output.is_symlink():
        raise PRCandidateMetadataError("PR binding outputs must not be symlinks")
    receipt = verify_snapshot_file(
        args.pr_snapshot,
        args.evidence_dir,
        expected_head_sha=args.expected_head_sha,
    )
    _write_json(args.output, receipt)
    public_binding = public_review_binding_index(
        receipt,
        receipt_sha256=_sha256_file(args.output),
    )
    _write_json(args.public_binding_output, public_binding)
    print(
        json.dumps(
            {
                "result": "passed",
                "pr_number": receipt["pr_number"],
                "repository_id": EXPECTED_REPOSITORY_ID,
                "public_binding": args.public_binding_output.name,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
