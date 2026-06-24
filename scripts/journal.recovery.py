#!/usr/bin/env python3
"""Schedule replay for journal entries quarantined by digest failures."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_journal_recovery_runtime"
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

from scope_recall_journal_recovery_runtime.journal_recovery import recovery_report, schedule_replay  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay retry/dead-letter journal digest entries")
    parser.add_argument("--hermes-home", required=True, help="Hermes home/profile path")
    parser.add_argument("--apply", action="store_true", help="Actually schedule replay; default is dry-run")
    parser.add_argument("--limit", type=int, default=500, help="Maximum entries to schedule/report")
    parser.add_argument("--batch-id", default="", help="Operator batch id for audit/rollback trace")
    parser.add_argument("--include-dead-letter", action="store_true", help="Also replay dead-letter:* entries; default only retry-exhausted:*")
    parser.add_argument("--format", choices=["json", "summary"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prefixes = ["retry-exhausted:"]
    if args.include_dead_letter:
        prefixes.append("dead-letter:")
    try:
        if args.apply:
            payload = schedule_replay(conn, reason_prefixes=prefixes, limit=max(0, int(args.limit)), dry_run=False, batch_id=args.batch_id or None)
        else:
            payload = recovery_report(conn, reason_prefixes=prefixes, limit=max(0, int(args.limit)))
            payload["dry_run"] = True
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "dry_run": payload.get("dry_run", not args.apply),
                        "candidate_count": payload.get("candidate_count"),
                        "scheduled": payload.get("scheduled", 0),
                        "batch_id": payload.get("batch_id", ""),
                        "by_reason": payload.get("by_reason", {}),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
