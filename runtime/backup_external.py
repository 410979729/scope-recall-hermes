"""Explicit local resources outside a store: entry configs and client spools.

Only registered entry homes and client config paths supplied by the operator
are read. Credential files referenced by a client are never opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.capture_filters import sanitize_source_capture_text
from ..core.events import capture_contains_secret
from .backup_primitives import BackupError, check_backup_deadline, safe_path, sha256
from .backup_resources import BackupPrivacy, budget_ledger_source, real_files, stage_instance_resources

ConfigStager = Callable[[Path, Path, str, list[dict[str, Any]]], int]


def _entry_source(data: Path, entry: dict) -> Path | None:
    """Dormant recovered entries own packed resources rather than a live host home."""
    if not entry.get("home") or entry.get("detached_at"):
        return None
    if entry.get("recovery_pending"):
        source = data / "entries" / entry["entry_id"]
        return source if source.is_dir() else None
    return Path(entry["home"]) / "scope-recall"


def reject_credential_overlap(
    data: Path, entries: list[dict], configs: tuple[Path, ...], *, with_vectors: bool
) -> None:
    """Resolve references before staging and refuse path and hard-link aliases.

    Every copied tree is checked against every client's reference, including a
    different client's spool. Credential bodies are never opened.
    """
    credentials = []
    roots = [data / "retained"]
    directories = [data] + [source for entry in entries if (source := _entry_source(data, entry)) is not None]
    if with_vectors:
        roots.append(data / "vectors")
    for path in configs:
        client = json.loads(safe_path(path, must_exist=True).read_text(encoding="utf-8"))
        if type(client) is not dict:
            raise BackupError("client configuration must be an object")
        for field in ("state_dir", "token_file"):
            if type(client.get(field)) is not str or not Path(client[field]).is_absolute():
                raise BackupError(f"client {field} must be an absolute credential or state reference")
        credentials.append(Path(client["token_file"]).resolve())
        roots.append(Path(client["state_dir"]) / "spool")
    roots.extend(directory / "kept-captures" for directory in directories)
    copied = [path for root in roots if root.is_dir() for path in real_files(safe_path(root, must_exist=True))]
    copied.extend(path for directory in directories if (path := budget_ledger_source(directory)[0]) is not None)
    copied.extend([*configs, data / "memory.sqlite3"])
    codex_config = data.parent / "codex-installation.json"
    if codex_config.is_file():
        copied.append(codex_config)
    copied.extend(path for directory in directories for path in directory.glob("*.json") if path.is_file())
    for credential in credentials:
        if any(credential == path.resolve() or (credential.exists() and credential.samefile(path)) for path in copied):
            raise BackupError("referenced credential overlaps a copied backup resource")


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
    data: Path,
    entries: list[dict[str, Any]],
    stage: Path,
    files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    stage_config: ConfigStager,
    privacy: BackupPrivacy | None = None,
) -> int:
    """Pack each registered home's config and independent spend snapshot."""
    redacted = 0
    for entry in entries:
        source = _entry_source(data, entry)
        if source is None:
            continue
        source = safe_path(source, must_exist=True)
        prefix = f"entries/{entry['entry_id']}"
        target = stage / prefix
        for name in ("attachment.json", "runtime-config.json", "remote-server.json"):
            config = source / name
            if config.is_file():
                redacted += stage_config(config, stage, f"{prefix}/{name}", files)
        extra_files, extra_resources = stage_instance_resources(source, target, privacy=privacy)
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
    privacy: BackupPrivacy | None = None,
) -> tuple[int, int, int]:
    """Pack a client's configured spool while retaining only its credential reference."""
    redacted = 0
    spool_redacted = spool_omitted = 0
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
        cleaned, omitted = _stage_spool(spool, stage, f"{prefix}/spool", files, privacy=privacy)
        spool_redacted += cleaned
        spool_omitted += omitted
        resources.append(
            {
                "role": "remote_client",
                "archive_path": config_path,
                "target_relative": prefix,
                "config_file": config_path,
                "field": "state_dir",
            }
        )
    return redacted, spool_redacted, spool_omitted


def _capture_form(value: Any) -> Any:
    """Remove transport bodies with admission's filter while preserving the wire envelope."""
    if isinstance(value, str):
        return sanitize_source_capture_text(value)
    if isinstance(value, dict):
        return {key: _capture_form(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_capture_form(item) for item in value]
    return value


def _stage_spool(
    source: Path, stage: Path, prefix: str, files: list[dict[str, Any]], *, privacy: BackupPrivacy | None = None
) -> tuple[int, int]:
    """Omit unreadable or refused entries individually; never alter the client's originals."""
    redacted = omitted = 0
    if not source.is_dir():
        return redacted, omitted
    for item in real_files(source):
        check_backup_deadline()
        try:
            raw = item.read_bytes()
            value = json.loads(raw)
            if type(value) is not dict or capture_contains_secret(value) or capture_contains_secret(json.dumps(value)):
                omitted += 1
                continue
            if privacy is not None and not privacy.permits_remote(value):
                omitted += 1
                continue
            cleaned = _capture_form(value)
            body = raw if cleaned == value else (json.dumps(cleaned, ensure_ascii=False) + "\n").encode("utf-8")
        except (OSError, ValueError, UnicodeError, RecursionError):
            omitted += 1
            continue
        redacted += cleaned != value
        relative = f"{prefix}/{item.relative_to(source).as_posix()}"
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        files.append({"path": relative, "sha256": sha256(target), "size_bytes": len(body)})
    return redacted, omitted
