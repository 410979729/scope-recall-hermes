"""Exact release-candidate manifest contracts."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile

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


def _write_provenance(candidate, tmp_path: Path) -> Path:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    wheel = artifact_dir / "hermes_scope_recall-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("scope_recall/__init__.py", "__all__ = []\n")
    sdist = artifact_dir / "hermes_scope_recall-2.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        content = b"candidate fixture\n"
        info = tarfile.TarInfo("hermes_scope_recall-2.0.0/README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    identity = candidate._repository_identity(ROOT, require_clean=False)
    exact_source = candidate.source_manifest(ROOT)
    payload = {
        "schema_version": "scope-recall.build-provenance.v1",
        "source_commit": identity["commit"],
        "source_tree": identity["tree"],
        "source_manifest_sha256": exact_source["manifest_sha256"],
        "source_dirty": False,
        "python_version": "test",
        "build_backend": "setuptools.build_meta",
        "build_command": ["python", "-m", "build"],
        "wheel": {
            "name": wheel.name,
            "relative_path": f"artifacts/{wheel.name}",
            "sha256": candidate._sha256_file(wheel),
            "member_manifest_sha256": candidate.archive_member_manifest(wheel)[
                "member_manifest_sha256"
            ],
        },
        "sdist": {
            "name": sdist.name,
            "relative_path": f"artifacts/{sdist.name}",
            "sha256": candidate._sha256_file(sdist),
            "member_manifest_sha256": candidate.archive_member_manifest(sdist)[
                "member_manifest_sha256"
            ],
        },
        "install_verification": {"wheel": "passed", "sdist": "passed"},
    }
    provenance = tmp_path / "BUILD_PROVENANCE.json"
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    return provenance


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


def test_candidate_manifest_binds_versions_schemas_and_denies_release_authority(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    provenance = _write_provenance(candidate, tmp_path)

    payload = candidate.build_candidate_manifest(
        ROOT,
        provenance_path=provenance,
        expected_version="2.0.0",
        require_clean=False,
    )

    assert payload["schema_version"] == "scope-recall.candidate-manifest.v1"
    assert payload["candidate_version"] == "2.0.0"
    assert payload["source"]["manifest"]["file_count"] >= 650
    assert payload["provenance"]["sha256"] == candidate._sha256_file(provenance)
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "wheel",
        "sdist",
    }
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


def test_candidate_manifest_refuses_artifact_provenance_mismatch(
    tmp_path: Path,
) -> None:
    candidate = _load_module()
    provenance = _write_provenance(candidate, tmp_path)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["wheel"]["sha256"] = "0" * 64
    provenance.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        candidate.CandidateManifestError,
        match="CANDIDATE_ARTIFACT_PROVENANCE_MISMATCH",
    ):
        candidate.build_candidate_manifest(
            ROOT,
            provenance_path=provenance,
            expected_version="2.0.0",
            require_clean=False,
        )


def test_candidate_manifest_cli_accepts_provenance_not_arbitrary_artifacts() -> None:
    candidate = _load_module()

    with pytest.raises(SystemExit):
        candidate.parse_args(["--artifact", "unbound.whl"])
    parsed = candidate.parse_args(["--provenance", "BUILD_PROVENANCE.json"])
    assert parsed.provenance == Path("BUILD_PROVENANCE.json")


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
