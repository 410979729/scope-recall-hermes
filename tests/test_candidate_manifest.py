"""Exact release-candidate manifest contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report.candidate_manifest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_candidate_manifest",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_source_manifest_is_deterministic_and_content_free() -> None:
    candidate = _load_module()

    first = candidate.source_manifest(ROOT)
    second = candidate.source_manifest(ROOT)

    assert first == second
    assert first["algorithm"] == "git-ls-files-content-sha256-v1"
    assert first["file_count"] == len(first["files"])
    assert first["file_count"] >= 650
    assert all(set(entry) == {"path", "sha256", "size_bytes"} for entry in first["files"])
    serialized = json.dumps(first, sort_keys=True)
    assert str(ROOT) not in serialized
    assert ":\\" not in serialized


def test_candidate_manifest_binds_versions_schemas_and_denies_release_authority() -> None:
    candidate = _load_module()

    payload = candidate.build_candidate_manifest(
        ROOT,
        expected_version="2.0.0",
        require_clean=False,
    )

    assert payload["schema_version"] == "scope-recall.candidate-manifest.v1"
    assert payload["candidate_version"] == "2.0.0"
    assert payload["source"]["manifest"]["file_count"] >= 650
    assert payload["schemas"]["sqlite_schema_version"] > 0
    assert all(payload["schemas"][key] for key in payload["schemas"] if key.endswith("sha256"))
    assert payload["private_artifacts_included"] is False
    assert payload["authorization"] == {
        "merge": False,
        "tag": False,
        "release": False,
        "deploy": False,
    }
    unsigned = dict(payload)
    digest = unsigned.pop("manifest_sha256")
    assert digest == candidate._sha256_bytes(candidate._canonical_bytes(unsigned))


def test_candidate_manifest_requires_clean_exact_epoch(monkeypatch) -> None:
    candidate = _load_module()

    monkeypatch.setattr(candidate, "_run_git", lambda *_args, **_kwargs: b"dirty\0")
    with pytest.raises(candidate.CandidateManifestError, match="not clean"):
        candidate._repository_identity(ROOT, require_clean=True)


def test_candidate_manifest_refuses_unignored_output() -> None:
    candidate = _load_module()

    with pytest.raises(candidate.CandidateManifestError, match="unignored"):
        candidate._require_ignored_output(
            ROOT,
            ROOT / "CANDIDATE_MANIFEST_NOT_IGNORED.json",
        )
