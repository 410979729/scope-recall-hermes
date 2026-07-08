#!/usr/bin/env python3
"""Dry-run-first CLI for Scope Recall candidate review actions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_candidate_review_runtime"
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

from scope_recall_candidate_review_runtime.candidate_review import review_candidate  # noqa: E402
from scope_recall_candidate_review_runtime.memory_browser import memory_db_path  # noqa: E402


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True, help="Memory candidate id")
    parser.add_argument("--hermes-home", default=argparse.SUPPRESS, help="Hermes home/profile path")
    parser.add_argument("--db", default=argparse.SUPPRESS, help="Explicit memory.sqlite3 path for tests or maintenance")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Preview the review action without mutation")
    parser.add_argument("--apply", action="store_true", default=argparse.SUPPRESS, help="Apply the review action")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review Scope Recall memory candidates")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--db", default="", help="Explicit memory.sqlite3 path for tests or maintenance")
    parser.add_argument("--dry-run", action="store_true", help="Preview the review action without mutation")
    parser.add_argument("--apply", action="store_true", help="Apply the review action")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    subparsers = parser.add_subparsers(dest="action", required=True)
    promote = subparsers.add_parser("promote")
    _add_common_options(promote)
    archive = subparsers.add_parser("archive")
    _add_common_options(archive)
    supersede = subparsers.add_parser("supersede")
    _add_common_options(supersede)
    supersede.add_argument("--superseded-by", required=True)
    return parser.parse_args()


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser().resolve() if args.db else memory_db_path(Path(args.hermes_home))


def _render_text(payload: dict) -> str:
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'unknown error')}"
    mode = "dry-run" if payload.get("dry_run") else "applied"
    return f"{mode}: {payload.get('action')} {payload.get('id')} -> {payload.get('after', {}).get('lifecycle')}"


def main() -> int:
    args = parse_args()
    dry_run = not bool(getattr(args, "apply", False))
    if getattr(args, "dry_run", False):
        dry_run = True
    try:
        conn = sqlite3.connect(_db_path(args))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        payload = {"ok": False, "dry_run": dry_run, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _render_text(payload))
        return 1
    try:
        payload = review_candidate(
            conn,
            memory_id=args.id,
            action=args.action,
            superseded_by=getattr(args, "superseded_by", ""),
            dry_run=dry_run,
        )
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _render_text(payload))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
