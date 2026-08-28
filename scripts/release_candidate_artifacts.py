"""Deterministic archive evidence and source-correspondence checks.

The release-candidate build and manifest verifier share this module so an
artifact cannot be accepted through a weaker second implementation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import tarfile
from typing import Mapping, Sequence
import zipfile


MEMBER_MANIFEST_ALGORITHM = "archive-regular-files-sha256-v1"

_FORBIDDEN_PARTS = {
    ".execution",
    ".hermes",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "activity-state",
    "activity_state",
    "backups",
    "lancedb",
    "logs",
    "quarantine",
    "scope-recall",
    "venv",
}
_FORBIDDEN_SUFFIXES = (
    ".db",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".sqlite-shm",
    ".sqlite-wal",
)
_FORBIDDEN_SECRET_NAMES = {
    ".env",
    "credentials.json",
    "private_key",
    "secrets.json",
    "token.json",
    "tokens.json",
}
_SDIST_GENERATED_EXACT = {
    "PKG-INFO",
    "setup.cfg",
}
_SDIST_GENERATED_EGG_INFO_NAMES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}


class ArtifactVerificationError(RuntimeError):
    """Raised when distribution bytes do not prove the candidate boundary."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(raw: str) -> str:
    normalized = str(raw).replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or normalized != pure.as_posix()
    ):
        raise ArtifactVerificationError(f"unsafe archive member: {raw!r}")
    return normalized


def read_archive_members(path: Path) -> dict[str, bytes]:
    """Read regular members from a wheel or sdist without extracting them."""

    artifact = Path(path).resolve(strict=True)
    members: dict[str, bytes] = {}
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            for info in archive.infolist():
                name = _safe_member_name(info.filename)
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise ArtifactVerificationError(
                        f"archive contains a symbolic link: {name}"
                    )
                if name in members:
                    raise ArtifactVerificationError(
                        f"archive contains a duplicate member: {name}"
                    )
                members[name] = archive.read(info)
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, "r:*") as archive:
            for info in archive.getmembers():
                name = _safe_member_name(info.name)
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ArtifactVerificationError(
                        f"archive contains a non-regular member: {name}"
                    )
                if name in members:
                    raise ArtifactVerificationError(
                        f"archive contains a duplicate member: {name}"
                    )
                handle = archive.extractfile(info)
                if handle is None:
                    raise ArtifactVerificationError(
                        f"archive member cannot be read: {name}"
                    )
                members[name] = handle.read()
    else:
        raise ArtifactVerificationError(
            f"unsupported distribution artifact: {artifact.name}"
        )
    if not members:
        raise ArtifactVerificationError("distribution artifact has no regular files")
    return dict(sorted(members.items()))


def member_manifest_from_members(members: Mapping[str, bytes]) -> dict[str, object]:
    entries = [
        {
            "path": name,
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
        }
        for name, content in sorted(members.items())
    ]
    return {
        "algorithm": MEMBER_MANIFEST_ALGORITHM,
        "file_count": len(entries),
        "member_manifest_sha256": sha256_bytes(canonical_bytes(entries)),
        "files": entries,
    }


def archive_member_manifest(path: Path) -> dict[str, object]:
    return member_manifest_from_members(read_archive_members(path))


def _strip_sdist_root(name: str, expected_root: str) -> str:
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or parts[0] != expected_root:
        raise ArtifactVerificationError(
            f"sdist member is outside expected root {expected_root!r}: {name}"
        )
    return PurePosixPath(*parts[1:]).as_posix()


