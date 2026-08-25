"""P1-13: one GitHub Release yields one PyPI path with identical artifacts."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"
PYPI_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "pypi.yml"
RELEASE_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "release.yml"


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_check_release_publish_workflows",
        CHECK_RELEASE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow_on(payload: dict) -> dict:
    """Return workflow triggers. PyYAML 1.1 treats the ``on`` key as boolean True."""

    triggers = payload.get("on")
    if triggers is None:
        triggers = payload.get(True)
    assert isinstance(triggers, dict)
    return triggers


def test_one_github_release_has_exactly_one_pypi_publish_path():
    pypi = yaml.safe_load(PYPI_WORKFLOW.read_text(encoding="utf-8"))
    pypi_text = PYPI_WORKFLOW.read_text(encoding="utf-8")
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    triggers = _workflow_on(pypi)
    assert "release" not in triggers, (
        "PyPI must not also listen for GitHub Release published events"
    )
    assert "repository_dispatch" in triggers
    assert triggers["repository_dispatch"]["types"] == ["scope-recall-pypi-publish"]
    assert "workflow_dispatch" in triggers
    assert "pypa/gh-action-pypi-publish" in pypi_text
    assert pypi_text.count("pypa/gh-action-pypi-publish") == 1
    assert "pypa/gh-action-pypi-publish" not in release_text
    assert "id-token: write" not in release_text
    assert "Trigger PyPI publish workflow" in release_text
    assert release_text.count("scope-recall-pypi-publish") == 1
    assert "repos/${GITHUB_REPOSITORY}/dispatches" in release_text


def test_pypi_reuses_github_release_artifacts_and_verifies_sha256():
    pypi_text = PYPI_WORKFLOW.read_text(encoding="utf-8")
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "python -m build" in release_text
    assert "python -m build" not in pypi_text
    assert "gh release download" in pypi_text
    assert "SHA256SUMS" in pypi_text
    assert "sha256" in pypi_text.lower()
    assert "Verify GitHub Release artifact SHA-256" in pypi_text
    assert "Download GitHub Release artifacts" in pypi_text
    assert "skip-existing" not in pypi_text


def test_pypi_workflow_is_per_tag_concurrent_and_refuses_repeat_upload():
    pypi = yaml.safe_load(PYPI_WORKFLOW.read_text(encoding="utf-8"))
    pypi_text = PYPI_WORKFLOW.read_text(encoding="utf-8")

    concurrency = pypi.get("concurrency")
    assert concurrency is not None
    group = str(concurrency.get("group") or "")
    assert "pypi-publish-" in group
    assert "client_payload.tag" in group or "inputs.release_tag" in group
    assert concurrency.get("cancel-in-progress") is False
    assert "Refuse repeated PyPI publish" in pypi_text
    assert "pypi.org/pypi/hermes-scope-recall" in pypi_text
    assert re.search(r"exit 1", pypi_text) is not None
    assert pypi_text.index("Refuse repeated PyPI publish") < pypi_text.index(
        "pypa/gh-action-pypi-publish"
    )
    assert pypi_text.index("scripts/check.release.py") < pypi_text.index(
        "pypa/gh-action-pypi-publish"
    )


def test_release_workflow_is_per_tag_concurrent():
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    concurrency = release.get("concurrency")
    assert concurrency is not None
    group = str(concurrency.get("group") or "")
    assert "github-release-" in group
    assert "inputs.release_tag" in group
    assert "github.ref_name" in group
    assert concurrency.get("cancel-in-progress") is False


def test_release_workflow_refuses_existing_release_without_clobber_or_dispatch():
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    refuse_at = release_text.index("Refuse existing GitHub Release")
    create_at = release_text.index("gh release create")
    dispatch_at = release_text.index("Trigger PyPI publish workflow")
    refuse_block = release_text[refuse_at:create_at]

    assert "Refusing to mutate existing GitHub Release" in refuse_block
    assert "gh release view" in refuse_block
    assert "exit 1" in refuse_block
    assert "gh release edit" not in release_text
    assert "gh release upload" not in release_text
    assert "--clobber" not in release_text
    assert "Create or update GitHub Release" not in release_text
    assert "dispatches" not in refuse_block
    assert release_text.count("scope-recall-pypi-publish") == 1
    assert release_text.count("repos/${GITHUB_REPOSITORY}/dispatches") == 1
    assert refuse_at < create_at < dispatch_at
    assert "continue-on-error: true" not in release_text


def test_release_gate_encodes_single_pypi_artifact_path():
    release_check = _load_release_check_module()
    gate = release_check.pypi_workflow_gate_check()
    assert gate["ok"] is True, gate.get("failures")
