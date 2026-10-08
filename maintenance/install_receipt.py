"""Signed install receipt: which files the installer wrote and may later remove."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

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

RECEIPT_SCHEMA = "scope-recall.install-receipt.v1"


def receipt_file(instance_root: Path) -> Path:
    return instance_root / RECEIPT_FILENAME


def _receipt_digest(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verify_receipt_digest(payload: dict[str, Any]) -> None:
    digest = payload.get("receipt_sha256")
    if type(digest) is not str or not digest:
        raise InstallError("receipt digest missing")
    if _receipt_digest(payload) != digest:
        raise InstallError("receipt digest mismatch")


def load_receipt(instance_root: Path) -> dict[str, Any] | None:
    path = receipt_file(instance_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError("existing install receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise InstallError("existing install receipt is invalid")
    if payload.get("schema_version") != RECEIPT_SCHEMA:
        raise InstallError("existing install receipt schema mismatch")
    _verify_receipt_digest(payload)
    return payload


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
