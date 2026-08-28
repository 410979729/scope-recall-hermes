"""Deterministic archive evidence and source-correspondence checks.

The release-candidate build and manifest verifier share this module so an
artifact cannot be accepted through a weaker second implementation.
"""

from __future__ import annotations

import base64
import csv
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
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
_WHEEL_REQUIRED_ROOT_DATA = {
    ".env.example",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "DESIGN.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "SECURITY.md",
    "config.json",
    "plugin.yaml",
    "py.typed",
    "pyproject.toml",
}
_WHEEL_DIST_INFO_MEMBERS = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
    "top_level.txt",
}
_WHEEL_DIST_INFO_ROOT = re.compile(
    r"^hermes_scope_recall-(?P<version>[0-9]+(?:\.[0-9]+)+(?:[^/]*)?)\.dist-info$"
)


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


def _expected_wheel_source_paths(
    source: Mapping[str, Mapping[str, object]],
) -> set[str]:
    expected: set[str] = set()
    for path in source:
        pure = PurePosixPath(path)
        parts = pure.parts
        if len(parts) == 1 and (path.endswith(".py") or path in _WHEEL_REQUIRED_ROOT_DATA):
            expected.add(path)
        elif parts and parts[0] == "_internal" and path.endswith(".py"):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "scripts" and (
            path.endswith(".py") or path.endswith(".json")
        ):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "docs" and path.endswith(".md"):
            expected.add(path)
        elif (
            len(parts) == 3
            and parts[:2] == ("docs", "benchmarks")
            and path.endswith(".md")
        ):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "benchmarks" and path.endswith(".json"):
            expected.add(path)
        elif (
            len(parts) == 3
            and parts[:2] == ("examples", "external_bridge")
            and pure.suffix in {".jsonl", ".sql"}
        ):
            expected.add(path)
    return expected


