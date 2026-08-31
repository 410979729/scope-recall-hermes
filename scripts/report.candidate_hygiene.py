#!/usr/bin/env python3
"""Emit a bounded, content-free, read-only candidate hygiene report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Sequence

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_candidate_hygiene_runtime"
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

from scope_recall_candidate_hygiene_runtime.candidate_hygiene import (  # noqa: E402
    DEFAULT_CANDIDATE_HYGIENE_LIMIT,
    candidate_hygiene_report,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to memory.sqlite3")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CANDIDATE_HYGIENE_LIMIT,
        help="Maximum candidate rows to report (hard-capped by the library)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database_path = Path(args.db).expanduser().resolve()
    if not database_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": "SQLite truth DB not found"},
                ensure_ascii=False,
            )
        )
        return 1
    try:
        report = candidate_hygiene_report(database_path, limit=args.limit)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
