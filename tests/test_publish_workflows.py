"""P1-13: one GitHub Release yields one PyPI path with identical artifacts."""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"
PYPI_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "pypi.yml"
RELEASE_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_CONSTRAINTS = PLUGIN_ROOT / "constraints" / "release.txt"
MANIFEST = PLUGIN_ROOT / "MANIFEST.in"


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


def test_pypi_runs_clean_tree_gate_before_artifact_work_directories():
    pypi = yaml.safe_load(PYPI_WORKFLOW.read_text(encoding="utf-8"))
    prepare_steps = pypi["jobs"]["prepare"]["steps"]
    steps = [step.get("name") for step in prepare_steps]

    assert steps.index("Verify tag matches package version") < steps.index(
        "Verify checked-out tag matches release source"
    )
    assert steps.index("Verify checked-out tag matches release source") < steps.index(
        "Install release dependencies"
    )
    assert steps.index("Install release dependencies") < steps.index(
        "Run tagged release gate"
    )
    assert steps.index("Run tagged release gate") < steps.index(
        "Download GitHub Release artifacts"
    )
    assert steps.index("Download GitHub Release artifacts") < steps.index(
        "Verify GitHub Release artifact SHA-256"
    )
    assert steps.index("Verify GitHub Release artifact SHA-256") < steps.index(
        "Verify source/run-bound release provenance"
    )
    assert steps.index("Verify source/run-bound release provenance") < steps.index(
        "Stage validated distributions"
    )
    gate_position = steps.index("Run tagged release gate")
    before_gate = "\n".join(str(step) for step in prepare_steps[:gate_position])
    assert "release-download" not in before_gate
    assert "release-staging" not in before_gate
    source_step = next(
        step
        for step in prepare_steps
        if step.get("name") == "Verify checked-out tag matches release source"
    )
    assert source_step["env"] == {
        "VERIFIED_SOURCE_SHA": "${{ needs.verify_release_origin.outputs.source_sha }}"
    }
    assert source_step["run"] == (
        'test "$(git rev-parse HEAD)" = "${VERIFIED_SOURCE_SHA}"'
    )


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


def test_ci_and_publish_workflows_force_utf8_subprocess_environment():
    for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW, PYPI_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        assert 'PYTHONUTF8: "1"' in text, workflow.name
        assert 'PYTHONIOENCODING: "utf-8"' in text, workflow.name


def test_release_provenance_is_bound_to_source_and_originating_workflow_run():
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    pypi_text = PYPI_WORKFLOW.read_text(encoding="utf-8")

    assert ".github/scripts/release_provenance.py create" in release_text
    assert "RELEASE-PROVENANCE.json" in release_text
    assert 'client_payload[source_sha]' in release_text
    assert 'client_payload[release_run_id]' in release_text
    assert ".github/scripts/release_provenance.py verify" in pypi_text
    assert "actions/runs/${RELEASE_RUN_ID}" in pypi_text
    assert "jq -er '.path'" in pypi_text
    assert "jq -er '.head_sha'" in pypi_text
    assert 'test "${DISPATCH_RUN_ID}" = "${RELEASE_RUN_ID}"' in pypi_text


def test_pypi_passes_one_verified_successful_run_snapshot_to_prepare():
    pypi = yaml.safe_load(PYPI_WORKFLOW.read_text(encoding="utf-8"))
    verify_job = pypi["jobs"]["verify_release_origin"]
    prepare_job = pypi["jobs"]["prepare"]

    assert verify_job["outputs"] == {
        "source_sha": "${{ steps.release_origin.outputs.source_sha }}",
        "release_run_id": "${{ steps.release_origin.outputs.release_run_id }}",
        "workflow_run_status": (
            "${{ steps.release_origin.outputs.workflow_run_status }}"
        ),
        "workflow_run_conclusion": (
            "${{ steps.release_origin.outputs.workflow_run_conclusion }}"
        ),
    }
    origin_step = next(
        step
        for step in verify_job["steps"]
        if step.get("name") == "Verify originating release workflow run"
    )
    assert origin_step["id"] == "release_origin"
    origin_run = origin_step["run"]
    assert origin_run.count(
        'gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${RELEASE_RUN_ID}"'
    ) == 1
    assert 'test "${WORKFLOW_RUN_STATUS}" = "completed"' in origin_run
    assert 'test "${WORKFLOW_RUN_CONCLUSION}" = "success"' in origin_run
    assert '>> "${GITHUB_OUTPUT}"' in origin_run

    assert prepare_job["needs"] == "verify_release_origin"
    assert prepare_job["permissions"] == {"contents": "read"}
    assert all(
        "gh api" not in str(step.get("run") or "") for step in prepare_job["steps"]
    )
    provenance_step = next(
        step
        for step in prepare_job["steps"]
        if step.get("name") == "Verify source/run-bound release provenance"
    )
    assert provenance_step["env"] == {
        "RELEASE_TAG": "${{ steps.release_tag.outputs.release_tag }}",
        "VERIFIED_SOURCE_SHA": "${{ needs.verify_release_origin.outputs.source_sha }}",
        "VERIFIED_RELEASE_RUN_ID": (
            "${{ needs.verify_release_origin.outputs.release_run_id }}"
        ),
        "WORKFLOW_RUN_STATUS": (
            "${{ needs.verify_release_origin.outputs.workflow_run_status }}"
        ),
        "WORKFLOW_RUN_CONCLUSION": (
            "${{ needs.verify_release_origin.outputs.workflow_run_conclusion }}"
        ),
    }
    provenance_run = provenance_step["run"]
    assert "--workflow-run-status \"${WORKFLOW_RUN_STATUS}\"" in provenance_run
    assert "--workflow-run-conclusion \"${WORKFLOW_RUN_CONCLUSION}\"" in provenance_run


