"""Signed install receipt: which files the installer wrote and may later remove."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from scope_recall.adapters.installation_receipt import (
    RECEIPT_SCHEMA as RECEIPT_SCHEMA,
)
from scope_recall.adapters.installation_receipt import (
    read_receipt,
)
from scope_recall.adapters.installation_receipt import (
    receipt_digest as _receipt_digest,
)

from .backup import atomic_write, sha256
from .install_common import (
    PACKAGE_VERSION,
    RECEIPT_FILENAME,
    HostChoice,
    InstallError,
    InstallPlan,
    json_dump,
    normalized_path,
    reject_symlink_chain,
    validate_host,
    within,
)


def receipt_file(instance_root: Path) -> Path:
    return instance_root / RECEIPT_FILENAME


def load_receipt(instance_root: Path) -> dict[str, Any] | None:
    return read_receipt(instance_root, error=InstallError)


def _owned_files_from_receipt(
    receipt: dict[str, Any],
    *,
    plugin_dir: Path,
    instance_root: Path,
) -> dict[str, str]:
    """Map each receipt path to its recorded digest, refusing paths outside the install roots."""
    roots = {
        "plugin": (normalized_path(plugin_dir), "receipt plugin path outside target_plugin_dir"),
        "instance": (normalized_path(instance_root), "receipt instance path outside instance_root"),
    }
    files = receipt.get("files")
    if not isinstance(files, list):
        raise InstallError("receipt files invalid")
    owned: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise InstallError("receipt files invalid")
        sha = item.get("sha256")
        if type(sha) is not str or len(sha) != 64:
            raise InstallError("receipt file hash invalid")
        path_text = item.get("path")
        if type(path_text) is not str or not path_text:
            raise InstallError("receipt file path invalid")
        path = Path(path_text)
        if not path.is_absolute():
            raise InstallError("receipt file path must be absolute")
        reject_symlink_chain(path)
        norm = normalized_path(path)
        role = item.get("role")
        if type(role) is not str or role not in roots:
            raise InstallError("receipt file role invalid")
        root_norm, message = roots[role]
        if not within(norm, root_norm):
            raise InstallError(message)
        if norm in owned and owned[norm] != sha:
            raise InstallError("receipt duplicate path")
        owned[norm] = sha
    return owned


def validate_receipt_binding(
    receipt: dict[str, Any],
    *,
    host: HostChoice,
    instance_root: Path,
    target_plugin_dir: Path,
) -> dict[str, str]:
    if validate_host(str(receipt.get("host") or "")) != host:
        raise InstallError("existing receipt host mismatch")
    if normalized_path(instance_root) != normalized_path(Path(str(receipt.get("instance_root") or ""))):
        raise InstallError("existing receipt instance_root mismatch")
    if normalized_path(target_plugin_dir) != normalized_path(Path(str(receipt.get("target_plugin_dir") or ""))):
        raise InstallError("existing receipt target_plugin_dir mismatch")
    if not str(receipt.get("installation_id") or "").strip():
        raise InstallError("existing receipt installation_id missing")
    return _owned_files_from_receipt(receipt, plugin_dir=target_plugin_dir, instance_root=instance_root)


def write_receipt(
    plan: InstallPlan,
    *,
    installation_id: str,
    written: Iterable[str],
    tracked: Iterable[Path],
    kept: Mapping[str, str] | None = None,
) -> Path:
    """Record every written wrapper plus the adapter-owned files an uninstall must recognize.  A skill file the
    install kept as an agent edited it (``kept``) is recorded with the package's digest, so that the next install
    still finds it edited and compares it with the package again, instead of writing over the edit."""
    instance_norm = normalized_path(plan.instance_root)
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in [Path(text) for text in written]:
        norm = normalized_path(path)
        role = "instance" if norm.startswith(instance_norm + os.sep) else "plugin"
        files.append({"path": norm, "sha256": sha256(path), "role": role})
        seen.add(norm)
    for norm, digest in sorted((kept or {}).items()):
        if norm not in seen:
            files.append(
                {
                    "path": norm,
                    "sha256": digest,
                    "role": "instance" if norm.startswith(instance_norm + os.sep) else "plugin",
                }
            )
            seen.add(norm)
    for path in tracked:
        norm = normalized_path(path)
        if path.is_file() and norm not in seen:
            files.append({"path": norm, "sha256": sha256(path), "role": "instance"})
            seen.add(norm)

    body: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "package_version": PACKAGE_VERSION,
        "host": plan.host,
        "installation_id": installation_id,
        "agent_id": plan.agent_id,
        "target_plugin_dir": normalized_path(plan.target_plugin_dir),
        "instance_root": normalized_path(plan.instance_root),
        "project_root": normalized_path(plan.project_root) if plan.project_root is not None else None,
        "python_executable": normalized_path(plan.python_executable),
        "files": files,
    }
    if plan.agent_workspace:
        body["agent_workspace"] = plan.agent_workspace
    if plan.env_file is not None:
        body["env_file"] = normalized_path(plan.env_file)
    body["receipt_sha256"] = _receipt_digest(body)
    path = receipt_file(plan.instance_root)
    atomic_write(path, json_dump(body))
    return path


def setup_receipts_directory(home: Path) -> Path:
    """The setup journal lives beside the entry pointer for local and shared installations alike."""
    return home / "scope-recall" / "setup-receipts"
