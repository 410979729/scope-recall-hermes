"""Signed install receipt: which files the installer wrote and may later remove."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .backup import _atomic_write, _sha256
from .install_common import (
    PACKAGE_VERSION,
    RECEIPT_FILENAME,
    HostChoice,
    InstallError,
    InstallPlan,
    _json_dump,
    _norm,
    _reject_symlink_chain,
    _validate_host,
    _within,
)

RECEIPT_SCHEMA = "scope-recall.install-receipt.v1"


def _receipt_path(instance_root: Path) -> Path:
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


def _load_receipt(instance_root: Path) -> dict[str, Any] | None:
    path = _receipt_path(instance_root)
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
        "plugin": (_norm(plugin_dir), "receipt plugin path outside target_plugin_dir"),
        "instance": (_norm(instance_root), "receipt instance path outside instance_root"),
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
        _reject_symlink_chain(path)
        norm = _norm(path)
        role = item.get("role")
        if type(role) is not str or role not in roots:
            raise InstallError("receipt file role invalid")
        root_norm, message = roots[role]
        if not _within(norm, root_norm):
            raise InstallError(message)
        if norm in owned and owned[norm] != sha:
            raise InstallError("receipt duplicate path")
        owned[norm] = sha
    return owned


def _validate_receipt_binding(
    receipt: dict[str, Any],
    *,
    host: HostChoice,
    instance_root: Path,
    target_plugin_dir: Path,
) -> dict[str, str]:
    if _validate_host(str(receipt.get("host") or "")) != host:
        raise InstallError("existing receipt host mismatch")
    if _norm(instance_root) != _norm(Path(str(receipt.get("instance_root") or ""))):
        raise InstallError("existing receipt instance_root mismatch")
    if _norm(target_plugin_dir) != _norm(Path(str(receipt.get("target_plugin_dir") or ""))):
        raise InstallError("existing receipt target_plugin_dir mismatch")
    if not str(receipt.get("installation_id") or "").strip():
        raise InstallError("existing receipt installation_id missing")
    return _owned_files_from_receipt(receipt, plugin_dir=target_plugin_dir, instance_root=instance_root)


def _write_receipt(
    plan: InstallPlan,
    *,
    installation_id: str,
    written: Iterable[str],
    tracked: Iterable[Path],
) -> Path:
    """Record every written wrapper plus the adapter-owned files an uninstall must recognize."""
    instance_norm = _norm(plan.instance_root)
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in [Path(text) for text in written]:
        norm = _norm(path)
        role = "instance" if norm.startswith(instance_norm + os.sep) else "plugin"
        files.append({"path": norm, "sha256": _sha256(path), "role": role})
        seen.add(norm)
    for path in tracked:
        norm = _norm(path)
        if path.is_file() and norm not in seen:
            files.append({"path": norm, "sha256": _sha256(path), "role": "instance"})
            seen.add(norm)

    body: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "package_version": PACKAGE_VERSION,
        "host": plan.host,
        "installation_id": installation_id,
        "agent_id": plan.agent_id,
        "target_plugin_dir": _norm(plan.target_plugin_dir),
        "instance_root": _norm(plan.instance_root),
        "project_root": _norm(plan.project_root) if plan.project_root is not None else None,
        "python_executable": _norm(plan.python_executable),
        "files": files,
    }
    if plan.agent_workspace:
        body["agent_workspace"] = plan.agent_workspace
    if plan.env_file is not None:
        body["env_file"] = _norm(plan.env_file)
    body["receipt_sha256"] = _receipt_digest(body)
    path = _receipt_path(plan.instance_root)
    _atomic_write(path, _json_dump(body))
    return path
