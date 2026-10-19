"""Explicit local resources outside a store: entry configs and client spools.

Only registered entry homes and client config paths supplied by the operator
are read. Credential files referenced by a client are never opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backup_primitives import BackupError, safe_path
from .backup_resources import stage_instance_resources

ConfigStager = Callable[[Path, Path, str, list[dict[str, Any]]], int]
TreeStager = Callable[[Path, Path, str, list[dict[str, Any]]], None]


def configured_client_paths(data: Path) -> tuple[Path, ...]:
    """Explicit client configs included by scheduled backups, never discovered from user homes."""
    path = data / "backup-config.json"
    if not path.is_file():
        return ()
    payload = json.loads(safe_path(path, must_exist=True).read_text(encoding="utf-8"))
    configs = payload.get("client_configs", [])
    if type(configs) is not list or any(type(value) is not str or not Path(value).is_absolute() for value in configs):
        raise BackupError("backup client_configs must list absolute config paths")
    return tuple(Path(value) for value in configs)


def stage_entry_resources(
    entries: list[dict[str, Any]],
    stage: Path,
    files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    stage_config: ConfigStager,
) -> int:
    """Pack each registered home's config and independent spend snapshot."""
    redacted = 0
    for entry in entries:
        if not entry.get("home") or entry.get("detached_at"):
            continue
        source = safe_path(Path(entry["home"]) / "scope-recall", must_exist=True)
        prefix = f"entries/{entry['entry_id']}"
        target = stage / prefix
        for name in ("attachment.json", "runtime-config.json", "remote-server.json"):
            config = source / name
            if config.is_file():
                redacted += stage_config(config, stage, f"{prefix}/{name}", files)
        extra_files, extra_resources = stage_instance_resources(source, target)
        files.extend({**item, "path": f"{prefix}/{item['path']}"} for item in extra_files)
        resources.extend(
            {
                **item,
                "archive_path": f"{prefix}/{item['archive_path']}",
                "target_relative": f"{prefix}/{item['target_relative']}",
                "config_file": f"{prefix}/{item['config_file']}" if item["config_file"] else None,
            }
            for item in extra_resources
        )
    return redacted


def stage_remote_clients(
    configs: tuple[Path, ...],
    stage: Path,
    files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    stage_config: ConfigStager,
    stage_tree: TreeStager,
) -> int:
    """Pack a client's configured spool while retaining only its credential reference."""
    redacted = 0
    for index, path in enumerate(configs, 1):
        source = safe_path(path, must_exist=True)
        client = json.loads(source.read_text(encoding="utf-8"))
        if (
            type(client) is not dict
            or type(client.get("state_dir")) is not str
            or not Path(client["state_dir"]).is_absolute()
        ):
            raise BackupError("client config must name an absolute state_dir")
        if type(client.get("token_file")) is not str or not Path(client["token_file"]).is_absolute():
            raise BackupError("client config must name an absolute credential reference")
        prefix = f"clients/client{index}"
        config_path = f"{prefix}/client.json"
        redacted += stage_config(source, stage, config_path, files)
        spool = safe_path(Path(client["state_dir"]) / "spool")
        stage_tree(stage, spool, f"{prefix}/spool", files)
        resources.append(
            {
                "role": "remote_client",
                "archive_path": config_path,
                "target_relative": prefix,
                "config_file": config_path,
                "field": "state_dir",
            }
        )
    return redacted
