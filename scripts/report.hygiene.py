#!/usr/bin/env python3
"""Read-only Scope Recall memory hygiene report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import lancedb
except Exception:  # pragma: no cover - optional dependency
    lancedb = None

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_hygiene_runtime"
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

from scope_recall_hygiene_runtime.hygiene import build_hygiene_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only Scope Recall SQLite hygiene report")
    parser.add_argument("--db", required=True, help="Path to scope-recall memory.sqlite3")
    parser.add_argument("--vector-dir", help="Path to Scope Recall LanceDB directory; defaults to sibling lancedb/ beside --db")
    parser.add_argument("--vector-table", default="memories", help="LanceDB table name")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--limit", type=int, default=200, help="Maximum examples per category")
    return parser.parse_args()


def _table_rows(table: Any) -> list[dict[str, Any]]:
    if hasattr(table, "to_list"):
        try:
            return list(table.to_list())
        except Exception:
            pass
    if hasattr(table, "to_arrow"):
        try:
            return table.to_arrow().to_pylist()
        except Exception:
            pass
    if hasattr(table, "to_pandas"):
        try:
            return table.to_pandas().to_dict(orient="records")
        except Exception:
            pass
    return []


class ReadOnlyLanceVectorRecords:
    def __init__(self, vector_dir: Path, table_name: str) -> None:
        self.vector_dir = vector_dir
        self.table_name = table_name

    def list_records(self) -> dict[str, dict[str, Any]]:
        if lancedb is None or not self.vector_dir.exists():
            return {}
        try:
            db = lancedb.connect(str(self.vector_dir))
            table = db.open_table(self.table_name)
        except Exception:
            return {}
        records: dict[str, dict[str, Any]] = {}
        for row in _table_rows(table):
            memory_id = str(row.get("id") or "")
            if not memory_id:
                continue
            current = records.get(memory_id)
            if current is None or str(row.get("updated_at") or "") >= str(current.get("updated_at") or ""):
                records[memory_id] = dict(row)
        return records


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Scope Recall Hygiene Report", "", f"Total rows: {report.get('total_rows', 0)}", "", "## Totals by target"]
    for target, count in (report.get("totals_by_target") or {}).items():
        lines.append(f"- {target}: {count}")
    categories = [
        "fts_index",
        "runtime_wrapper_noise",
        "assistant_prose_rows",
        "duplicate_dedupe_keys",
        "very_short_rows",
        "very_long_rows",
        "general_vector_rows",
        "likely_promotion_candidates",
        "likely_delete_candidates",
    ]
    for category in categories:
        payload = report.get(category) or {}
        if category == "fts_index":
            lines.extend([
                "",
                "## fts_index",
                f"Healthy: {payload.get('healthy', False)}",
                f"Memory rows: {payload.get('memory_rows', 0)}",
                f"FTS rows: {payload.get('fts_rows', 0)}",
                f"Stale FTS rows: {payload.get('stale_fts_rows', 0)}",
                f"Missing FTS rows: {payload.get('missing_fts_rows', 0)}",
                f"Duplicate FTS extra rows: {payload.get('duplicate_fts_extra_rows', 0)}",
            ])
            continue
        lines.extend(["", f"## {category}", f"Count: {payload.get('count', 0)}"])
        for item in payload.get("items", [])[:10]:
            preview = item.get("preview") or item.get("dedup_key") or item.get("id") or ""
            lines.append(f"- {item.get('id', item.get('keep_id', ''))}: {preview}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"SQLite truth DB not found: {db_path}"}, ensure_ascii=False))
        return 1
    vector_dir = Path(args.vector_dir).expanduser().resolve() if args.vector_dir else (db_path.parent / "lancedb")
    vector_store = ReadOnlyLanceVectorRecords(vector_dir, str(args.vector_table or "memories"))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_hygiene_report(conn, vector_store=vector_store, limit=args.limit)
        report["vector_report_source"] = {
            "enabled": lancedb is not None and vector_dir.exists(),
            "path": str(vector_dir),
            "table": str(args.vector_table or "memories"),
        }
    finally:
        conn.close()
    if args.format == "markdown":
        print(render_markdown(report), end="")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
