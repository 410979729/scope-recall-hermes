#!/usr/bin/env python3
"""Read-only Scope Recall memory browser CLI."""

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
PACKAGE_NAME = "scope_recall_memory_browser_runtime"
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

from scope_recall_memory_browser_runtime.memory_browser import (  # noqa: E402
    explain_recall,
    inspect_memory,
    list_candidates,
    list_memories,
    memory_db_path,
    open_readonly_memory_db,
    render_text,
)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-home", default=argparse.SUPPRESS, help="Hermes home/profile path")
    parser.add_argument("--db", default=argparse.SUPPRESS, help="Explicit memory.sqlite3 path for tests or maintenance")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    parser.add_argument("--raw", action="store_true", default=argparse.SUPPRESS, help="Show raw content/metadata instead of redacted browser output")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse Scope Recall memory rows read-only")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--db", default="", help="Explicit memory.sqlite3 path for tests or maintenance")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--raw", action="store_true", help="Show raw content/metadata instead of redacted browser output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    memories = subparsers.add_parser("memories")
    memories_sub = memories.add_subparsers(dest="memories_command", required=True)
    memories_list = memories_sub.add_parser("list")
    memories_list.add_argument("--target", default="")
    memories_list.add_argument("--scope-id", default="")
    memories_list.add_argument("--limit", type=int, default=20)
    _add_common_options(memories_list)
    memories_inspect = memories_sub.add_parser("inspect")
    memories_inspect.add_argument("--id", required=True)
    _add_common_options(memories_inspect)

    candidates = subparsers.add_parser("candidates")
    candidates_sub = candidates.add_subparsers(dest="candidates_command", required=True)
    candidates_list = candidates_sub.add_parser("list")
    candidates_list.add_argument("--target", default="")
    candidates_list.add_argument("--scope-id", default="")
    candidates_list.add_argument("--limit", type=int, default=20)
    _add_common_options(candidates_list)

    recall = subparsers.add_parser("recall")
    recall_sub = recall.add_subparsers(dest="recall_command", required=True)
    recall_explain = recall_sub.add_parser("explain")
    recall_explain.add_argument("--query", required=True)
    recall_explain.add_argument("--scope-id", default="")
    recall_explain.add_argument("--limit", type=int, default=5)
    _add_common_options(recall_explain)
    return parser.parse_args()


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser().resolve() if args.db else memory_db_path(Path(args.hermes_home))


def _emit(payload: dict[str, Any], *, json_output: bool, text_key: str) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(payload, key=text_key))
    return 0 if payload.get("ok") else 1


def main() -> int:
    args = parse_args()
    try:
        conn = open_readonly_memory_db(_db_path(args))
    except (FileNotFoundError, sqlite3.Error) as exc:
        payload = {"ok": False, "read_only": True, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"error: {exc}")
        return 1
    try:
        if args.command == "memories" and args.memories_command == "list":
            payload = list_memories(conn, target=args.target, scope_id=args.scope_id, limit=args.limit, raw=bool(args.raw))
            return _emit(payload, json_output=args.json, text_key="memories")
        if args.command == "memories" and args.memories_command == "inspect":
            payload = inspect_memory(conn, memory_id=args.id, raw=bool(args.raw))
            return _emit(payload, json_output=args.json, text_key="memory")
        if args.command == "candidates" and args.candidates_command == "list":
            payload = list_candidates(conn, target=args.target, scope_id=args.scope_id, limit=args.limit, raw=bool(args.raw))
            return _emit(payload, json_output=args.json, text_key="candidates")
        if args.command == "recall" and args.recall_command == "explain":
            payload = explain_recall(conn, query=args.query, scope_id=args.scope_id, limit=args.limit)
            return _emit(payload, json_output=args.json, text_key="results")
        print("scope-recall browser error: unsupported command", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
