"""Security contracts for the GitHub Actions release supply chain.

Release credentials must never coexist with repository checkout, dependency
installation, or execution of remotely fetched Hermes source.  All reusable
actions and the Hermes compatibility source are pinned to reviewed commits.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HERMES_AGENT_COMMIT = "cc4cab2f592e60a197e796506de9168f74baf3ea"
ACTION_PINS = {
    "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _steps(workflow: dict):
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") for step in job.get("steps", []))


def _checkout_helper():
    path = ROOT / ".github" / "scripts" / "checkout_pinned_hermes.py"
    spec = importlib.util.spec_from_file_location("checkout_pinned_hermes_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_reusable_action_is_pinned_to_the_reviewed_commit():
    for workflow_name in ("ci.yml", "release.yml", "pypi.yml"):
        workflow = _workflow(workflow_name)
        for job_name, step in _steps(workflow):
            uses = str(step.get("uses") or "")
            if not uses:
                continue
            action, separator, commit = uses.partition("@")
            assert separator and FULL_SHA_RE.fullmatch(commit), (
                workflow_name,
                job_name,
                uses,
            )
            assert ACTION_PINS[action] == commit


def test_ci_and_release_builds_pin_and_verify_stable_hermes_source():
    checkout_script = (
        ROOT / ".github" / "scripts" / "checkout_pinned_hermes.py"
    ).read_text(encoding="utf-8")
    assert 'os.environ.get("HERMES_AGENT_COMMIT")' in checkout_script
    assert '"fetch"' in checkout_script
    assert '"rev-parse", "HEAD"' in checkout_script
    assert "actual != commit" in checkout_script

    for workflow_name in ("ci.yml", "release.yml", "pypi.yml"):
        workflow = _workflow(workflow_name)
        assert workflow.get("permissions") == {"contents": "read"}
        assert workflow.get("env", {}).get("HERMES_AGENT_COMMIT") == HERMES_AGENT_COMMIT

        all_run_text = "\n".join(
            str(step.get("run") or "") for _, step in _steps(workflow)
        )
        assert ".github/scripts/checkout_pinned_hermes.py" in all_run_text
        assert not re.search(r"(?:--branch|checkout|fetch)[^\n]*\bmain\b", all_run_text)

    ci_workflow = _workflow("ci.yml")
    full_matrix_checkout = [
        step
        for step in ci_workflow["jobs"]["test"]["steps"]
        if str(step.get("uses") or "").startswith("actions/checkout@")
    ]
    assert len(full_matrix_checkout) == 1
    assert full_matrix_checkout[0].get("with", {}).get("fetch-depth") == 0


def test_checkout_helper_timeout_cleans_process_tree_and_reports_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _checkout_helper()
    cleaned: list[int] = []

    class HangingProcess:
        pid = 12345
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["git", "fetch"], timeout)
            self.returncode = -9
            return "", "stopped"

    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: HangingProcess())
    monkeypatch.setattr(
        helper,
        "_terminate_process_tree",
        lambda process: cleaned.append(process.pid),
    )

    with pytest.raises(helper.CheckoutCommandError) as caught:
        helper._run("git", "fetch", timeout_seconds=7)

    assert cleaned == [12345]
    assert caught.value.as_dict() == {
        "command": ["git", "fetch"],
        "error": "command_timeout",
        "timeout_seconds": 7,
    }


@pytest.mark.parametrize("taskkill_returncode", [0, 1])
def test_checkout_helper_windows_tree_cleanup_failure_stays_bounded_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    taskkill_returncode: int,
) -> None:
    helper = _checkout_helper()
    communicate_timeouts: list[float | int | None] = []

    class HangingProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return None

        def communicate(self, timeout=None):
            communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(
                ["git", "fetch"],
                timeout,
                output="PRIVATE_TIMEOUT_OUTPUT",
                stderr="PRIVATE_TIMEOUT_ERROR",
            )

        def kill(self):
            return None

    def taskkill(*args, **kwargs):
        assert kwargs["timeout"] == helper.TERMINATION_GRACE_SECONDS
        return subprocess.CompletedProcess(args[0], taskkill_returncode)

    monkeypatch.setattr(helper.os, "name", "nt")
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: HangingProcess())
    monkeypatch.setattr(helper.subprocess, "run", taskkill)

    started = time.monotonic()
    with pytest.raises(helper.CheckoutCommandError) as caught:
        helper._run("git", "fetch", timeout_seconds=0.01)
    elapsed = time.monotonic() - started

    payload = caught.value.as_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert elapsed < 0.5
    assert communicate_timeouts == [
        0.01,
        helper.TERMINATION_GRACE_SECONDS,
        helper.TERMINATION_GRACE_SECONDS,
    ]
    assert payload == {
        "command": ["git", "fetch"],
        "error": "process_tree_termination_failed",
        "timeout_seconds": 0.01,
    }
    assert "PRIVATE_TIMEOUT_OUTPUT" not in serialized
    assert "PRIVATE_TIMEOUT_ERROR" not in serialized


def test_release_jobs_have_hard_timeouts() -> None:
    expected = {
        "release.yml": {"verify_release_policy": 10, "build": 45, "publish": 10},
        "pypi.yml": {"verify_release_origin": 10, "prepare": 45, "publish": 10},
    }

    for workflow_name, job_timeouts in expected.items():
        workflow = _workflow(workflow_name)
        assert {
            job_name: job.get("timeout-minutes")
            for job_name, job in workflow["jobs"].items()
        } == job_timeouts


def test_explicit_pytest_nodes_in_workflows_exist():
    def strings(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
        elif isinstance(value, str):
            yield value

    missing_paths: dict[str, list[str]] = {}
    missing_nodes: dict[str, list[str]] = {}
    node_pattern = re.compile(
        r"(?P<path>tests/[A-Za-z0-9_./-]+\.py)::(?P<name>test_[A-Za-z0-9_]+)"
    )
    for workflow_name in ("ci.yml", "release.yml", "pypi.yml"):
        workflow = _workflow(workflow_name)
        workflow_text = "\n".join(strings(workflow))
        paths = set(re.findall(r"tests/[A-Za-z0-9_./-]+\.py", workflow_text))
        absent_paths = sorted(path for path in paths if not (ROOT / path).is_file())
        if absent_paths:
            missing_paths[workflow_name] = absent_paths
        absent_nodes: list[str] = []
        for match in node_pattern.finditer(workflow_text):
            relative_path = match.group("path")
            test_path = ROOT / relative_path
            if not test_path.is_file():
                continue
            tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=relative_path)
            test_names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            }
            if match.group("name") not in test_names:
                absent_nodes.append(f"{relative_path}::{match.group('name')}")
        if absent_nodes:
            missing_nodes[workflow_name] = sorted(set(absent_nodes))
    assert missing_paths == {}
    assert missing_nodes == {}


def test_ci_jobs_that_execute_plugin_tests_use_pinned_hermes_checkout():
    workflow = _workflow("ci.yml")
    for job_name, job in workflow["jobs"].items():
        serialized = yaml.safe_dump(job)
        if "pytest" not in serialized and "matrix.command" not in serialized:
            continue
        assert ".github/scripts/checkout_pinned_hermes.py" in _run_text(job), job_name


def test_ci_required_is_stable_and_fails_closed_over_every_required_job():
    workflow = _workflow("ci.yml")
    job = workflow["jobs"]["ci-required"]
    required = {
        "test",
        "macos-lancedb-smoke",
        "windows-no-symlink",
        "windows-installer",
        "release-gate",
    }

    assert job["name"] == "ci-required"
    assert job["if"] in {"always()", "${{ always() }}"}
    assert job["permissions"] == {}
    assert set(job["needs"]) == required
    assert job["runs-on"] == "ubuntu-latest"
    assert job.get("continue-on-error") is None
    run_text = _run_text(job)
    for dependency in required:
        assert f"needs.{dependency}.result" in run_text
    assert run_text.count('= "success"') == len(required)

    release_text = (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")
    assert "ci-required" in release_text
    assert "windows-full-py311 \\" not in release_text
    assert "windows-full-py312 \\" not in release_text
    assert "windows-no-symlink-py311\n" not in release_text


def test_github_release_publish_is_immutable_without_source_execution():
    publish_text = _run_text(_workflow("release.yml")["jobs"]["publish"])
    assert "gh release view" in publish_text
    assert "gh release create" in publish_text
    assert "gh release edit" not in publish_text
    assert "gh release upload" not in publish_text
    assert "--clobber" not in publish_text
    assert "Refusing to mutate existing GitHub Release" in publish_text
    assert publish_text.index("gh release view") < publish_text.index("gh release create")
    assert publish_text.count("gh release ") == 2
    assert publish_text.count('--repo "${GITHUB_REPOSITORY}"') == 2


def test_release_credentials_are_isolated_from_build_and_source_execution():
    expectations = {
        "release.yml": {"contents": "write"},
        "pypi.yml": {"contents": "read", "id-token": "write"},
    }
    forbidden_publish_commands = re.compile(
        r"(?:\bpython\b|\bpip\b|\bgit\b|pytest|check\.release|\.hermes-agent-src)",
        re.IGNORECASE,
    )

    for workflow_name, expected_publish_permissions in expectations.items():
        workflow = _workflow(workflow_name)
        source_job_name = "prepare" if workflow_name == "pypi.yml" else "build"
        build_job = workflow["jobs"][source_job_name]
        publish_job = workflow["jobs"]["publish"]

        assert build_job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        assert publish_job["needs"] == source_job_name
        assert publish_job["permissions"] == expected_publish_permissions
        expected_environment = "pypi" if workflow_name == "pypi.yml" else None
        assert publish_job.get("environment") == expected_environment

        publish_actions = {
            str(step.get("uses") or "").partition("@")[0]
            for step in publish_job.get("steps", [])
            if step.get("uses")
        }
        assert "actions/download-artifact" in publish_actions
        assert "actions/checkout" not in publish_actions
        assert "actions/setup-python" not in publish_actions
        assert not forbidden_publish_commands.search(_run_text(publish_job))

        build_permissions = build_job.get("permissions", workflow.get("permissions", {}))
        assert build_permissions.get("id-token") != "write"
        assert build_permissions.get("contents") != "write"
