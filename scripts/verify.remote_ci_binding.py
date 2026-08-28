#!/usr/bin/env python3
"""Verify saved GitHub Actions API responses against one exact candidate SHA."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Sequence


SCHEMA_VERSION = "scope-recall.remote-ci-binding.v1"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class RemoteCIBindingError(RuntimeError):
    """Raised when GitHub API evidence is missing, ambiguous, or mismatched."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> tuple[Path, dict[str, object]]:
    resolved = path.resolve(strict=True)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteCIBindingError(
            f"invalid GitHub API response: {resolved.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise RemoteCIBindingError("GitHub API response root must be an object")
    return resolved, payload


def build_remote_ci_binding(
    *,
    run_json: Path,
    jobs_json: Path,
    expected_repository: str,
    expected_sha: str,
    expected_workflow_id: int,
    expected_workflow_path: str,
    required_job: str,
) -> dict[str, object]:
    run_path, run = _load_object(run_json)
    jobs_path, jobs_payload = _load_object(jobs_json)
    if not FULL_SHA.fullmatch(expected_sha):
        raise RemoteCIBindingError("expected SHA must be a full lowercase Git SHA")
    repository = run.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != expected_repository:
        raise RemoteCIBindingError("workflow run repository mismatch")
    if run.get("head_sha") != expected_sha:
        raise RemoteCIBindingError("workflow run head SHA mismatch")
    if run.get("workflow_id") != expected_workflow_id:
        raise RemoteCIBindingError("workflow run workflow ID mismatch")
    if run.get("path") != expected_workflow_path:
        raise RemoteCIBindingError("workflow run workflow path mismatch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RemoteCIBindingError("workflow run is not completed successfully")
    run_id = run.get("id")
    run_attempt = run.get("run_attempt")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise RemoteCIBindingError("workflow run ID is invalid")
    if (
        isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt <= 0
    ):
        raise RemoteCIBindingError("workflow run attempt is invalid")
    raw_jobs = jobs_payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise RemoteCIBindingError("workflow jobs response is missing jobs")
    jobs = [item for item in raw_jobs if isinstance(item, dict)]
    if len(jobs) != len(raw_jobs) or jobs_payload.get("total_count") != len(jobs):
        raise RemoteCIBindingError("workflow jobs response count mismatch")
    required = [job for job in jobs if job.get("name") == required_job]
    if len(required) != 1:
        raise RemoteCIBindingError("required aggregate job is missing or ambiguous")
    required_payload = required[0]
    if (
        required_payload.get("status") != "completed"
        or required_payload.get("conclusion") != "success"
    ):
        raise RemoteCIBindingError("required aggregate job did not succeed")
    for job in jobs:
        if job.get("run_id") not in {None, run_id}:
            raise RemoteCIBindingError("workflow job belongs to another run")
        if job.get("run_attempt") not in {None, run_attempt}:
            raise RemoteCIBindingError("workflow job belongs to another attempt")
        if job.get("head_sha") not in {None, expected_sha}:
            raise RemoteCIBindingError("workflow job head SHA mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "repository": expected_repository,
        "run_id": str(run_id),
        "run_attempt": run_attempt,
        "event": str(run.get("event") or ""),
        "head_sha": expected_sha,
        "head_branch": str(run.get("head_branch") or ""),
        "workflow_id": expected_workflow_id,
        "workflow_name": str(run.get("name") or ""),
        "workflow_path": expected_workflow_path,
        "run_status": "completed",
        "run_conclusion": "success",
        "required_job": required_job,
        "required_job_id": str(required_payload.get("id") or ""),
        "required_job_conclusion": "success",
        "job_count": len(jobs),
        "run_response_sha256": _sha256(run_path),
        "jobs_response_sha256": _sha256(jobs_path),
        "authentication_material_recorded": False,
        "result": "passed",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--jobs-json", type=Path, required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-workflow-id", type=int, required=True)
    parser.add_argument("--expected-workflow-path", required=True)
    parser.add_argument("--required-job", default="ci-required")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_remote_ci_binding(
        run_json=args.run_json,
        jobs_json=args.jobs_json,
        expected_repository=str(args.expected_repository),
        expected_sha=str(args.expected_sha),
        expected_workflow_id=int(args.expected_workflow_id),
        expected_workflow_path=str(args.expected_workflow_path),
        required_job=str(args.required_job),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"ok": True, "run_id": payload["run_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RemoteCIBindingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1) from exc
