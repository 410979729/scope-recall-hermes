#!/usr/bin/env python3
"""Repair orphan rows in scope-recall SQLite graph companion tables.

SQLite ``memories`` remains the truth source. ``memory_entities`` and
``memory_relations`` are rebuildable graph/lookup companions; orphan or
lifecycle-hidden rows should not survive deletes or lifecycle transitions.

Default mode is a read-only dry run. Pass ``--apply`` to delete repairable graph
rows. ``--dry-run`` wins over accidental ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _ensure_source_import() -> None:
    here = Path(__file__).resolve()
    root = here.parents[1]
    parent = root.parent
    for candidate in (str(parent), str(root)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


_ensure_source_import()

from scope_recall.graph_hygiene import (  # noqa: E402
    repair_graph_hygiene,
    repair_relation_rebuild_debt,
)
from scope_recall.maintenance_ops import effective_apply  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove orphan scope-recall graph rows")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--apply", action="store_true", help="delete orphan rows; default is read-only dry-run")
    parser.add_argument("--dry-run", action="store_true", help="explicit read-only dry-run (default; accepted for operator convenience)")
    parser.add_argument("--json", action="store_true", help="emit JSON output (accepted for operator convenience)")
    parser.add_argument(
        "--seed-relations",
        action="store_true",
        help="retired compatibility flag; fails closed without enqueueing work",
    )
    parser.add_argument(
        "--drain-relations",
        action="store_true",
        help="retired compatibility flag; fails closed without draining legacy work",
    )
    parser.add_argument("--scope-id", action="append", default=[], help="scope to seed; repeat for multiple scopes")
    parser.add_argument("--max-events", type=int, default=100, help="maximum relation chunks to drain")
    parser.add_argument("--pair-limit", type=int, default=250, help="maximum focus/peer pairs per relation chunk")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    should_apply = effective_apply(apply=args.apply, dry_run=args.dry_run)
    payload = repair_graph_hygiene(Path(args.hermes_home), apply=should_apply)
    relation_payload = repair_relation_rebuild_debt(
        Path(args.hermes_home),
        seed=bool(should_apply and args.seed_relations),
        drain=bool(should_apply and args.drain_relations),
        scope_ids=list(args.scope_id or []),
        max_events=args.max_events,
        pair_limit=args.pair_limit,
    )
    payload["relation_rebuild"] = relation_payload
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (payload.get("ok") and relation_payload.get("ok")) or not should_apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