def _source_entries(source_manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_files = source_manifest.get("files")
    if not isinstance(raw_files, list):
        raise ArtifactVerificationError("source manifest files must be an array")
    entries: dict[str, Mapping[str, object]] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ArtifactVerificationError("source manifest entry must be an object")
        path = str(raw.get("path") or "")
        if not path or path in entries:
            raise ArtifactVerificationError("source manifest has invalid paths")
        entries[path] = raw
    return entries


def verify_wheel_source_correspondence(
    members: Mapping[str, bytes],
    source_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Bind every packaged runtime Python file to the exact tracked source bytes."""

    source = _source_entries(source_manifest)
    packaged: dict[str, str] = {}
    mismatches: list[str] = []
    for member_name, content in sorted(members.items()):
        parts = PurePosixPath(member_name).parts
        if len(parts) < 2 or parts[0] != "scope_recall" or not member_name.endswith(".py"):
            continue
        source_path = PurePosixPath(*parts[1:]).as_posix()
        packaged[source_path] = member_name
        wanted = source.get(source_path)
        if wanted is None or wanted.get("sha256") != sha256_bytes(content):
            mismatches.append(member_name)

    expected = {
        path
        for path in source
        if path.endswith(".py")
        and (
            "/" not in path
            or path.startswith("_internal/")
            or path.startswith("scripts/")
        )
    }
    missing = sorted(expected - set(packaged))
    if mismatches or missing:
        raise ArtifactVerificationError(
            "wheel/source correspondence failed: "
            f"mismatched={sorted(mismatches)!r}, missing={missing!r}"
        )
    return {
        "verified_runtime_python_files": len(packaged),
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
    }


def _is_generated_sdist_member(relative: str) -> bool:
    pure = PurePosixPath(relative)
    if relative in _SDIST_GENERATED_EXACT:
        return True
    return (
        len(pure.parts) == 2
        and pure.parts[0].endswith(".egg-info")
        and pure.name in _SDIST_GENERATED_EGG_INFO_NAMES
    )


def verify_sdist_source_correspondence(
    members: Mapping[str, bytes],
    source_manifest: Mapping[str, object],
    *,
    expected_root: str,
) -> dict[str, object]:
    """Require every non-generated sdist member to equal a tracked source file."""

    source = _source_entries(source_manifest)
    verified = 0
    untracked: list[str] = []
    mismatches: list[str] = []
    for member_name, content in sorted(members.items()):
        relative = _strip_sdist_root(member_name, expected_root)
        wanted = source.get(relative)
        if wanted is None:
            if not _is_generated_sdist_member(relative):
                untracked.append(member_name)
            continue
        verified += 1
        if wanted.get("sha256") != sha256_bytes(content):
            mismatches.append(member_name)
    if untracked or mismatches:
        raise ArtifactVerificationError(
            "sdist/source correspondence failed: "
            f"untracked={untracked!r}, mismatched={mismatches!r}"
        )
    return {
        "verified_tracked_files": verified,
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
    }


def artifact_name_findings(
    members: Mapping[str, bytes],
    *,
    kind: str,
    sdist_root: str = "",
    allowed_sdist_tests: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Return content-free path-policy findings for actual archive members."""

    allowed_tests = {PurePosixPath(item).as_posix() for item in allowed_sdist_tests}
    findings: list[dict[str, str]] = []
    for member_name in sorted(members):
        relative = (
            _strip_sdist_root(member_name, sdist_root)
            if kind == "sdist"
            else member_name
        )
        pure = PurePosixPath(relative)
        lowered_parts = {part.casefold() for part in pure.parts}
        lowered_name = pure.name.casefold()
        reason = ""
        if lowered_parts & _FORBIDDEN_PARTS:
            reason = "forbidden_runtime_or_local_path"
        elif lowered_name in _FORBIDDEN_SECRET_NAMES:
            reason = "secret_or_live_configuration_name"
        elif lowered_name.endswith(_FORBIDDEN_SUFFIXES):
            reason = "state_log_key_or_cache_file"
        elif "tests" in lowered_parts:
            if kind != "sdist" or relative not in allowed_tests:
                reason = "arbitrary_test_not_allowlisted"
        if reason:
            findings.append({"path": member_name, "reason": reason})
    return findings
