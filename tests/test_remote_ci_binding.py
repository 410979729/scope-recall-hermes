"""Exact GitHub Actions run/job binding receipt contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify.remote_ci_binding.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("remote_ci_binding_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    sha = "a" * 40
    run = {
        "id": 12345,
        "run_attempt": 2,
        "event": "pull_request",
        "head_sha": sha,
        "head_branch": "scope-recall/2.0.0-rc-audit-remediation",
        "workflow_id": 77,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "410979729/scope-recall-hermes"},
    }
    jobs = {
        "total_count": 2,
        "jobs": [
            {
                "id": 1,
                "name": "windows",
                "status": "completed",
                "conclusion": "success",
                "run_id": 12345,
                "run_attempt": 2,
                "head_sha": sha,
            },
            {
                "id": 2,
                "name": "ci-required",
                "status": "completed",
                "conclusion": "success",
                "run_id": 12345,
                "run_attempt": 2,
                "head_sha": sha,
            },
        ],
    }
    run_path = tmp_path / "run.json"
    jobs_path = tmp_path / "jobs.json"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    return run_path, jobs_path, sha


def _build(module, run_path: Path, jobs_path: Path, sha: str):
    return module.build_remote_ci_binding(
        run_json=run_path,
        jobs_json=jobs_path,
        expected_repository="410979729/scope-recall-hermes",
        expected_sha=sha,
        expected_workflow_id=77,
        expected_workflow_path=".github/workflows/ci.yml",
        required_job="ci-required",
    )


def test_remote_ci_receipt_binds_repository_workflow_sha_attempt_and_job(
    tmp_path: Path,
) -> None:
    module = _load_module()
    run_path, jobs_path, sha = _fixture(tmp_path)

    payload = _build(module, run_path, jobs_path, sha)

    assert payload["head_sha"] == sha
    assert payload["run_id"] == "12345"
    assert payload["run_attempt"] == 2
    assert payload["required_job_conclusion"] == "success"
    assert payload["authentication_material_recorded"] is False
    assert str(tmp_path) not in json.dumps(payload)


@pytest.mark.parametrize("mutation", ["sha", "workflow", "job"])
def test_remote_ci_receipt_fails_closed_on_binding_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    module = _load_module()
    run_path, jobs_path, sha = _fixture(tmp_path)
    if mutation == "sha":
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["head_sha"] = "b" * 40
        run_path.write_text(json.dumps(run), encoding="utf-8")
    elif mutation == "workflow":
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["workflow_id"] = 78
        run_path.write_text(json.dumps(run), encoding="utf-8")
    else:
        jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs["jobs"][1]["conclusion"] = "failure"
        jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    with pytest.raises(module.RemoteCIBindingError):
        _build(module, run_path, jobs_path, sha)
