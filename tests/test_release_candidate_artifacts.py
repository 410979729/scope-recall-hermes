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


def _complete_wheel(
    source_files: dict[str, bytes],
    package_files: dict[str, bytes],
    *,
    version: str = "2.0.1",
) -> dict[str, bytes]:
    dist_info = f"hermes_scope_recall-{version}.dist-info"
    members = dict(package_files)
    members.update(
        {
            f"{dist_info}/METADATA": (
                "Metadata-Version: 2.4\n"
                "Name: hermes-scope-recall\n"
                f"Version: {version}\n"
            ).encode(),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\n"
                "Generator: test\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            ).encode(),
            f"{dist_info}/entry_points.txt": (
                "[console_scripts]\n"
                "hermes-scope-recall = scope_recall.cli:main\n"
            ).encode(),
            f"{dist_info}/top_level.txt": b"scope_recall\n",
            f"{dist_info}/licenses/LICENSE": source_files["LICENSE"],
        }
    )
    record_name = f"{dist_info}/RECORD"
    rows = [
        f"{name},{artifacts._record_digest(content)},{len(content)}"
        for name, content in sorted(members.items())
    ]
    rows.append(f"{record_name},,")
    members[record_name] = ("\n".join(rows) + "\n").encode()
    return members


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
        "LICENSE": b"public license\n",
    }
    source = _source_manifest(source_files)
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        _complete_wheel(
            source_files,
            {
                "scope_recall/__init__.py": source_files["__init__.py"],
                "scope_recall/_internal/runtime.py": source_files["_internal/runtime.py"],
                "scope_recall/scripts/doctor.py": source_files["scripts/doctor.py"],
                "scope_recall/README.md": source_files["README.md"],
                "scope_recall/LICENSE": source_files["LICENSE"],
            },
        ),
    )
    sdist = tmp_path / "candidate.tar.gz"
    root = "hermes_scope_recall-2.0.1"
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
    assert wheel_result["verified_package_data_count"] == 2
    assert wheel_result["missing_expected_count"] == 0
    assert wheel_result["unknown_generated_count"] == 0
    assert sdist_result["verified_tracked_files"] == 1


def test_archive_source_mismatch_and_arbitrary_tests_fail_closed(tmp_path: Path) -> None:
    source_files = {
        "__init__.py": b"expected\n",
        "LICENSE": b"license\n",
    }
    source = _source_manifest(source_files)
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        _complete_wheel(
            source_files,
            {
                "scope_recall/__init__.py": b"different\n",
                "scope_recall/LICENSE": source_files["LICENSE"],
                "scope_recall/tests/test_private.py": b"def test_private(): pass\n",
            },
        ),
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


def test_wheel_missing_or_tampered_package_data_fails_closed(tmp_path: Path) -> None:
    source_files = {
        "__init__.py": b"__all__ = []\n",
        "plugin.yaml": b"name: scope-recall\n",
        "config.json": b"{}\n",
        "LICENSE": b"license\n",
    }
    source = _source_manifest(source_files)

    missing = _complete_wheel(
        source_files,
        {
            "scope_recall/__init__.py": source_files["__init__.py"],
            "scope_recall/config.json": source_files["config.json"],
            "scope_recall/LICENSE": source_files["LICENSE"],
        },
    )
    with pytest.raises(artifacts.ArtifactVerificationError, match="missing"):
        artifacts.verify_wheel_source_correspondence(missing, source)

    tampered = _complete_wheel(
        source_files,
        {
            "scope_recall/__init__.py": source_files["__init__.py"],
            "scope_recall/plugin.yaml": source_files["plugin.yaml"],
            "scope_recall/config.json": b'{"tampered": true}\n',
            "scope_recall/LICENSE": source_files["LICENSE"],
        },
    )
    with pytest.raises(artifacts.ArtifactVerificationError, match="mismatched"):
        artifacts.verify_wheel_source_correspondence(tampered, source)


def test_wheel_unknown_generated_member_fails_closed(tmp_path: Path) -> None:
    source_files = {
        "__init__.py": b"__all__ = []\n",
        "LICENSE": b"license\n",
    }
    source = _source_manifest(source_files)
    members = _complete_wheel(
        source_files,
        {
            "scope_recall/__init__.py": source_files["__init__.py"],
            "scope_recall/LICENSE": source_files["LICENSE"],
        },
    )
    members["hermes_scope_recall-2.0.1.dist-info/private.txt"] = b"unexpected\n"

    with pytest.raises(artifacts.ArtifactVerificationError, match="generated-member"):
        artifacts.verify_wheel_source_correspondence(members, source)


def test_sdist_test_allowlist_is_exact(tmp_path: Path) -> None:
    root = "hermes_scope_recall-2.0.1"
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