def _wheel_dist_info_root(members: Mapping[str, bytes]) -> tuple[str, str]:
    roots = {
        PurePosixPath(name).parts[0]
        for name in members
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if len(roots) != 1:
        raise ArtifactVerificationError("wheel must contain exactly one dist-info root")
    root = next(iter(roots))
    match = _WHEEL_DIST_INFO_ROOT.fullmatch(root)
    if match is None:
        raise ArtifactVerificationError(f"unexpected wheel dist-info root: {root}")
    return root, str(match.group("version"))


def _record_digest(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii")
    return "sha256=" + encoded.rstrip("=")


def _validate_wheel_generated_members(
    members: Mapping[str, bytes],
    *,
    dist_info_root: str,
    version: str,
    source: Mapping[str, Mapping[str, object]],
) -> None:
    relative_generated = {
        PurePosixPath(name).relative_to(dist_info_root).as_posix()
        for name in members
        if PurePosixPath(name).parts[0] == dist_info_root
    }
    if relative_generated != _WHEEL_DIST_INFO_MEMBERS:
        raise ArtifactVerificationError(
            "wheel generated-member contract failed: "
            f"missing={sorted(_WHEEL_DIST_INFO_MEMBERS - relative_generated)!r}, "
            f"unexpected={sorted(relative_generated - _WHEEL_DIST_INFO_MEMBERS)!r}"
        )

    metadata = Parser().parsestr(
        members[f"{dist_info_root}/METADATA"].decode("utf-8")
    )
    if metadata.get("Name") != "hermes-scope-recall" or metadata.get(
        "Version"
    ) != version:
        raise ArtifactVerificationError("wheel METADATA identity contract failed")
    wheel_lines = {
        line.strip()
        for line in members[f"{dist_info_root}/WHEEL"].decode("utf-8").splitlines()
    }
    if "Root-Is-Purelib: true" not in wheel_lines or "Tag: py3-none-any" not in wheel_lines:
        raise ArtifactVerificationError("wheel WHEEL portability contract failed")
    entry_point_lines = {
        line.strip()
        for line in members[f"{dist_info_root}/entry_points.txt"]
        .decode("utf-8")
        .splitlines()
    }
    if "hermes-scope-recall = scope_recall.cli:main" not in entry_point_lines:
        raise ArtifactVerificationError("wheel console entry-point contract failed")
    if members[f"{dist_info_root}/top_level.txt"].decode("utf-8").strip() != "scope_recall":
        raise ArtifactVerificationError("wheel top-level package contract failed")
    license_entry = source.get("LICENSE")
    if (
        license_entry is None
        or license_entry.get("sha256")
        != sha256_bytes(members[f"{dist_info_root}/licenses/LICENSE"])
    ):
        raise ArtifactVerificationError("wheel generated license differs from source")

    record_name = f"{dist_info_root}/RECORD"
    rows = list(
        csv.reader(
            io.StringIO(members[record_name].decode("utf-8", errors="strict"))
        )
    )
    if any(len(row) != 3 for row in rows):
        raise ArtifactVerificationError("wheel RECORD rows must have three columns")
    record = {row[0]: (row[1], row[2]) for row in rows}
    if len(record) != len(rows) or set(record) != set(members):
        raise ArtifactVerificationError("wheel RECORD member set mismatch")
    for name, content in members.items():
        digest, size = record[name]
        if name == record_name:
            if digest or size:
                raise ArtifactVerificationError("wheel RECORD self row must be unhashed")
        elif digest != _record_digest(content) or size != str(len(content)):
            raise ArtifactVerificationError(f"wheel RECORD hash/size mismatch: {name}")


def verify_wheel_source_correspondence(
    members: Mapping[str, bytes],
    source_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Enforce the explicit, bidirectional wheel/source member policy."""

    source = _source_entries(source_manifest)
    expected = _expected_wheel_source_paths(source)
    packaged: dict[str, str] = {}
    mismatches: list[str] = []
    unexpected: list[str] = []
    for member_name, content in sorted(members.items()):
        parts = PurePosixPath(member_name).parts
        if len(parts) < 2 or parts[0] != "scope_recall":
            continue
        source_path = PurePosixPath(*parts[1:]).as_posix()
        if source_path not in expected:
            unexpected.append(member_name)
            continue
        packaged[source_path] = member_name
        wanted = source.get(source_path)
        if wanted is None or wanted.get("sha256") != sha256_bytes(content):
            mismatches.append(member_name)

    missing = sorted(expected - set(packaged))
    dist_info_root, version = _wheel_dist_info_root(members)
    unknown_roots = sorted(
        name
        for name in members
        if PurePosixPath(name).parts[0] not in {"scope_recall", dist_info_root}
    )
    _validate_wheel_generated_members(
        members,
        dist_info_root=dist_info_root,
        version=version,
        source=source,
    )
    if mismatches or missing or unexpected or unknown_roots:
        raise ArtifactVerificationError(
            "wheel/source correspondence failed: "
            f"mismatched={sorted(mismatches)!r}, missing={missing!r}, "
            f"unexpected={sorted(unexpected)!r}, unknown={unknown_roots!r}"
        )
    policy = {
        "source_paths": sorted(expected),
        "generated_members": sorted(_WHEEL_DIST_INFO_MEMBERS),
    }
    member_manifest = member_manifest_from_members(members)
    return {
        "policy_sha256": sha256_bytes(canonical_bytes(policy)),
        "wheel_version": version,
        "expected_source_member_count": len(expected),
        "verified_runtime_python_files": sum(path.endswith(".py") for path in packaged),
        "verified_package_data_count": sum(not path.endswith(".py") for path in packaged),
        "generated_allowlist_count": len(_WHEEL_DIST_INFO_MEMBERS),
        "missing_expected_count": 0,
        "mismatched_source_count": 0,
        "unexpected_member_count": 0,
        "unknown_generated_count": 0,
        "wheel_member_manifest_sha256": member_manifest["member_manifest_sha256"],
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
        "status": "passed",
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
