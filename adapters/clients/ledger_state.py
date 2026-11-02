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


class AuthorizationStateError(ValueError):
    """A bound authorization time cannot safely grant automatic replay."""


def database_lifetime(binding, *, seconds: float = 5) -> tuple[str, str | None]:
    """Read the database's identity through the verified storage boundary, independent of its filename."""
    core = MemoryCore(CoreConfig(binding))
    context = TrustedContext(binding, "capture-authorization", binding.scope_ids, "host_generated")
    with core.storage.read(context, remaining_seconds=seconds) as tx:
        return tx.database_identity()


def record_authorization(
    config: CodexInstallationConfig, since: str, *, basis: str = "installation", fresh: bool = False
) -> str:
    """Bind authorization to one database lifetime; explicit grants may restart future capture."""
    identity, born = database_lifetime(config.to_binding())
    with advisory_file_lock(config.data_directory / "capture-ledgers" / "scan.lock", timeout_seconds=5):
        saved = checkpoint(config.data_directory, "authorized-at.json")
        belongs = saved.get("installation_id") == config.installation_id
        same = saved.get("database_id") == identity or (saved.get("database_id") is None and born is None)
        if not fresh and belongs and same:
            since = _authorization_time(saved.get("since"))
            basis = saved.get("basis", basis)
        value = _authorization_time(since)
        if born is not None:
            value = max(value, _authorization_time(born))
        save_checkpoint(
            config.data_directory,
            "authorized-at.json",
            dict(installation_id=config.installation_id, database_id=identity, since=value, basis=basis),
        )
        if fresh or saved.get("database_id") != identity:
            save_checkpoint(
                config.data_directory,
                "scan.json",
                dict(installation_id=config.installation_id, database_id=identity, after=None, revisit=False),
            )
        return value


def _authorization_time(value: object) -> str:
    if type(value) is not str:
        raise AuthorizationStateError("authorization_time_requires_text")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationStateError("authorization_time_invalid") from exc
    if stamp.tzinfo is None:
        raise AuthorizationStateError("authorization_time_requires_timezone")
    return stamp.astimezone(UTC).isoformat()


def authorization_problem(config: CodexInstallationConfig) -> str | None:
    """Inspect a current lifetime's time without migrating or repairing any state."""
    saved = checkpoint(config.data_directory, "authorized-at.json")
    identity, born = database_lifetime(config.to_binding())
    if saved.get("installation_id") != config.installation_id:
        return None
    if saved.get("database_id") not in (None, identity):
        return None
    try:
        _authorization_time(saved.get("since"))
    except AuthorizationStateError as exc:
        return str(exc)
    return None


def local_authorized_at(config: CodexInstallationConfig, *, seconds: float) -> str | None:
    """Preserve reliable grant evidence within a database lifetime; a rebuilt database starts afresh."""
    identity, born = database_lifetime(config.to_binding(), seconds=seconds)
    saved = checkpoint(config.data_directory, "authorized-at.json")
    if saved.get("installation_id") == config.installation_id:
        if saved.get("database_id") in (None, identity):
            _authorization_time(saved.get("since"))
        same = saved.get("database_id") == identity or (saved.get("database_id") is None and born is None)
        if same:
            return record_authorization(config, _authorization_time(saved.get("since")))
        return record_authorization(config, born or datetime.now(UTC).isoformat(), fresh=True)
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
    return record_authorization(config, row[0], basis="first_capture") if row and row[0] else None
