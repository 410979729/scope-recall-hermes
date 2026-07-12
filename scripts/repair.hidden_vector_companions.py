#!/usr/bin/env python3
"""Plan or apply backup-first cleanup of hidden/orphan vector IDs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_hidden_vector_repair_runtime"
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

from scope_recall_hidden_vector_repair_runtime.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall_hidden_vector_repair_runtime.vector_repair import repair_hidden_vector_companions  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify and remove terminal-hidden/orphan IDs from every local vector companion"
    )
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only (default)")
    mode.add_argument("--apply", action="store_true", help="Back up all affected companions, then delete classified IDs")
    parser.add_argument(
        "--include-policy-excluded",
        action="store_true",
        help="Also delete policy-excluded truth IDs, such as general rows when index_general is disabled",
    )
    parser.add_argument(
        "--confirm-policy-excluded",
        action="store_true",
        help="Required with --apply --include-policy-excluded",
    )
    parser.add_argument(
        "--confirm-quiescent",
        action="store_true",
        help="Required with --apply; confirms vector writers are stopped or otherwise quiescent",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _render_summary(result: dict[str, Any]) -> str:
    lines = [
        f"ok={bool(result.get('ok'))}",
        f"dry_run={bool(result.get('dry_run'))}",
        f"companions={int(result.get('companion_count') or 0)}",
        f"planned_delete={int(result.get('planned_delete') or 0)}",
    ]
    if not result.get("dry_run"):
        lines.extend(
            [
                f"deleted={int(result.get('deleted') or 0)}",
                f"failed={int(result.get('failed') or 0)}",
                f"backup_root={result.get('backup_root') or ''}",
                f"receipt_path={result.get('receipt_path') or ''}",
            ]
        )
    for item in result.get("companions") or []:
        lines.append(
            "companion "
            f"backend={item.get('backend')} path={item.get('path')} "
            f"status={item.get('status')} terminal_hidden={int(item.get('terminal_hidden_count') or 0)} "
            f"policy_excluded={int(item.get('policy_excluded_count') or 0)} "
            f"orphan={int(item.get('orphan_count') or 0)} "
            f"planned_delete={int(item.get('planned_delete_count') or 0)}"
        )
    for error in result.get("errors") or []:
        lines.append(f"error={sanitize_report_text(str(error))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.confirm_quiescent:
        message = "--apply requires --confirm-quiescent"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 2
    if args.apply and args.include_policy_excluded and not args.confirm_policy_excluded:
        message = "--apply --include-policy-excluded requires --confirm-policy-excluded"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 2

    try:
        result = repair_hidden_vector_companions(
            Path(args.hermes_home).expanduser(),
            include_policy_excluded=bool(args.include_policy_excluded),
            apply=bool(args.apply),
            quiescent_confirmed=bool(args.confirm_quiescent),
        )
    except Exception as exc:
        message = sanitize_report_text(str(exc))
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"error={message}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_summary(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
