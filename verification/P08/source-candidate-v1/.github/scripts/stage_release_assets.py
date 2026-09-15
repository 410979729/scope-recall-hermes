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


def _expected_stable_update_names(version: str) -> tuple[str, str]:
    return (
        f"scope-recall-hermes-{version}.tar.gz",
        "scope-recall-stable-update.json",
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
        try:
            bytes.fromhex(digest)
        except ValueError as exc:
            raise ValueError("SHA256SUMS contains a malformed entry") from exc
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
    """Verify the exact Release inventory and stage only PyPI distributions."""

    source = Path(source_dir)
    sums_path = source / "SHA256SUMS"
    provenance_path = source / "RELEASE-PROVENANCE.json"
    if not sums_path.is_file():
        raise ValueError("release assets are missing SHA256SUMS")
    if not provenance_path.is_file():
        raise ValueError("release assets are missing RELEASE-PROVENANCE.json")
    package_names = _expected_package_names(version)
    stable_update_names = _expected_stable_update_names(version)
    hashed_names = (*package_names, *stable_update_names)
    package_paths = [source / name for name in package_names]
    stable_update_paths = [source / name for name in stable_update_names]
    if any(not path.is_file() for path in package_paths):
        raise ValueError("release assets do not contain the expected wheel and sdist")
    if any(not path.is_file() for path in stable_update_paths):
        raise ValueError("release assets do not contain the stable-update assets")

    expected_inventory = {
        *hashed_names,
        sums_path.name,
        provenance_path.name,
    }
    actual_inventory = {path.name for path in source.iterdir()}
    if actual_inventory != expected_inventory or any(
        path.is_symlink() or not path.is_file() for path in source.iterdir()
    ):
        raise ValueError("release assets contain an unexpected asset")

    checksums = _read_checksums(sums_path)
    if set(checksums) != set(hashed_names):
        raise ValueError("SHA256SUMS must list exactly the expected hashed assets")
    for path in (*package_paths, *stable_update_paths):
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
        "packages": sorted(package_names),
        "stable_update_assets": sorted(stable_update_names),
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
