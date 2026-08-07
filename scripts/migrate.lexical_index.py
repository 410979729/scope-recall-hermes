#!/usr/bin/env python3
"""Plan, build, activate, or roll back the CJK lexical shadow generation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_lexical_migration_runtime"
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

from scope_recall_lexical_migration_runtime.capture_filters import (  # noqa: E402
    sanitize_report_text,
)
from scope_recall_lexical_migration_runtime.fts_maintenance import (  # noqa: E402
    backup_permission_model,
    secure_online_backup,
)
from scope_recall_lexical_migration_runtime.lexical_generation import (  # noqa: E402
    LEXICAL_GENERATION_ID,
    activate_generation,
    lexical_source_binding,
    rollback_generation,
)
from scope_recall_lexical_migration_runtime.lexical_migration import (  # noqa: E402
    build_lexical_generation,
    plan_lexical_migration,
)
from scope_recall_lexical_migration_runtime.maintenance_ops import (  # noqa: E402
    connect_memory_db,
    memory_db_path,
)
from scope_recall_lexical_migration_runtime.maintenance_lease import (  # noqa: E402
    MaintenanceLeaseError,
    acquire_activation_lease,
    ensure_activation_guard_triggers,
    remove_activation_guard_triggers,
    release_activation_lease,
)
from scope_recall_lexical_migration_runtime.sql_store import ensure_schema  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse the bounded lexical migration operator command line."""

    parser = argparse.ArgumentParser(
        description="Build or switch the supplemental CJK lexical shadow index"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
    )
    parser.add_argument("--generation-id", default=LEXICAL_GENERATION_ID)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--sample-limit", type=int, default=24)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument(
        "--expected-current",
        default=None,
        help="Required CAS value for activate/rollback; use 'legacy' for no supplemental generation",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _expected_current(raw: str | None) -> str:
    normalized = str(raw or "").strip()
    return "" if normalized.lower() == "legacy" else normalized


def _blocked(
    status: str,
    error: str,
    *,
    backup_path: str = "",
    maintenance_lease: dict[str, bool] | None = None,
) -> int:
    _emit(
        {
            "ok": False,
            "status": status,
            "dry_run": False,
            "backup_path": backup_path,
            "error": sanitize_report_text(error),
            "maintenance_lease": maintenance_lease
            or {"acquired": False, "released": False},
        }
    )
    return 2


def main() -> int:
    """Execute a dry-run plan or an explicitly confirmed migration operation."""

    args = parse_args()
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(encoding="utf-8")
    if args.activate and args.rollback:
        return _blocked("invalid_request", "activate and rollback are mutually exclusive")
    if (args.activate or args.rollback) and args.expected_current is None:
        return _blocked(
            "expected_current_required",
            "activate and rollback require --expected-current for CAS",
        )
    if args.batch_size < 1 or args.batch_size > 10_000:
        return _blocked("invalid_request", "batch-size must be between 1 and 10000")
    if args.sample_limit < 1 or args.sample_limit > 128:
        return _blocked("invalid_request", "sample-limit must be between 1 and 128")

    hermes_home = Path(args.hermes_home).expanduser().resolve()
    db_path = memory_db_path(hermes_home)
    if not db_path.is_file():
        return _blocked("missing", "SQLite truth DB not found")

    if not args.apply:
        conn = connect_memory_db(db_path, apply=False)
        try:
            dry_payload = plan_lexical_migration(
                conn,
                str(args.generation_id),
            )
        finally:
            conn.close()
        dry_payload["requested_action"] = (
            "rollback" if args.rollback else "activate" if args.activate else "build"
        )
        _emit(dry_payload)
        return 0

    if not args.maintenance_confirmed:
        return _blocked(
            "confirmation_required",
            "stop normal Scope Recall writers and pass --maintenance-confirmed before apply",
        )

    action = "rollback" if args.rollback else "activate" if args.activate else "build"
    backup_path = ""
    try:
        lease = acquire_activation_lease(db_path)
    except MaintenanceLeaseError as exc:
        return _blocked("lease_conflict", str(exc))
    lease_state = {
        "acquired": True,
        "guards_installed": False,
        "guards_removed": False,
        "released": False,
    }
    conn = None
    apply_payload: dict[str, Any] | None = None
    failure: Exception | None = None
    try:
        conn = connect_memory_db(
            db_path,
            apply=True,
            lease_token=str(lease["token"]),
        )
        conn.execute("BEGIN IMMEDIATE")
        # Hold the write fence across binding capture, backup, compare, and
        # guard installation. SQLite online backup cannot run on the fenced
        # connection itself, so a separate reader connection copies the
        # committed snapshot while the fence blocks raw writers. The backup is
        # taken before guard triggers exist, so it stays free of temporary
        # maintenance objects.
        source_before = lexical_source_binding(conn)
        backup_reader = connect_memory_db(db_path, apply=False)
        try:
            backup = secure_online_backup(
                conn,
                db_path,
                purpose=f"lexical-{action}",
                backup_source=backup_reader,
            )
        finally:
            backup_reader.close()
        backup_path = str(backup)
        source_after_backup = lexical_source_binding(conn)
        if source_after_backup != source_before:
            raise RuntimeError("SQLite truth changed during backup fence")
        ensure_activation_guard_triggers(
            conn,
            db_path,
            lease_token=str(lease["token"]),
        )
        conn.commit()
        lease_state["guards_installed"] = True
        ensure_schema(conn, commit=True)
        if args.rollback:
            apply_payload = rollback_generation(
                conn,
                expected_current=_expected_current(args.expected_current),
            )
            conn.commit()
        elif args.activate:
            apply_payload = activate_generation(
                conn,
                str(args.generation_id),
                expected_current=_expected_current(args.expected_current),
            )
            conn.commit()
        else:
            apply_payload = build_lexical_generation(
                conn,
                str(args.generation_id),
                batch_size=int(args.batch_size),
                sample_limit=int(args.sample_limit),
            )
        assert apply_payload is not None
        apply_payload.update(
            {
                "ok": bool(apply_payload.get("ok", True)),
                "dry_run": False,
                "requested_action": action,
                "backup_path": backup_path,
                "backup_permission_model": backup_permission_model(),
                "source_fence": {
                    "before": source_before,
                    "after_backup": source_after_backup,
                    "stable": True,
                },
            }
        )
    except Exception as exc:
        failure = exc
        if conn is not None and conn.in_transaction:
            conn.rollback()
    finally:
        if conn is not None and lease_state["guards_installed"]:
            try:
                remove_activation_guard_triggers(conn)
                conn.commit()
                lease_state["guards_removed"] = True
            except Exception as exc:
                if conn.in_transaction:
                    conn.rollback()
                if failure is None:
                    failure = exc
        if conn is not None:
            conn.close()
        if not lease_state["guards_installed"] or lease_state["guards_removed"]:
            lease_state["released"] = release_activation_lease(lease)

    if not lease_state["released"]:
        return _blocked(
            "lease_release_failed",
            "activation maintenance lease release failed",
            backup_path=backup_path,
            maintenance_lease=lease_state,
        )
    if failure is not None:
        return _blocked(
            "blocked",
            str(failure),
            backup_path=backup_path,
            maintenance_lease=lease_state,
        )
    if apply_payload is None:
        return _blocked(
            "blocked",
            "lexical migration produced no receipt",
            backup_path=backup_path,
            maintenance_lease=lease_state,
        )
    apply_payload["maintenance_lease"] = lease_state
    _emit(apply_payload)
    return 0 if bool(apply_payload.get("ok")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