def test_release_toolchain_uses_bounded_constraints_shipped_with_source():
    constraints = RELEASE_CONSTRAINTS.read_text(encoding="utf-8").splitlines()
    required = ("build", "setuptools", "wheel", "twine")
    for dependency in required:
        line = next(item for item in constraints if item.startswith(dependency))
        assert ">=" in line and "<" in line
    for workflow in (RELEASE_WORKFLOW, PYPI_WORKFLOW):
        assert "PIP_CONSTRAINT: constraints/release.txt" in workflow.read_text(
            encoding="utf-8"
        )
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "include constraints/release.txt" in manifest
    assert "recursive-include .github/scripts *.py" in manifest


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


def test_pypi_publish_uses_validated_distribution_only_directory():
    pypi_text = PYPI_WORKFLOW.read_text(encoding="utf-8")

    assert "stage_release_assets.py" in pypi_text
    assert "--packages-dir release-staging/packages" in pypi_text
    assert "--metadata-dir release-staging/metadata" in pypi_text
    assert "python -m twine check release-staging/packages/*" in pypi_text
    assert "release-staging/packages/*.whl" in pypi_text
    assert "release-staging/packages/*.tar.gz" in pypi_text
    assert "release-staging/metadata/SHA256SUMS" in pypi_text
    assert "packages-dir: release-assets/packages/" in pypi_text


def test_release_gate_rejects_pypi_packages_dir_that_includes_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy2(RELEASE_WORKFLOW, workflows / "release.yml")
    bad = PYPI_WORKFLOW.read_text(encoding="utf-8").replace(
        "packages-dir: release-assets/packages/",
        "packages-dir: release-assets/",
    )
    (workflows / "pypi.yml").write_text(bad, encoding="utf-8")
    release_check = _load_release_check_module()
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    gate = release_check.pypi_workflow_gate_check()

    assert gate["ok"] is False
    assert any("distribution-only" in failure for failure in gate["failures"])


def test_release_gate_rejects_artifact_work_directory_before_clean_tree_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy2(RELEASE_WORKFLOW, workflows / "release.yml")
    bad = PYPI_WORKFLOW.read_text(encoding="utf-8").replace(
        "      - name: Install release dependencies\n",
        "      - name: Create release workspace too early\n"
        "        run: mkdir -p release-download\n"
        "      - name: Install release dependencies\n",
        1,
    )
    (workflows / "pypi.yml").write_text(bad, encoding="utf-8")
    release_check = _load_release_check_module()
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    gate = release_check.pypi_workflow_gate_check()

    assert gate["ok"] is False
    assert any(
        "before creating release artifact work directories" in failure
        for failure in gate["failures"]
    )


def test_release_gate_rejects_missing_pre_execution_source_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy2(RELEASE_WORKFLOW, workflows / "release.yml")
    bad = PYPI_WORKFLOW.read_text(encoding="utf-8").replace(
        'test "$(git rev-parse HEAD)" = "${VERIFIED_SOURCE_SHA}"',
        'test -n "${VERIFIED_SOURCE_SHA}"',
        1,
    )
    (workflows / "pypi.yml").write_text(bad, encoding="utf-8")
    release_check = _load_release_check_module()
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    gate = release_check.pypi_workflow_gate_check()

    assert gate["ok"] is False
    assert any(
        "bind the checked-out tag to the verified release source" in failure
        for failure in gate["failures"]
    )


def test_release_gate_rejects_missing_tag_and_main_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy2(PYPI_WORKFLOW, workflows / "pypi.yml")
    bad = RELEASE_WORKFLOW.read_text(encoding="utf-8").replace(
        'tag_type="$(git cat-file -t "refs/tags/${RELEASE_TAG}")"',
        'tag_type="tag"',
    )
    (workflows / "release.yml").write_text(bad, encoding="utf-8")
    release_check = _load_release_check_module()
    monkeypatch.setattr(release_check, "ROOT", tmp_path)

    gate = release_check.pypi_workflow_gate_check()

    assert gate["ok"] is False
    assert any("local main ancestry" in failure for failure in gate["failures"])
