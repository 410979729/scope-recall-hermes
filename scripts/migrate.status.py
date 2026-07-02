#!/usr/bin/env python3
"""Report Scope Recall SQLite schema migration ledger status.

Read-only by default and by design: this script opens the SQLite truth DB with
``mode=ro`` and ``PRAGMA query_only=ON``.  It does not call ``ensure_schema``;
missing or legacy ledgers are reported instead of being repaired implicitly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_migrate_status_runtime"
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

from scope_recall_migrate_status_runtime.sql_store import schema_migration_status  # noqa: E402

REPORT_SCHEMA_VERSION = "migration_status_report.v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report Scope Recall SQLite schema migration status")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--db", default="", help="Explicit memory.sqlite3 path; overrides --hermes-home")
    parser.add_argument("--json", action="store_true", help="Emit JSON output (default; accepted for operator consistency)")
    return parser.parse_args(argv)


def db_path_from_args(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    return Path(args.hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"


def build_payload(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "ok": False,
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "missing",
            "db": str(db_path),
            "schema_migrations": {},
            "error": f"SQLite truth DB not found: {db_path}",
        }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            status = schema_migration_status(conn)
        finally:
            conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "error",
            "db": str(db_path),
            "schema_migrations": {},
            "error": str(exc),
        }
    current = bool(status.get("current"))
    return {
        "ok": current,
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "current" if current else "needs_migration_metadata",
        "db": str(db_path),
        "schema_migrations": status,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(db_path_from_args(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
