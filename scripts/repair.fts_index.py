#!/usr/bin/env python3
"""Inspect or explicitly reconcile lifecycle-aware memory FTS membership."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _ensure_source_import() -> None:
    root = Path(__file__).resolve().parents[1]
    parent = root.parent
    for candidate in (str(parent), str(root)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


_ensure_source_import()

from scope_recall.fts_maintenance import repair_fts_index  # noqa: E402
from scope_recall.maintenance_ops import effective_apply  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or reconcile Scope Recall memory FTS lifecycle membership"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes home/profile path",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rebuild the FTS companion from policy-visible truth rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit read-only inspection (default and overrides --apply)",
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="confirm normal Scope Recall writers are stopped for the apply window",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON output (default; accepted for operator convenience)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    should_apply = effective_apply(apply=args.apply, dry_run=args.dry_run)
    payload = repair_fts_index(
        Path(args.hermes_home).expanduser(),
        apply=should_apply,
        maintenance_confirmed=bool(args.maintenance_confirmed),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (not should_apply or bool(payload.get("ok"))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
