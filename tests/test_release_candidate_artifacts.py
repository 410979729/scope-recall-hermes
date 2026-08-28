"""Real-archive source and publication-boundary contracts."""

from __future__ import annotations

import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts import release_candidate_artifacts as artifacts


def _source_manifest(files: dict[str, bytes]) -> dict[str, object]:
    entries = [
        {
            "path": path,
            "sha256": artifacts.sha256_bytes(content),
            "size_bytes": len(content),
        }
        for path, content in sorted(files.items())
    ]
    return {
        "algorithm": "git-ls-files-content-sha256-v1",
        "file_count": len(entries),
        "manifest_sha256": artifacts.sha256_bytes(artifacts.canonical_bytes(entries)),
        "files": entries,
    }


def _write_wheel(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _write_sdist(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_real_archive_member_manifest_is_deterministic_and_content_free(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        {
            "scope_recall/z.py": b"z = 1\n",
            "scope_recall/a.py": b"a = 1\n",
        },
    )

    first = artifacts.archive_member_manifest(wheel)
    second = artifacts.archive_member_manifest(wheel)

    assert first == second
    assert first["algorithm"] == "archive-regular-files-sha256-v1"
    assert [entry["path"] for entry in first["files"]] == [
        "scope_recall/a.py",
        "scope_recall/z.py",
    ]
    assert str(tmp_path) not in str(first)


def test_wheel_and_sdist_members_bind_to_exact_tracked_source(tmp_path: Path) -> None:
    source_files = {
        "__init__.py": b"__all__ = []\n",
        "_internal/runtime.py": b"ready = True\n",
        "scripts/doctor.py": b"def main(): return 0\n",
        "README.md": b"public readme\n",
    }
    source = _source_manifest(source_files)
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        {
            "scope_recall/__init__.py": source_files["__init__.py"],
            "scope_recall/_internal/runtime.py": source_files["_internal/runtime.py"],
            "scope_recall/scripts/doctor.py": source_files["scripts/doctor.py"],
        },
    )
    sdist = tmp_path / "candidate.tar.gz"
    root = "hermes_scope_recall-2.0.0"
    _write_sdist(
        sdist,
        {
            f"{root}/README.md": source_files["README.md"],
            f"{root}/setup.cfg": b"[metadata]\n",
            f"{root}/hermes_scope_recall.egg-info/PKG-INFO": b"generated\n",
        },
    )

    wheel_result = artifacts.verify_wheel_source_correspondence(
        artifacts.read_archive_members(wheel),
        source,
    )
    sdist_result = artifacts.verify_sdist_source_correspondence(
        artifacts.read_archive_members(sdist),
        source,
        expected_root=root,
    )

    assert wheel_result["verified_runtime_python_files"] == 3
    assert sdist_result["verified_tracked_files"] == 1


def test_archive_source_mismatch_and_arbitrary_tests_fail_closed(tmp_path: Path) -> None:
    source = _source_manifest({"__init__.py": b"expected\n"})
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        {
            "scope_recall/__init__.py": b"different\n",
            "scope_recall/tests/test_private.py": b"def test_private(): pass\n",
        },
    )
    members = artifacts.read_archive_members(wheel)

    with pytest.raises(artifacts.ArtifactVerificationError, match="correspondence"):
        artifacts.verify_wheel_source_correspondence(members, source)
    assert artifacts.artifact_name_findings(members, kind="wheel") == [
        {
            "path": "scope_recall/tests/test_private.py",
            "reason": "arbitrary_test_not_allowlisted",
        }
    ]


def test_sdist_test_allowlist_is_exact(tmp_path: Path) -> None:
    root = "hermes_scope_recall-2.0.0"
    sdist = tmp_path / "candidate.tar.gz"
    _write_sdist(
        sdist,
        {
            f"{root}/tests/test_allowed.py": b"allowed\n",
            f"{root}/tests/test_extra.py": b"extra\n",
        },
    )

    findings = artifacts.artifact_name_findings(
        artifacts.read_archive_members(sdist),
        kind="sdist",
        sdist_root=root,
        allowed_sdist_tests=("tests/test_allowed.py",),
    )

    assert findings == [
        {
            "path": f"{root}/tests/test_extra.py",
            "reason": "arbitrary_test_not_allowlisted",
        }
    ]
