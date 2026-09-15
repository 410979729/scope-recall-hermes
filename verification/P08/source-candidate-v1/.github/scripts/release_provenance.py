#!/usr/bin/env python3
"""Create and verify source/run-bound release provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

PROVENANCE_FORMAT = "scope-recall-release-provenance-v1"


def _distribution_hashes(packages_dir: Path) -> dict[str, str]:
    root = Path(packages_dir)
    packages = sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    )
    if len(packages) != 2:
        raise ValueError("provenance requires exactly one wheel and one sdist")
    if sum(path.name.endswith(".whl") for path in packages) != 1:
        raise ValueError("provenance requires exactly one wheel")
    if sum(path.name.endswith(".tar.gz") for path in packages) != 1:
        raise ValueError("provenance requires exactly one sdist")
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in packages
    }


def write_provenance(
    output: Path,
    *,
    repository: str,
    source_sha: str,
    source_tree: str,
    release_tag: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    packages_dir: Path,
) -> dict[str, Any]:
    """Write a deterministic receipt binding distributions to source and run."""

    payload: dict[str, Any] = {
        "format": PROVENANCE_FORMAT,
        "repository": repository,
        "release_tag": release_tag,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "workflow": {
            "path": ".github/workflows/release.yml",
            "run_id": str(workflow_run_id),
            "run_attempt": str(workflow_run_attempt),
        },
        "artifacts": _distribution_hashes(packages_dir),
    }
    Path(output).write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _require_equal(payload: dict[str, Any], key: str, expected: str) -> None:
    if payload.get(key) != expected:
        raise ValueError(f"provenance {key} mismatch")


def verify_provenance(
    provenance_path: Path,
    *,
    expected_repository: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_release_tag: str,
    expected_workflow_run_id: str,
    workflow_run_status: str,
    workflow_run_conclusion: str,
    packages_dir: Path,
) -> dict[str, Any]:
    """Verify receipt identity fields and the exact distribution bytes."""

    payload = json.loads(Path(provenance_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provenance root must be an object")
    _require_equal(payload, "format", PROVENANCE_FORMAT)
    _require_equal(payload, "repository", expected_repository)
    _require_equal(payload, "source_sha", expected_source_sha)
    _require_equal(payload, "source_tree", expected_source_tree)
    _require_equal(payload, "release_tag", expected_release_tag)
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("provenance workflow must be an object")
    if workflow.get("path") != ".github/workflows/release.yml":
        raise ValueError("provenance workflow path mismatch")
    if workflow.get("run_id") != str(expected_workflow_run_id):
        raise ValueError("provenance workflow run_id mismatch")
    if workflow_run_status != "completed":
        raise ValueError("originating workflow run must be completed")
    if workflow_run_conclusion != "success":
        raise ValueError("originating workflow run must be successful")
    artifacts = payload.get("artifacts")
    if artifacts != _distribution_hashes(packages_dir):
        raise ValueError("provenance artifact digest mismatch")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    verify = subparsers.add_parser("verify")
    for command in (create, verify):
        command.add_argument("--provenance", type=Path, required=True)
        command.add_argument("--repository", required=True)
        command.add_argument("--source-sha", required=True)
        command.add_argument("--source-tree", required=True)
        command.add_argument("--release-tag", required=True)
        command.add_argument("--workflow-run-id", required=True)
        command.add_argument("--packages-dir", type=Path, required=True)
    create.add_argument("--workflow-run-attempt", required=True)
    verify.add_argument("--workflow-run-status", required=True)
    verify.add_argument("--workflow-run-conclusion", required=True)
    args = parser.parse_args()
    common = {
        "repository": args.repository,
        "source_sha": args.source_sha,
        "source_tree": args.source_tree,
        "release_tag": args.release_tag,
        "packages_dir": args.packages_dir,
    }
    if args.command == "create":
        payload = write_provenance(
            args.provenance,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            **common,
        )
    else:
        payload = verify_provenance(
            args.provenance,
            expected_repository=common["repository"],
            expected_source_sha=common["source_sha"],
            expected_source_tree=common["source_tree"],
            expected_release_tag=common["release_tag"],
            expected_workflow_run_id=args.workflow_run_id,
            workflow_run_status=args.workflow_run_status,
            workflow_run_conclusion=args.workflow_run_conclusion,
            packages_dir=args.packages_dir,
        )
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
