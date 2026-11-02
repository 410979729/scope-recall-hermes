"""Content-free replay checkpoints and immutable local capture authorization."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.backup_primitives import atomic_json
from scope_recall.core.file_lock import advisory_file_lock

from ..installation_receipt import RECEIPT_FILENAME, read_receipt

if TYPE_CHECKING:
    from .config import CodexInstallationConfig


def checkpoint(root: Path, name: str) -> dict:
    """Read bounded metadata; a malformed checkpoint never supplies authorization."""
    try:
        with (root / "capture-ledgers" / name).open("r", encoding="utf-8") as stream:
            value = json.loads(stream.read(8193))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_checkpoint(root: Path, name: str, value: dict) -> None:
    """Atomically publish metadata beside the library's existing ledger locators."""
    atomic_json(root / "capture-ledgers" / name, value)


def record_authorization(
    config: CodexInstallationConfig, since: str, *, basis: str = "installation", fresh: bool = False
) -> str:
    """Persist an authorization start; only explicit creation of a fresh store begins a new lifetime."""
    with advisory_file_lock(config.data_directory / "capture-ledgers" / "scan.lock", timeout_seconds=5):
        saved = checkpoint(config.data_directory, "authorized-at.json")
        if not fresh and saved.get("installation_id") == config.installation_id and saved.get("since") is not None:
            return _authorization_time(saved["since"])
        value = _authorization_time(since)
        save_checkpoint(
            config.data_directory,
            "authorized-at.json",
            dict(installation_id=config.installation_id, since=value, basis=basis),
        )
        if fresh:
            save_checkpoint(
                config.data_directory, "scan.json", dict(installation_id=config.installation_id, after=None)
            )
        return value


def _authorization_time(value: object) -> str:
    if type(value) is not str:
        raise ValueError("authorization_time_requires_text")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("authorization_time_requires_timezone")
    return stamp.astimezone(UTC).isoformat()


def local_authorized_at(config: CodexInstallationConfig, *, seconds: float) -> str | None:
    """Migrate from a bound install receipt, else the entry's first server-recorded capture; never infer from config."""
    saved = checkpoint(config.data_directory, "authorized-at.json")
    if saved.get("installation_id") == config.installation_id and saved.get("since"):
        return record_authorization(config, saved["since"])
    root = config.config_path.parent
    receipt = read_receipt(root)
    if (
        receipt is not None
        and receipt.get("installation_id") == config.installation_id
        and receipt.get("host") == "codex"
        and type(receipt.get("instance_root")) is str
        and Path(receipt["instance_root"]).is_absolute()
        and os.path.normcase(str(Path(receipt["instance_root"]).resolve())) == os.path.normcase(str(root.resolve()))
    ):
        since = datetime.fromtimestamp((root / RECEIPT_FILENAME).stat().st_mtime, UTC).isoformat()
        return record_authorization(config, since)
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding))
    context = TrustedContext(binding, "capture-authorization", binding.scope_ids, "host_generated")
    with core.storage.read(context, remaining_seconds=seconds) as tx:
        row = tx._check().execute("SELECT min(recorded_at) FROM source_events WHERE entry_id='local'").fetchone()
    if row and row[0]:
        return record_authorization(config, row[0], basis="first_capture")
    return None
