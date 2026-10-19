"""Maintenance re-exports and CLI helpers for full backup bundle creation and verification.

Provides store binding resolution for CLI commands and delegates bundle operations
to runtime.backup_bundle.
"""

from __future__ import annotations

from pathlib import Path

from scope_recall.contracts import InstanceBinding

from ..adapters.hermes.shared_entries import read_shared_payload
from ..runtime.backup_bundle import (
    BUNDLE_FORMAT,
    BackupBundleError,
    create_backup_bundle,
    verify_backup_bundle,
)
from .doctor import load_binding
from .doctor_report import HostChoice
from .shared import store_binding


def resolve_store_binding(
    *,
    root: Path | None = None,
    host: HostChoice | None = None,
    instance_root: Path | None = None,
) -> InstanceBinding:
    """Resolve an InstanceBinding from CLI parameters."""
    if root is not None:
        payload = read_shared_payload(root)
        return store_binding(payload, root.resolve(), frozenset(payload.get("scope_ids", ())))
    if host is not None and instance_root is not None:
        binding, _ = load_binding(host, instance_root)
        return binding
    raise BackupBundleError("must provide --root for shared store, or --host and --instance-root for local store")


__all__ = [
    "BUNDLE_FORMAT",
    "BackupBundleError",
    "create_backup_bundle",
    "resolve_store_binding",
    "verify_backup_bundle",
]
