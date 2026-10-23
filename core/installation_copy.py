"""Persist the identity of one writable copy without changing source identities."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .file_lock import advisory_file_lock
from .retained_artifacts import extended_path


def copy_task_name(copy_id: str) -> str:
    """Name a background registration from the persisted writable-copy identity."""
    return "ScopeRecall-" + hashlib.sha256(copy_id.encode()).hexdigest()[:20]


def installation_copy_id(directory: Path, installation_id: str) -> str:
    """Read or exclusively create copy metadata; recovered copies mint their own ID.

    The truth installation ID continues to qualify sources and tombstones. This
    ID owns only local registrations and retained recovery points.
    """
    path = extended_path(directory / "copy-identity.json")
    with advisory_file_lock(directory / "copy-identity.lock", timeout_seconds=0):
        if not path.exists():
            payload = {"installation_id": installation_id, "copy_id": uuid.uuid4().hex}
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(payload) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        type(raw) is not dict
        or raw.get("installation_id") != installation_id
        or type(raw.get("copy_id")) is not str
        or len(raw["copy_id"]) != 32
        or any(character not in "0123456789abcdef" for character in raw["copy_id"])
    ):
        raise ValueError("invalid installation copy identity")
    return raw["copy_id"]
