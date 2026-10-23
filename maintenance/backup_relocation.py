"""Place a recovered store in the layout its normal host loader expects."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..adapters.hermes.installation import load_installation_manifest
from ..adapters.installation_identity import path_identity
from ..core.installation_copy import installation_copy_id
from ..runtime.backup_archive import ArchiveMember
from ..runtime.backup_primitives import BackupError
from ..runtime.backup_resources import restored_resource_path
from .install_hermes import bound_workspace


@dataclass(frozen=True)
class RestoreLayout:
    """The fresh root and its actual store; a local root is a new host home."""

    root: Path
    data: Path
    host: Literal["hermes", "codex"] | None

    def members(self, members: tuple[ArchiveMember, ...]) -> tuple[ArchiveMember, ...]:
        """Keep every extracted file inside the requested fresh root."""
        prefix = self.data.relative_to(self.root).as_posix()
        return tuple(
            ArchiveMember(
                item.archive_name,
                item.destination_relative
                if self.host is None or item.archive_name == "configs/codex-installation.json"
                else f"{prefix}/{item.destination_relative}",
            )
            for item in members
        )


def restore_layout(root: Path, manifest: dict) -> RestoreLayout:
    """Choose a supported layout from the archive's installation identity."""
    if manifest.get("installation_kind") == "shared":
        return RestoreLayout(root, root, None)
    installation = manifest.get("installation_id", "")
    if isinstance(installation, str) and installation.startswith("hermes-install:"):
        return RestoreLayout(root, root / "scope-recall", "hermes")
    if isinstance(installation, str) and installation.startswith("codex-install:"):
        return RestoreLayout(root, root / "data", "codex")
    raise BackupError("bundle does not identify a supported installation layout")


def relocate_installation(layout: RestoreLayout) -> None:
    """Reanchor only the new copy; its stable SQLite identity is preserved."""
    path = layout.root / "codex-installation.json" if layout.host == "codex" else layout.data / "installation.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    installation_copy_id(layout.data, payload["installation_id"])
    if type(payload) is not dict:
        raise BackupError("installation configuration must be an object")
    payload["data_directory"] = str(layout.data)
    if layout.host is not None:
        anchor = path if layout.host == "codex" else layout.root
        payload["location_id"] = path_identity(layout.host, anchor)
        if layout.host == "hermes":
            payload["hermes_home"] = str(layout.root)
    else:
        for entry in payload["entries"]:
            # Restored grants stay dormant until the owner explicitly attaches a home.
            entry["home"] = str(layout.data / "entries" / entry["entry_id"] / "home")
            if not entry.get("detached_at"):
                entry["recovery_pending"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reregistration_arguments(layout: RestoreLayout, entries: list[dict]) -> list[list[str]]:
    """Return real CLI arguments, leaving host-specific installation choices explicit."""
    if layout.host is not None:
        path = layout.root / "codex-installation.json" if layout.host == "codex" else layout.data / "installation.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        common = [
            "--host",
            layout.host,
            "--instance-root",
            str(layout.root),
            "--agent-id",
            config["agent_id"],
            "--python",
            "<host-python>",
            "--target-plugin-dir",
            "<plugin-directory>",
        ]
        if config.get("test_mode"):
            common.append("--test-mode")
        if layout.host == "codex":
            common.extend(["--project-root", "<project-directory>"])
        else:
            common.extend(["--agent-workspace", bound_workspace(load_installation_manifest(layout.root))])
        return [["scope-recall", command, *common] for command in ("plan-install", "apply-install")]
    commands = []
    for entry in entries:
        if entry.get("detached_at"):
            continue
        command = [
            "scope-recall",
            "attach",
            "--root",
            str(layout.data),
            "--host",
            entry["host"],
            "--entry",
            entry["entry_id"],
            "--display-name",
            entry["display_name"],
            "--instance-root",
            f"<new-home-for-{entry['entry_id']}>",
        ]
        config = layout.data / "entries" / entry["entry_id"] / "runtime-config.json"
        if config.is_file():
            command.extend(["--runtime-config-from", str(config)])
        commands.append(command)
    return commands


def relocate_remote_clients(data: Path, resources: list[dict]) -> list[dict]:
    """Repoint spools into the recovered store and return missing credential references."""
    references = []
    for resource in resources:
        if resource.get("role") != "remote_client":
            continue
        config_path = restored_resource_path(data, resource["config_file"])
        client = json.loads(config_path.read_text(encoding="utf-8"))
        name = Path(client["token_file"]).name
        client["state_dir"] = str(config_path.parent)
        client["token_file"] = str(config_path.parent / "credentials" / name)
        config_path.write_text(json.dumps(client, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        references.append(
            {"client_config": str(config_path), "credential_reference": name, "required_file": client["token_file"]}
        )
    return references


def command_text(arguments: list[str]) -> str:
    """Quote for PowerShell on Windows or a POSIX shell elsewhere; never execute."""
    if os.name == "nt":
        return arguments[0] + " " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments[1:])
    return shlex.join(arguments)
