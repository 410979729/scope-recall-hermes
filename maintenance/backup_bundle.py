"""Maintenance re-exports and CLI helpers for full backup bundle creation and verification.

Provides store binding resolution for CLI commands and delegates bundle operations
to runtime.backup_bundle.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from scope_recall.contracts import InstanceBinding
from scope_recall.maintenance.shared import store_binding

from ..adapters.hermes.shared_entries import read_shared_payload
from ..runtime.backup_bundle import (
    BUNDLE_FORMAT,
    BackupBundleError,
    create_backup_bundle,
)
from ..runtime.backup_bundle import (
    verify_backup_bundle as verify_archive,
)
from .disaster_restore import apply_disaster_restore
from .doctor import load_binding
from .doctor_report import HostChoice


def verify_backup_bundle(
    bundle_path: Path, *, temp_restore_check: bool = False, mirror: Path | None = None
) -> dict[str, Any]:
    """Verify an archive and optionally rehearse the real restore in an isolated directory.

    A rehearsal requires the installation's deletion mirror and runs relocation,
    deletion replay and store diagnostics without registering or starting a host.
    Only durable results are returned; temporary destination paths are omitted.
    """
    receipt = verify_archive(bundle_path)
    if temp_restore_check:
        with tempfile.TemporaryDirectory(prefix="sr-rehearsal-") as raw:
            restored = apply_disaster_restore(bundle_path, Path(raw) / "restored", mirror=mirror)
            receipt["restore_check"] = {
                key: restored[key]
                for key in (
                    "status",
                    "doctor",
                    "mirror_replayed_operations",
                    "vector_rebuild_queued",
                    "deletion_protection",
                )
            }
    return receipt


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
