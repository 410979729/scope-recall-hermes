"""Security contracts for the GitHub Actions release supply chain.

Release credentials must never coexist with repository checkout, dependency
installation, or execution of remotely fetched Hermes source.  All reusable
actions and the Hermes compatibility source are pinned to reviewed commits.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

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


def test_github_release_publish_is_retry_safe_without_source_execution():
    publish_text = _run_text(_workflow("release.yml")["jobs"]["publish"])
    assert "gh release view" in publish_text
    assert "gh release edit" in publish_text
    assert "gh release upload" in publish_text
    assert "--clobber" in publish_text
    assert "gh release create" in publish_text


def test_release_credentials_are_isolated_from_build_and_source_execution():
    expectations = {
        "release.yml": {"contents": "write", "id-token": "write"},
        "pypi.yml": {"contents": "read", "id-token": "write"},
    }
    forbidden_publish_commands = re.compile(
        r"(?:\bpython\b|\bpip\b|\bgit\b|pytest|check\.release|\.hermes-agent-src)",
        re.IGNORECASE,
    )

    for workflow_name, expected_publish_permissions in expectations.items():
        workflow = _workflow(workflow_name)
        build_job = workflow["jobs"]["build"]
        publish_job = workflow["jobs"]["publish"]

        assert build_job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        assert publish_job["needs"] == "build"
        assert publish_job["permissions"] == expected_publish_permissions

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
