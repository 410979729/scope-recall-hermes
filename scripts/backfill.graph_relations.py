#!/usr/bin/env python3
"""Backfill deterministic Scope Recall graph relations from SQLite truth.

Safe defaults:
- dry-run unless --apply is passed
- reads only the configured Hermes home SQLite truth store
- creates only deterministic `supersedes` edges from metadata.superseded_by
- same-scope only unless --allow-cross-scope is explicit
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
PACKAGE_NAME = "scope_recall_graph_backfill_runtime"
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

from scope_recall_graph_backfill_runtime.graph_relations import (  # noqa: E402
    backfill_supersedes_from_metadata,
    graph_relation_stats,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill deterministic scope-recall graph relations")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--db-path", default="", help="Override SQLite truth DB path")
    parser.add_argument("--apply", action="store_true", help="Write missing relations. Default is dry-run only")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run flag for operator/script compatibility; this is the default unless --apply is set")
    parser.add_argument("--scope-id", action="append", default=[], help="Restrict old/new memories to this scope id; may repeat")
    parser.add_argument("--allow-cross-scope", action="store_true", help="Allow supersedes edges across scopes")
    parser.add_argument("--limit", type=int, default=-1, help="Limit scanned candidate rows; negative means no limit")
    parser.add_argument("--max-planned", type=int, default=50, help="Maximum planned relation rows to include in JSON output")
    return parser.parse_args()


def _db_path(args: argparse.Namespace) -> Path:
    if str(args.db_path or "").strip():
        return Path(str(args.db_path)).expanduser()
    hermes_home = Path(str(args.hermes_home or "~/.hermes")).expanduser()
    return hermes_home / "scope-recall" / "memory.sqlite3"


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    args = parse_args()
    db_path = _db_path(args)
    apply_changes = bool(args.apply) and not bool(args.dry_run)
    if not db_path.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"SQLite truth DB not found: {db_path}",
                    "db_path": str(db_path),
                    "dry_run": not apply_changes,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    conn = _connect(db_path, read_only=not apply_changes)
    try:
        before = graph_relation_stats(conn, accessible_scope_ids=args.scope_id or None)
        result = backfill_supersedes_from_metadata(
            conn,
            apply=apply_changes,
            accessible_scope_ids=args.scope_id or None,
            same_scope_only=not bool(args.allow_cross_scope),
            limit=None if int(args.limit) < 0 else int(args.limit),
            max_planned=max(0, int(args.max_planned)),
        )
        if apply_changes:
            conn.commit()
        else:
            conn.rollback()
        after = graph_relation_stats(conn, accessible_scope_ids=args.scope_id or None)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "ok": True,
        "db_path": str(db_path),
        "dry_run": not apply_changes,
        "same_scope_only": not bool(args.allow_cross_scope),
        "before": before,
        "backfill": result,
        "after": after,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
