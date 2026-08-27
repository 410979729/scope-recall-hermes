"""Repository-governance tests for the canonical local census."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report.repository_census.py"


def _load_census_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_repository_census",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_census_is_deterministic_complete_and_local_safe() -> None:
    census = _load_census_module()

    first = census.build_census(ROOT)
    second = census.build_census(ROOT)

    assert first == second
    assert first["schema_version"] == "scope-recall.repository-census.v1"
    assert first["algorithm"]["name"] == "git-ls-files-sha256-v1"
    assert first["algorithm"]["ignored_files_included"] is False
    files = first["files"]
    paths = [entry["path"] for entry in files]
    assert paths == sorted(set(paths))
    assert first["file_count"] == len(files)
    assert first["tracked_file_count"] + first["untracked_file_count"] == len(files)
    assert sum(first["counts"]["category"].values()) == len(files)
    assert sum(first["counts"]["lifecycle"].values()) == len(files)
    assert not any(path.startswith(".execution/") for path in paths)
    assert not any(path.startswith(".venv/") for path in paths)
    assert {
        "REPOSITORY_CENSUS_SUMMARY.md",
        "docs/compatibility-removal-registry.json",
        "docs/repository-census.anomalies.json",
        "docs/repository-census.schema.json",
        "docs/repository-deletion-evidence.json",
        "scripts/report.repository_census.py",
        "tests/test_repository_census.py",
    } <= set(paths)
    expected_hash = hashlib.sha256(census._canonical_json_bytes(files)).hexdigest()
    assert first["inventory_sha256"] == expected_hash


def test_repository_census_schema_and_committed_governance_are_coherent() -> None:
    schema = json.loads(
        (ROOT / "docs" / "repository-census.schema.json").read_text(
            encoding="utf-8"
        )
    )
    anomalies = json.loads(
        (ROOT / "docs" / "repository-census.anomalies.json").read_text(
            encoding="utf-8"
        )
    )
    deletion = json.loads(
        (ROOT / "docs" / "repository-deletion-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    compatibility = json.loads(
        (ROOT / "docs" / "compatibility-removal-registry.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["properties"]["schema_version"]["const"] == (
        "scope-recall.repository-census.v1"
    )
    assert schema["properties"]["files"]["items"]["$ref"] == "#/$defs/fileEntry"
    assert anomalies["blocking_anomalies"] == []
    assert deletion["program"] == "Program 6A"
    assert deletion["base_commit"] == "b155932a7d7de535746c51dcc0ba7085d5e66f1b"
    assert deletion["deleted_files"] == []
    assert deletion["deletion_authorized"] is False
    assert {entry["id"] for entry in compatibility["entries"]} == {
        "CR-001",
        "CR-002",
        "CR-003",
    }
    assert all(
        entry["removal_epoch"] == "not_scheduled"
        for entry in compatibility["entries"]
    )


def test_repository_census_refuses_unignored_in_tree_output() -> None:
    census = _load_census_module()

    with pytest.raises(census.CensusError, match="not Git-ignored"):
        census._require_ignored_output(
            ROOT,
            ROOT / "REPOSITORY_CENSUS_NOT_IGNORED.json",
        )


def test_repository_deletion_evidence_matches_worktree_delta() -> None:
    census = _load_census_module()
    deleted = census._run_git(
        ROOT,
        [
            "diff",
            "--diff-filter=D",
            "--name-only",
            "b155932a7d7de535746c51dcc0ba7085d5e66f1b",
            "--",
        ],
    )

    assert deleted == b""
