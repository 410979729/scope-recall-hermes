#!/usr/bin/env python3
"""Validate and stage immutable GitHub Release assets for PyPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def _expected_package_names(version: str) -> tuple[str, str]:
    return (
        f"hermes_scope_recall-{version}-py3-none-any.whl",
        f"hermes_scope_recall-{version}.tar.gz",
    )


def _read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or len(fields[0]) != 64:
            raise ValueError("SHA256SUMS contains a malformed entry")
        digest, name = fields
        normalized = Path(name).name
        if normalized != name or normalized in checksums:
            raise ValueError("SHA256SUMS contains an unsafe or duplicate name")
        checksums[normalized] = digest.lower()
    return checksums


def _require_empty_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError(f"staging directory must be empty: {path.name}")


def stage_release_assets(
    source_dir: Path,
    *,
    packages_dir: Path,
    metadata_dir: Path,
    version: str,
) -> dict[str, Any]:
    """Stage exactly one wheel and one sdist, with metadata outside packages."""

    source = Path(source_dir)
    sums_path = source / "SHA256SUMS"
    provenance_path = source / "RELEASE-PROVENANCE.json"
    if not sums_path.is_file():
        raise ValueError("release assets are missing SHA256SUMS")
    if not provenance_path.is_file():
        raise ValueError("release assets are missing RELEASE-PROVENANCE.json")
    expected_names = _expected_package_names(version)
    package_paths = [source / name for name in expected_names]
    if any(not path.is_file() for path in package_paths):
        raise ValueError("release assets do not contain the expected wheel and sdist")

    distributions = sorted(
        path.name
        for path in source.iterdir()
        if path.is_file()
        and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    )
    if distributions != sorted(expected_names):
        raise ValueError("release assets contain unexpected distributions")

    checksums = _read_checksums(sums_path)
    if sorted(checksums) != sorted(expected_names):
        raise ValueError("SHA256SUMS must list exactly the expected distributions")
    for path in package_paths:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if checksums[path.name] != actual:
            raise ValueError(f"SHA-256 mismatch for {path.name}")

    _require_empty_directory(packages_dir)
    _require_empty_directory(metadata_dir)
    for path in package_paths:
        shutil.copy2(path, packages_dir / path.name)
    shutil.copy2(sums_path, metadata_dir / sums_path.name)
    shutil.copy2(provenance_path, metadata_dir / provenance_path.name)
    return {
        "ok": True,
        "packages": sorted(expected_names),
        "metadata": sorted((sums_path.name, provenance_path.name)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--packages-dir", type=Path, required=True)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    receipt = stage_release_assets(
        args.source_dir,
        packages_dir=args.packages_dir,
        metadata_dir=args.metadata_dir,
        version=str(args.version),
    )
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
