#!/usr/bin/env python3
"""Read-only Experience Kernel replay benchmark."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_experience_replay_runtime"
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

from scope_recall_experience_replay_runtime.experience_replay import build_replay_report, load_replay_cases  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a read-only Experience Kernel replay benchmark")
    parser.add_argument("--db", required=True, help="Path to scope-recall memory.sqlite3")
    parser.add_argument("--case-file", required=True, help="JSON/JSONL replay cases")
    parser.add_argument("--scope-id", action="append", default=[], help="Accessible scope id; repeat for multiple scopes")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    return parser.parse_args()


def render_markdown(report: dict) -> str:
    lines = [
        "# Experience Replay Report",
        "",
        f"Cases: {report.get('case_count', 0)}",
        f"Passed: {report.get('pass_count', 0)}",
        f"Baseline coverage: {report.get('average_baseline_coverage', 0):.2f}",
        f"With experience coverage: {report.get('average_with_experience_coverage', 0):.2f}",
        f"Coverage gain: {report.get('average_coverage_gain', 0):.2f}",
        "",
        "## Cases",
    ]
    for case in report.get("cases") or []:
        status = "PASS" if case.get("passed") else "FAIL"
        lines.extend(
            [
                "",
                f"### {case.get('id', '')}: {status}",
                f"- decision: {case.get('decision', '')}",
                f"- playbook_id: {case.get('playbook_id', '')}",
                f"- baseline_coverage: {case.get('baseline_coverage', 0):.2f}",
                f"- with_experience_coverage: {case.get('with_experience_coverage', 0):.2f}",
                f"- missing_after_experience: {', '.join(case.get('missing_after_experience') or []) or '-'}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    case_file = Path(args.case_file).expanduser().resolve()
    cases = load_replay_cases(case_file)
    scopes = [str(scope_id).strip() for scope_id in args.scope_id if str(scope_id).strip()]
    if not scopes:
        raise SystemExit("at least one --scope-id is required")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_replay_report(conn, cases=cases, accessible_scope_ids=scopes)
    finally:
        conn.close()
    if args.format == "markdown":
        print(render_markdown(report), end="")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("pass_count") == report.get("case_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
