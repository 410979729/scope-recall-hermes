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


MARKER_SCHEMA_VERSION = "scope-recall.pr-candidate-metadata.v1"
RECEIPT_SCHEMA_VERSION = "scope-recall.pr-candidate-metadata-check.v1"
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
        "sha256": _required_text(payload, "sha256"),
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
        "sha256": _required_text(matches[0], "sha256"),
    }


def _pr_identity(snapshot: Mapping[str, object]) -> dict[str, object]:
    head = _required_object(snapshot, "head")
    base = _required_object(snapshot, "base")
    number = snapshot.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PRCandidateMetadataError("PR number is invalid")
    if snapshot.get("state") != "open" or snapshot.get("draft") is not True:
        raise PRCandidateMetadataError("release-candidate PR must remain open and Draft")
    if _required_text(base, "ref") != "main":
        raise PRCandidateMetadataError("release-candidate PR base must remain main")
    return {
        "number": number,
        "branch": _required_text(head, "ref"),
        "head_sha": _required_text(head, "sha"),
        "base_commit": _required_text(base, "sha"),
    }


def expected_marker(
    pr_snapshot: Mapping[str, object],
    evidence_dir: Path,
) -> dict[str, object]:
    """Build the only acceptable public metadata marker for this PR snapshot."""

    root = evidence_dir.resolve(strict=True)
    candidate_path = root / "CANDIDATE_MANIFEST.json"
    provenance_path = root / "BUILD_PROVENANCE.json"
    remote_path = root / "REMOTE_CI_BINDING.json"
    honesty_path = root / "PYTEST_SKIP_REPORT.json"
    shareable_path = root / "SHAREABLE_EVIDENCE_INDEX.json"
    active_isolation_path = root / "ACTIVE_ISOLATION.json"
    hermes_supported_path = root / "HERMES_COMPATIBILITY_PROBE.0.19.1.json"
    hermes_probe_path = root / "HERMES_COMPATIBILITY_PROBE.0.20.6.json"
    candidate = _load_object(candidate_path)
    provenance = _load_object(provenance_path)
    remote = _load_object(remote_path)
    honesty = _load_object(honesty_path)
    shareable = _load_object(shareable_path)
    active_isolation = _load_object(active_isolation_path)
    hermes_supported = _load_object(hermes_supported_path)
    hermes_probe = _load_object(hermes_probe_path)
    source = _required_object(candidate, "source")
    pr = _pr_identity(pr_snapshot)
    candidate_commit = _required_text(source, "commit")
    candidate_tree = _required_text(source, "tree")
    if _required_text(candidate, "candidate_version") != "2.0.0":
        raise PRCandidateMetadataError("Candidate Manifest version is not 2.0.0")
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
    if provenance.get("source_manifest_sha256") != source_manifest.get(
        "manifest_sha256"
    ):
        raise PRCandidateMetadataError("source manifest differs from build provenance")
    provenance_artifacts = {
        kind: _artifact(provenance, kind) for kind in ("wheel", "sdist")
    }
    if any(
        _candidate_artifact(candidate, kind) != provenance_artifacts[kind]
        for kind in ("wheel", "sdist")
    ):
        raise PRCandidateMetadataError("Candidate Manifest artifacts differ from provenance")
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
        or active_isolation.get("active_instance_touched") is not False
    ):
        raise PRCandidateMetadataError("Hermes support or active isolation boundary changed")
    if (
        hermes_supported.get("expected_hermes_version") != "0.19.1"
        or hermes_probe.get("expected_hermes_version") != "0.20.6"
        or hermes_probe.get("result") not in {"compatible", "incompatible"}
    ):
        raise PRCandidateMetadataError("Hermes 0.20.6 probe result is invalid")
    supported_source = _required_object(hermes_supported, "hermes_source")
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
        or any(
            shareable.get(field) != 0
            for field in (
                "private_path_match_count",
                "secret_match_count",
                "missing_indexed_file_count",
                "unexpected_shareable_file_count",
            )
        )
    ):
        raise PRCandidateMetadataError("shareable evidence closure is not clean")
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "pr_number": pr["number"],
        "branch": pr["branch"],
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
        "source_manifest_sha256": _required_text(
            provenance, "source_manifest_sha256"
        ),
        "candidate_manifest_sha256": _sha256_file(candidate_path),
        "shareable_evidence_index_sha256": _sha256_file(shareable_path),
        "local_tests": local_tests,
        "hermes_support": {
            "supported_version": _required_text(
                hermes_supported, "expected_hermes_version"
            ),
            "supported_commit": _required_text(supported_source, "commit"),
            "supported_result": _required_text(hermes_supported, "result"),
            "probe_version": _required_text(hermes_probe, "expected_hermes_version"),
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
    return (
        "# Scope Recall 2.0 release candidate\n\n"
        "This pull request remains Draft pending an independent re-audit. "
        "It does not authorize merge, deploy, tag, or release.\n\n"
        "## Verified candidate\n\n"
        f"- Exact commit: `{marker['candidate_commit']}`\n"
        f"- Exact tree: `{marker['candidate_tree']}`\n"
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
        f"{MARKER_START}\n```json\n"
        + json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True)
        + f"\n```\n{MARKER_END}\n"
    )


def verify_snapshot(
    pr_snapshot: Mapping[str, object],
    evidence_dir: Path,
) -> dict[str, object]:
    expected = expected_marker(pr_snapshot, evidence_dir)
    actual = parse_marker(_required_text(pr_snapshot, "body"))
    if actual != expected:
        differing = sorted(
            key for key in set(actual) | set(expected) if actual.get(key) != expected.get(key)
        )
        raise PRCandidateMetadataError(
            "PR candidate marker differs from exact evidence: " + ", ".join(differing)
        )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "pr_number": expected["pr_number"],
        "branch": expected["branch"],
        "candidate_commit": expected["candidate_commit"],
        "candidate_tree": expected["candidate_tree"],
        "marker_sha256": _canonical_sha256(actual),
        "candidate_manifest_sha256": expected["candidate_manifest_sha256"],
        "shareable_evidence_index_sha256": expected[
            "shareable_evidence_index_sha256"
        ],
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
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    receipt = verify_snapshot(
        _load_object(args.pr_snapshot),
        args.evidence_dir,
    )
    _write_json(args.output, receipt)
    print(json.dumps({"result": "passed", "pr_number": receipt["pr_number"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
