"""CLI router and handler for scope-recall backup commands.

Supports:
- Legacy database snapshot: scope-recall backup --database <db> --output <out> [--manifest <manifest>]
- Full bundle creation: scope-recall backup create --output <bundle>, with a
  shared --root or local --host/--instance-root, and optional --with-vectors.
- Bundle verification: scope-recall backup verify --bundle <bundle> [--temp-restore-check]
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..core.file_lock import advisory_file_lock
from ..runtime.backup_bundle import BackupBundleError, create_backup_bundle
from ..runtime.backup_primitives import BackupError, backup_sqlite, safe_path
from .backup_bundle import resolve_store_binding, verify_backup_bundle
from .disaster_restore import DisasterRestoreError


def _emit(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _run_legacy_backup(args: argparse.Namespace) -> int:
    source_db = safe_path(args.database, must_exist=True)
    target = safe_path(args.output)
    manifest = safe_path(args.manifest) if args.manifest else target.with_suffix(target.suffix + ".json")
    receipt = backup_sqlite(source_db, target, manifest=manifest)
    _emit(receipt)
    return 0


def _run_create_bundle(args: argparse.Namespace) -> int:
    # Shared commands take this reentrant lock before resolving the binding;
    # runtime assembly takes it before the truth writer and worker locks too.
    held = advisory_file_lock(args.root / "commands.lock", timeout_seconds=0) if args.root else nullcontext()
    with held:
        binding = resolve_store_binding(root=args.root, host=args.host, instance_root=args.instance_root)
        output = safe_path(args.output)
        receipt = create_backup_bundle(
            binding=binding,
            output=output,
            with_vectors=bool(args.with_vectors),
            client_configs=tuple(args.client_config),
        )
    _emit(receipt)
    return 0


def _run_verify_bundle(args: argparse.Namespace) -> int:
    bundle_path = safe_path(args.bundle, must_exist=True)
    receipt = verify_backup_bundle(bundle_path, temp_restore_check=bool(args.temp_restore_check), mirror=args.mirror)
    _emit(receipt)
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "backup":
        argv = argv[1:]

    # Check if this is a subcommand (create, bundle, verify) or legacy flags
    if argv and argv[0] in ("create", "bundle"):
        parser = argparse.ArgumentParser(
            prog="scope-recall backup create", description="Create a complete backup bundle."
        )
        parser.add_argument("--output", type=Path, required=True, help="output zip bundle destination")
        parser.add_argument("--root", type=Path, help="shared store root directory")
        parser.add_argument("--instance-root", type=Path, help="local instance root directory")
        parser.add_argument("--host", choices=("hermes", "codex", "claude-code", "workbuddy", "dsh"), help="host type")
        parser.add_argument("--with-vectors", action="store_true", help="include vector files in backup")
        parser.add_argument(
            "--client-config",
            type=Path,
            action="append",
            default=[],
            help="also pack this local remote client's spool; repeat for each client",
        )
        args = parser.parse_args(argv[1:])
        try:
            return _run_create_bundle(args)
        except (BackupBundleError, BackupError, ValueError, OSError) as exc:
            _emit({"status": "error", "error": str(exc)})
            return 2

    if argv and argv[0] == "verify":
        parser = argparse.ArgumentParser(
            prog="scope-recall backup verify", description="Verify integrity of a backup bundle."
        )
        parser.add_argument("--bundle", type=Path, required=True, help="bundle zip file path")
        parser.add_argument(
            "--temp-restore-check", action="store_true", help="rehearse disaster restore in a temporary directory"
        )
        parser.add_argument("--mirror", type=Path, help="deletion mirror for a restore rehearsal")
        args = parser.parse_args(argv[1:])
        try:
            return _run_verify_bundle(args)
        except (BackupError, DisasterRestoreError, ValueError, OSError) as exc:
            _emit({"status": "error", "error": str(exc)})
            return 2

    # Legacy snapshot mode
    parser = argparse.ArgumentParser(
        prog="scope-recall backup", description="Consistent SQLite snapshot or bundle command."
    )
    parser.add_argument("--database", type=Path, required=True, help="source SQLite database")
    parser.add_argument("--output", type=Path, required=True, help="destination snapshot path")
    parser.add_argument("--manifest", type=Path, help="optional manifest path")
    args = parser.parse_args(argv)
    try:
        return _run_legacy_backup(args)
    except (BackupError, ValueError, OSError) as exc:
        _emit({"status": "error", "error": str(exc)})
        return 2


__all__ = ["main"]
