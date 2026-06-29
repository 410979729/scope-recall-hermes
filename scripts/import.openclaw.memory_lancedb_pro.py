#!/usr/bin/env python3
"""Import historical OpenClaw `memory-lancedb-pro` records into scope-recall.

The script is a thin LanceDB reader around ``migration_openclaw``.  It defaults
safe: inspect/dry-run unless ``--apply`` is passed.  The core importer remains
idempotent, refuses unsafe rows before writing, creates an online SQLite backup
before applying to an existing target DB, and can emit a JSON receipt.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_openclaw_import_runtime"
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

from scope_recall_openclaw_import_runtime.migration_openclaw import (  # noqa: E402
    DEFAULT_ALLOWED_TARGETS,
    map_openclaw_row,
    run_openclaw_import_rows,
)


def map_row(row: dict[str, Any], scope_prefix: str):
    """Backward-compatible wrapper for tests/operators that imported the script."""

    mapped = map_openclaw_row(row, scope_prefix)
    if mapped is None:
        raise ValueError("OpenClaw row has empty text and is not importable")
    return mapped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely import OpenClaw memory-lancedb-pro history into scope-recall SQLite truth")
    parser.add_argument("--source", required=True, help="Path to OpenClaw memory/lancedb-pro directory")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Target Hermes home containing scope-recall/")
    parser.add_argument("--scope-prefix", default="imported.openclaw", help="Prefix for generated scope ids")
    parser.add_argument(
        "--allow-target",
        action="append",
        default=[],
        help=f"Allowed target/category to import. Repeatable. Defaults to {', '.join(sorted(DEFAULT_ALLOWED_TARGETS))}",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the import. Default is dry-run/inspect only")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; kept for compatibility and overrides --apply")
    parser.add_argument("--receipt", default="", help="Optional JSON receipt path written after successful --apply")
    parser.add_argument(
        "--vector-repair",
        default="recommend",
        choices=["recommend", "dry-run", "apply", "none"],
        help="Record the desired post-import vector repair mode in the receipt. This importer does not run vector repair inline.",
    )
    return parser.parse_args()


def connect_lancedb(source: Path):
    try:
        import lancedb  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "lancedb is required only for importing OpenClaw memory-lancedb-pro sources; "
            "install scope-recall[lancedb] or lancedb to run this importer."
        ) from exc
    return lancedb.connect(str(source))


def load_openclaw_rows(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    db = connect_lancedb(source)
    listed = db.list_tables()
    tables = [str(item) for item in list(getattr(listed, "tables", listed))]
    if "memories" not in tables:
        raise RuntimeError(json.dumps({"error": f"memories table missing in {source}", "tables": tables}, ensure_ascii=False))
    table = db.open_table("memories")
    if hasattr(table, "to_list"):
        rows = table.to_list()
    elif hasattr(table, "to_arrow"):
        rows = table.to_arrow().to_pylist()
    else:
        rows = table.to_pandas().to_dict(orient="records")
    return [dict(row) for row in rows], tables


def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser()
    hermes_home = Path(args.hermes_home).expanduser()
    target_db = hermes_home / "scope-recall" / "memory.sqlite3"
    receipt_path = Path(args.receipt).expanduser() if args.receipt else None
    allowed_targets = set(args.allow_target) if args.allow_target else None
    apply = bool(args.apply and not args.dry_run)

    if not source.exists():
        print(json.dumps({"ok": False, "error": f"source not found: {source}"}, ensure_ascii=False))
        return 1
    try:
        rows, tables = load_openclaw_rows(source)
        report = run_openclaw_import_rows(
            rows,
            source_path=source,
            target_db=target_db,
            scope_prefix=args.scope_prefix,
            allowed_targets=allowed_targets,
            apply=apply,
            receipt_path=receipt_path,
            vector_repair=args.vector_repair,
        )
        report["tables"] = tables
    except RuntimeError as exc:
        try:
            payload = json.loads(str(exc))
        except Exception:
            payload = {"error": str(exc)}
        print(json.dumps({"ok": False, **payload}, ensure_ascii=False))
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
