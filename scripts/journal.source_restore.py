#!/usr/bin/env python3
"""Restore an approved journal/digest-run window from a trusted snapshot."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_journal_source_restore_runtime"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load scope-recall package from {PLUGIN_ROOT}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

from scope_recall_journal_source_restore_runtime.journal_source_restore import (  # noqa: E402
    mark_source_restore_cleanup_failure,
    run_journal_source_restore,
    source_restore_error_receipt,
)
from scope_recall_journal_source_restore_runtime.maintenance_lease import (  # noqa: E402
    MaintenanceLeaseError,
    acquire_activation_lease,
    release_activation_lease,
)
from scope_recall_journal_source_restore_runtime.maintenance_ops import effective_apply  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore approved journal rows from a trusted SQLite snapshot"
    )
    parser.add_argument("--source", required=True, help="Trusted checkpointed source snapshot")
    parser.add_argument("--target", required=True, help="Offline target truth database")
    parser.add_argument("--journal-created-at-start", required=True)
    parser.add_argument("--journal-created-at-end", required=True)
    parser.add_argument("--digest-started-at-start", required=True)
    parser.add_argument("--digest-started-at-end", required=True)
    parser.add_argument("--expected-journal-count", required=True, type=int)
    parser.add_argument("--expected-digest-run-count", required=True, type=int)
    parser.add_argument("--expected-journal-set-digest", required=True)
    parser.add_argument("--expected-digest-run-set-digest", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-schema-digest", required=True)
    parser.add_argument("--expected-user-version", required=True, type=int)
    parser.add_argument("--expected-target-epoch-digest", default="")
    parser.add_argument("--prewrite-backup-path", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--journal-excluded-start", default="")
    parser.add_argument("--journal-excluded-end", default="")
    parser.add_argument("--digest-excluded-start", default="")
    parser.add_argument("--digest-excluded-end", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _run_restore(args: argparse.Namespace, *, should_apply: bool) -> dict[str, Any]:
    return run_journal_source_restore(
        source_path=args.source,
        target_path=args.target,
        journal_created_at_start=args.journal_created_at_start,
        journal_created_at_end=args.journal_created_at_end,
        digest_started_at_start=args.digest_started_at_start,
        digest_started_at_end=args.digest_started_at_end,
        expected_journal_count=args.expected_journal_count,
        expected_digest_run_count=args.expected_digest_run_count,
        expected_journal_set_digest=args.expected_journal_set_digest,
        expected_digest_run_set_digest=args.expected_digest_run_set_digest,
        expected_source_sha256=args.expected_source_sha256,
        expected_schema_digest=args.expected_schema_digest,
        expected_user_version=args.expected_user_version,
        dry_run=not should_apply,
        maintenance_confirmed=args.maintenance_confirmed,
        expected_target_epoch_digest=args.expected_target_epoch_digest,
        prewrite_backup_path=args.prewrite_backup_path or None,
        operation_id=args.operation_id,
        journal_excluded_start=args.journal_excluded_start,
        journal_excluded_end=args.journal_excluded_end,
        digest_excluded_start=args.digest_excluded_start,
        digest_excluded_end=args.digest_excluded_end,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    should_apply = effective_apply(apply=args.apply, dry_run=args.dry_run)
    lease: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    if should_apply and args.maintenance_confirmed:
        try:
            lease = acquire_activation_lease(Path(args.target))
        except MaintenanceLeaseError:
            _print(source_restore_error_receipt("activation_lease_conflict", dry_run=False))
            return 1
    try:
        payload = _run_restore(args, should_apply=should_apply)
    except Exception:
        payload = source_restore_error_receipt(
            "apply_rolled_back" if should_apply else "source_unhealthy",
            dry_run=not should_apply,
        )
    finally:
        released = True
        if lease is not None:
            try:
                released = bool(release_activation_lease(lease))
            except Exception:
                released = False
        if lease is not None and not released:
            original = str((payload or {}).get("error_code") or "")
            if payload is None:
                payload = source_restore_error_receipt(
                    "activation_lease_cleanup_failed", dry_run=False
                )
            payload = mark_source_restore_cleanup_failure(
                payload, original_code=original
            )
    assert payload is not None
    _print(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
