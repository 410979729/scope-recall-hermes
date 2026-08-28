#!/usr/bin/env python3
"""Create a read-only, content-free receipt for accidental HOME residue."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
from typing import Sequence


SCHEMA_VERSION = "scope-recall.accidental-home-cleanup-receipt.v1"
DEFAULT_OUTPUT = Path(".execution/ACCIDENTAL_HOME_CLEANUP_RECEIPT.json")
_CONFIG_NAMES = {"config.json", "config.yaml", "config.yml", "plugin.yaml"}
_PLUGIN_NAMES = {"__init__.py", "provider.py", "plugin.yaml"}


class HomeCleanupReceiptError(RuntimeError):
    """Raised when a read-only residue boundary cannot be proven safely."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def inventory_path(path: Path) -> dict[str, object]:
    """Inventory path metadata and hashes without returning file contents or roots."""

    requested = Path(path)
    if not requested.exists() and not requested.is_symlink():
        entries: list[dict[str, object]] = []
        return {
            "state": "missing",
            "exists": False,
            "empty": False,
            "file_count": 0,
            "directory_count": 0,
            "inventory_sha256": hashlib.sha256(_canonical_bytes(entries)).hexdigest(),
            "residual_classes": {
                "pycache": 0,
                "config": 0,
                "plugin_files": 0,
                "other": 0,
            },
            "entry_names_included": False,
        }
    root = requested.resolve(strict=False)
    if root.is_file() or root.is_symlink():
        raise HomeCleanupReceiptError("inventory root must be a real directory")
    entries = []
    directory_count = 0
    residual = {"pycache": 0, "config": 0, "plugin_files": 0, "other": 0}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            directory = current_path / directory_name
            relative = directory.relative_to(root).as_posix()
            if directory.is_symlink():
                target = os.readlink(directory).encode("utf-8", errors="surrogatepass")
                entries.append(
                    {
                        "path": relative,
                        "kind": "symlink",
                        "sha256": hashlib.sha256(target).hexdigest(),
                        "size_bytes": len(target),
                    }
                )
            else:
                directory_count += 1
        for file_name in file_names:
            file_path = current_path / file_name
            relative = file_path.relative_to(root).as_posix()
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts:
                raise HomeCleanupReceiptError("unsafe relative inventory path")
            if file_path.is_symlink():
                target = os.readlink(file_path).encode("utf-8", errors="surrogatepass")
                digest = hashlib.sha256(target).hexdigest()
                size = len(target)
                kind = "symlink"
            else:
                digest = _sha256_file(file_path)
                size = file_path.stat().st_size
                kind = "file"
            entries.append(
                {
                    "path": relative,
                    "kind": kind,
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
            lowered_parts = {part.casefold() for part in pure.parts}
            if "__pycache__" in lowered_parts or pure.suffix.casefold() == ".pyc":
                residual["pycache"] += 1
            elif pure.name.casefold() in _CONFIG_NAMES:
                residual["config"] += 1
            elif pure.name.casefold() in _PLUGIN_NAMES:
                residual["plugin_files"] += 1
            else:
                residual["other"] += 1
    entries.sort(key=lambda item: str(item["path"]))
    return {
        "state": "present",
        "exists": True,
        "empty": not entries and directory_count == 0,
        "file_count": sum(item["kind"] in {"file", "symlink"} for item in entries),
        "directory_count": directory_count,
        "inventory_sha256": hashlib.sha256(_canonical_bytes(entries)).hexdigest(),
        "residual_classes": residual,
        "entry_names_included": False,
    }


def build_cleanup_receipt(
    *,
    accidental_path: Path,
    active_plugin_path: Path,
    quarantine_path: Path,
) -> dict[str, object]:
    accidental = Path(accidental_path).resolve(strict=False)
    active = Path(active_plugin_path).resolve(strict=False)
    quarantine = Path(quarantine_path).resolve(strict=False)
    if _overlap(active, quarantine):
        raise HomeCleanupReceiptError(
            "active plugin and quarantine boundaries overlap"
        )
    if _overlap(active, accidental):
        raise HomeCleanupReceiptError(
            "active plugin and accidental HOME residue boundaries overlap"
        )
    started = datetime.now(timezone.utc)
    accidental_inventory = inventory_path(accidental)
    quarantine_inventory = inventory_path(quarantine)
    finished = datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "read_only_inventory",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "active_instance_touched": False,
        "deletion_performed": False,
        "paths": {
            "accidental_home_residue": "redacted-local-path",
            "active_plugin": "redacted-local-path",
            "quarantine": "redacted-local-path",
        },
        "boundaries": {
            "active_quarantine_overlap": False,
            "active_accidental_overlap": False,
        },
        "accidental_home_residue": accidental_inventory,
        "quarantine": quarantine_inventory,
    }


def _write_ignored(root: Path, output: Path, payload: dict[str, object]) -> None:
    resolved = output if output.is_absolute() else root / output
    try:
        relative = resolved.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError:
        relative = None
    if relative is not None:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "check-ignore",
                "--quiet",
                "--",
                relative.as_posix(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
        if result.returncode != 0:
            raise HomeCleanupReceiptError(
                "refusing to write cleanup receipt to an unignored repository path"
            )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=resolved.parent,
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, resolved)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--accidental-path", type=Path, required=True)
    parser.add_argument("--active-plugin-path", type=Path, required=True)
    parser.add_argument("--quarantine-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve(strict=True)
    payload = build_cleanup_receipt(
        accidental_path=args.accidental_path,
        active_plugin_path=args.active_plugin_path,
        quarantine_path=args.quarantine_path,
    )
    _write_ignored(root, args.output, payload)
    accidental_receipt = payload.get("accidental_home_residue")
    quarantine_receipt = payload.get("quarantine")
    if not isinstance(accidental_receipt, dict) or not isinstance(
        quarantine_receipt, dict
    ):
        raise HomeCleanupReceiptError("cleanup inventory payload is invalid")
    print(
        json.dumps(
            {
                "ok": True,
                "deletion_performed": False,
                "accidental_state": accidental_receipt["state"],
                "quarantine_inventory_sha256": quarantine_receipt[
                    "inventory_sha256"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HomeCleanupReceiptError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
