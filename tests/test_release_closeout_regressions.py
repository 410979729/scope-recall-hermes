"""Regression coverage for release/Windows/scanner closeout findings."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path
import sys
import tomllib
import zipfile

import pytest
from packaging.requirements import Requirement

from scope_recall import memory_queries


ROOT = Path(__file__).resolve().parents[1]


def _load_release_asset_stager():
    path = ROOT / ".github" / "scripts" / "stage_release_assets.py"
    spec = importlib.util.spec_from_file_location("scope_recall_stage_release_assets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_release_check():
    path = ROOT / "scripts" / "check.release.py"
    spec = importlib.util.spec_from_file_location("scope_recall_release_closeout", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_release_provenance():
    path = ROOT / ".github" / "scripts" / "release_provenance.py"
    spec = importlib.util.spec_from_file_location("scope_recall_release_provenance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_release_provenance_fixture(tmp_path: Path):
    provenance = _load_release_provenance()
    packages = tmp_path / "dist"
    packages.mkdir()
    (packages / "candidate.whl").write_bytes(b"wheel")
    (packages / "candidate.tar.gz").write_bytes(b"sdist")
    receipt = tmp_path / "RELEASE-PROVENANCE.json"
    provenance.write_provenance(
        receipt,
        repository="owner/repo",
        source_sha="a" * 40,
        source_tree="b" * 40,
        release_tag="v1.10.4",
        workflow_run_id="12345",
        workflow_run_attempt="2",
        packages_dir=packages,
    )
    return provenance, packages, receipt


def test_public_migration_info_exposes_only_logical_status() -> None:
    private_windows = "C:/" + "Users/Alice/private/memory.sqlite3"
    private_posix = "/" + "home/alice/private/memory.sqlite3"

    public = memory_queries._public_migration_info(  # type: ignore[attr-defined]
        {
            "migrated": True,
            "config_copied": True,
            "source": private_windows,
            "target": private_posix,
            "nested": {"backup_path": private_windows},
        }
    )

    assert public == {"migrated": True, "config_copied": True}


def test_pypi_stager_separates_packages_from_checksum(tmp_path: Path) -> None:
    stager = _load_release_asset_stager()
    source = tmp_path / "download"
    packages = tmp_path / "staged" / "packages"
    metadata = tmp_path / "staged" / "metadata"
    source.mkdir()
    version = "1.10.4"
    wheel = source / f"hermes_scope_recall-{version}-py3-none-any.whl"
    sdist = source / f"hermes_scope_recall-{version}.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    sums = "\n".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in (wheel, sdist)
    )
    (source / "SHA256SUMS").write_text(sums + "\n", encoding="utf-8")
    (source / "RELEASE-PROVENANCE.json").write_text("{}\n", encoding="utf-8")

    receipt = stager.stage_release_assets(
        source,
        packages_dir=packages,
        metadata_dir=metadata,
        version=version,
    )

    assert receipt["packages"] == sorted((wheel.name, sdist.name))
    assert sorted(path.name for path in packages.iterdir()) == sorted(
        (wheel.name, sdist.name)
    )
    assert sorted(path.name for path in metadata.iterdir()) == [
        "RELEASE-PROVENANCE.json",
        "SHA256SUMS",
    ]


def test_windows_cli_capture_helpers_decode_utf8_strictly() -> None:
    missing: list[str] = []
    for relative in (
        "tests/test_golden_benchmark.py",
        "tests/test_lexical_migration_cli.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
                and function.attr == "run"
            ):
                continue
            keywords = {item.arg: item.value for item in node.keywords if item.arg}
            text_mode = keywords.get("text")
            if not (
                isinstance(text_mode, ast.Constant) and text_mode.value is True
            ):
                continue
            encoding = keywords.get("encoding")
            errors = keywords.get("errors")
            if not (
                isinstance(encoding, ast.Constant)
                and encoding.value == "utf-8"
                and isinstance(errors, ast.Constant)
                and errors.value == "strict"
            ):
                missing.append(f"{relative}:{node.lineno}")

    assert missing == []


def test_distribution_persona_scan_covers_markdown_and_all_private_agent_names(
    tmp_path: Path,
) -> None:
    release_check = _load_release_check()
    artifact = tmp_path / "candidate.whl"
    private_agent = "Tian" + "shu"
    private_agent_2 = "Yu" + "heng"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("scope_recall/OPERATIONS.md", f"Contact {private_agent}.\n")
        archive.writestr("scope_recall/runtime.py", f'AGENT = "{private_agent_2}"\n')

    findings = release_check.scan_distribution_artifact(artifact)

    assert {item["marker"] for item in findings["private_persona"]} == {
        "private_agent_tianshu",
        "agent_persona_yuheng",
    }


def test_release_provenance_binds_source_run_and_distribution_bytes(tmp_path: Path) -> None:
    provenance, packages, receipt = _write_release_provenance_fixture(tmp_path)

    verified = provenance.verify_provenance(
        receipt,
        expected_repository="owner/repo",
        expected_source_sha="a" * 40,
        expected_source_tree="b" * 40,
        expected_release_tag="v1.10.4",
        expected_workflow_run_id="12345",
        workflow_run_status="completed",
        workflow_run_conclusion="success",
        packages_dir=packages,
    )
    assert verified["workflow"]["run_attempt"] == "2"
    assert sorted(verified["artifacts"]) == ["candidate.tar.gz", "candidate.whl"]

    try:
        provenance.verify_provenance(
            receipt,
            expected_repository="owner/repo",
            expected_source_sha="c" * 40,
            expected_source_tree="b" * 40,
            expected_release_tag="v1.10.4",
            expected_workflow_run_id="12345",
            workflow_run_status="completed",
            workflow_run_conclusion="success",
            packages_dir=packages,
        )
    except ValueError as exc:
        assert "source_sha" in str(exc)
    else:
        raise AssertionError("mismatched source SHA was accepted")


def test_release_provenance_accepts_completed_successful_workflow_run(
    tmp_path: Path,
) -> None:
    provenance, packages, receipt = _write_release_provenance_fixture(tmp_path)

    verified = provenance.verify_provenance(
        receipt,
        expected_repository="owner/repo",
        expected_source_sha="a" * 40,
        expected_source_tree="b" * 40,
        expected_release_tag="v1.10.4",
        expected_workflow_run_id="12345",
        workflow_run_status="completed",
        workflow_run_conclusion="success",
        packages_dir=packages,
    )

    assert verified["workflow"]["run_id"] == "12345"


@pytest.mark.parametrize(
    ("workflow_run_status", "workflow_run_conclusion", "message"),
    (
        ("in_progress", "", "completed"),
        ("completed", "failure", "successful"),
    ),
)
def test_release_provenance_rejects_non_successful_workflow_runs(
    tmp_path: Path,
    workflow_run_status: str,
    workflow_run_conclusion: str,
    message: str,
) -> None:
    provenance, packages, receipt = _write_release_provenance_fixture(tmp_path)

    with pytest.raises(ValueError, match=message):
        provenance.verify_provenance(
            receipt,
            expected_repository="owner/repo",
            expected_source_sha="a" * 40,
            expected_source_tree="b" * 40,
            expected_release_tag="v1.10.4",
            expected_workflow_run_id="12345",
            workflow_run_status=workflow_run_status,
            workflow_run_conclusion=workflow_run_conclusion,
            packages_dir=packages,
        )


def test_release_provenance_cli_forwards_failed_run_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance, packages, receipt = _write_release_provenance_fixture(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_provenance.py",
            "verify",
            "--provenance",
            str(receipt),
            "--repository",
            "owner/repo",
            "--source-sha",
            "a" * 40,
            "--source-tree",
            "b" * 40,
            "--release-tag",
            "v1.10.4",
            "--workflow-run-id",
            "12345",
            "--workflow-run-status",
            "completed",
            "--workflow-run-conclusion",
            "failure",
            "--packages-dir",
            str(packages),
        ],
    )

    with pytest.raises(ValueError, match="successful"):
        provenance.main()


def test_package_integration_subprocesses_are_bounded() -> None:
    relative = "tests/test_journal_source_restore_package.py"
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
            and function.attr == "run"
        ):
            continue
        if "timeout" not in {keyword.arg for keyword in node.keywords}:
            missing.append(node.lineno)

    assert missing == []


def test_release_dependencies_are_bounded_and_hash_locked() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    dependency_groups = [
        pyproject["build-system"]["requires"],
        project["dependencies"],
        project["optional-dependencies"]["lancedb"],
        project["optional-dependencies"]["pgvector"],
        project["optional-dependencies"]["all"],
        project["optional-dependencies"]["dev"],
    ]
    unbounded = [
        spec
        for group in dependency_groups
        for spec in group
        if "<" not in spec
    ]
    assert unbounded == []

    lock = (ROOT / "constraints" / "release-hashed.txt").read_text(
        encoding="utf-8"
    )
    assert "--hash=sha256:" in lock
    assert "F:/" not in lock
    assert "C:/" not in lock
    assert "\\Users\\" not in lock

    for workflow_name in ("release.yml", "pypi.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )
        assert "pip install --require-hashes -r constraints/release-hashed.txt" in workflow
        assert 'pip install --no-deps ".[lancedb,dev]"' in workflow


def test_release_constraints_have_no_extras_and_preserve_runtime_extra_closure() -> None:
    requirements = [
        Requirement(line)
        for raw in (ROOT / "constraints" / "release.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]
    assert all(not requirement.extras for requirement in requirements)

    names = {requirement.name.lower() for requirement in requirements}
    assert {
        "colorama",
        "cryptography",
        "httptools",
        "python-dotenv",
        "pyyaml",
        "socksio",
        "uvloop",
        "watchfiles",
        "websockets",
    } <= names


def test_release_workflows_do_not_resolve_after_hash_locked_install() -> None:
    workflows = {
        name: (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("release.yml", "pypi.yml")
    }

    for name, workflow in workflows.items():
        assert "python -m pip install --no-deps -e .hermes-agent-src" in workflow, name
        assert "python -m pip install -e .hermes-agent-src" not in workflow, name
    assert "python -m build --no-isolation" in workflows["release.yml"]
    assert "python -m build\n" not in workflows["release.yml"]


def test_release_hash_lock_closes_over_pinned_hermes_runtime_dependencies() -> None:
    lock_lines = (ROOT / "constraints" / "release-hashed.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    locked = {
        line.split("==", 1)[0].lower()
        for line in lock_lines
        if line and not line[0].isspace() and "==" in line
    }
    hermes_runtime = {
        "certifi",
        "concurrent-log-handler",
        "croniter",
        "cryptography",
        "fastapi",
        "fire",
        "httpx",
        "jinja2",
        "markdown",
        "nemo-relay",
        "openai",
        "packaging",
        "pathspec",
        "pillow",
        "prompt-toolkit",
        "psutil",
        "ptyprocess",
        "pydantic",
        "pyjwt",
        "python-dotenv",
        "python-multipart",
        "pywin32",
        "pywinpty",
        "pyyaml",
        "requests",
        "rich",
        "ruamel-yaml",
        "tenacity",
        "tzdata",
        "urllib3",
        "uvicorn",
        "websockets",
    }

    assert hermes_runtime <= locked, sorted(hermes_runtime - locked)


def test_ci_exercises_minimum_and_maximum_runtime_dependencies() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    runtime_min = (ROOT / "constraints" / "runtime-min.txt").read_text(
        encoding="utf-8"
    )
    runtime_min_requirements = [
        Requirement(line)
        for raw in runtime_min.splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]
    pyyaml_constraints = [
        requirement
        for requirement in runtime_min_requirements
        if requirement.name.lower() == "pyyaml"
    ]
    assert "linux-dependency-min-py311" in workflow
    assert "constraints/runtime-min.txt" in workflow
    assert "linux-dependency-max-py312" in workflow
    assert "constraints/runtime-max.txt" in workflow
    assert "plugin + pinned Hermes integration dependency set" in runtime_min
    assert len(pyyaml_constraints) == 1
    assert str(pyyaml_constraints[0].specifier) == "==6.0.3"
    assert "Hermes Agent v0.19.1 / v2026.7.30" in workflow
    assert (
        "HERMES_AGENT_COMMIT: cc4cab2f592e60a197e796506de9168f74baf3ea"
        in workflow
    )


def test_sdist_prunes_unreviewed_test_fixtures_before_explicit_restore_tests() -> None:
    manifest_lines = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    prune_index = manifest_lines.index("prune tests")
    selected_test_indexes = [
        index
        for index, line in enumerate(manifest_lines)
        if line.startswith("include tests/")
    ]

    assert selected_test_indexes
    assert all(prune_index < index for index in selected_test_indexes)
