#!/usr/bin/env python3
"""Run one Scope Recall governance scheduler cycle."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

try:
    from scope_recall.governance_scheduler import run_governance_cycle_for_home
except ImportError:  # pragma: no cover - direct source execution fallback
    from governance_scheduler import run_governance_cycle_for_home


def default_hermes_home() -> str:
    """Return the profile home for portable scheduler CLI use.

    Published scripts must not assume the maintainer's `.hermes-yuheng` profile;
    operators can set HERMES_HOME, otherwise the standard Hermes home is used.
    """
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", default=default_hermes_home(), help="Hermes home directory containing scope-recall/memory.sqlite3")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Run read-only inspection only (default)")
    mode.add_argument("--apply-safe", action="store_true", help="Apply only audited low-risk cleanup actions; requires at least one --scope-id")
    parser.add_argument("--json", action="store_true", help="Emit JSON (default output is JSON for automation)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--scope-id", action="append", default=[], help="Scope id to include; may be repeated")
    args = parser.parse_args()

    if args.apply_safe and not args.scope_id:
        parser.error("--apply-safe requires at least one --scope-id")

    dry_run = not bool(args.apply_safe)
    selected_scope_ids = list(args.scope_id or []) or None
    payload = run_governance_cycle_for_home(
        args.hermes_home,
        dry_run=dry_run,
        apply_safe=bool(args.apply_safe),
        limit=args.limit,
        scope_ids=selected_scope_ids,
        accessible_scope_ids=selected_scope_ids,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
