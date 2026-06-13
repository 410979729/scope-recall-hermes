#!/usr/bin/env python3
"""Archive legacy raw/general/scratch rows and normalize durable metadata.

Default mode is dry-run. Use --apply to mutate SQLite truth after an automatic
SQLite backup. This script intentionally does not delete content; legacy scratch
rows are marked archived so they remain auditable and restorable while normal
recall filters can ignore them.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_legacy_hygiene_runtime"
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

from scope_recall_legacy_hygiene_runtime.gating import compact_text  # noqa: E402
from scope_recall_legacy_hygiene_runtime.governance import classify_memory  # noqa: E402

SCRATCH_SOURCES = {"raw", "scratch", "legacy-raw", "legacy-scratch", "turn-user", "turn-assistant"}
SCRATCH_TYPES = {"raw", "scratch", "general"}
SCRIPT_VERSION = "legacy-hygiene-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy Scope Recall memory hygiene metadata")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--db", default="", help="Explicit memory.sqlite3 path; overrides --hermes-home")
    parser.add_argument("--apply", action="store_true", help="Apply the migration. Default is dry-run")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a SQLite backup before --apply")
    parser.add_argument("--limit", type=int, default=12, help="Maximum samples per category in output")
    return parser.parse_args()


def load_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {"raw_metadata": str(raw or "")}
    return dict(parsed) if isinstance(parsed, dict) else {"raw_metadata": str(raw or "")}


def is_legacy_scratch(row: sqlite3.Row, metadata: dict[str, Any]) -> bool:
    target = str(row["target"] or "").strip().lower()
    source = str(row["source"] or "").strip().lower()
    memory_type = str(metadata.get("memory_type") or metadata.get("type") or "").strip().lower()
    lifecycle = str(metadata.get("lifecycle") or "").strip().lower()
    if lifecycle == "archived":
        return False
    return target == "general" or source in SCRATCH_SOURCES and target == "general" or memory_type in SCRATCH_TYPES and target == "general"


def missing_durable_metadata(row: sqlite3.Row, metadata: dict[str, Any]) -> bool:
    target = str(row["target"] or "").strip().lower()
    if target == "general":
        return False
    return not str(metadata.get("lifecycle") or "").strip() or not str(metadata.get("category") or "").strip()


def sample(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "target": str(row["target"]),
        "source": str(row["source"]),
        "preview": compact_text(str(row["content"] or ""), 180),
    }


def backup_sqlite(conn: sqlite3.Connection, db_path: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"memory.sqlite3.pre-legacy-hygiene.{stamp}.sqlite3"
    dest = sqlite3.connect(backup_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return str(backup_path)


def planned_updates(rows: list[sqlite3.Row], *, migrated_at: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    archive_updates: list[tuple[str, str]] = []
    normalize_updates: list[tuple[str, str]] = []
    archive_samples: list[dict[str, Any]] = []
    normalize_samples: list[dict[str, Any]] = []
    for row in rows:
        metadata = load_metadata(row["metadata"])
        if is_legacy_scratch(row, metadata):
            original = {key: metadata.get(key) for key in ("category", "lifecycle", "memory_type", "tier") if key in metadata}
            metadata["category"] = "legacy-scratch"
            metadata["lifecycle"] = "archived"
            metadata.setdefault("memory_type", "episodic")
            metadata.setdefault("tier", "working")
            metadata.setdefault("scope_mode", "local")
            metadata["legacy_hygiene"] = {
                "action": "archive_legacy_scratch",
                "version": SCRIPT_VERSION,
                "migrated_at": migrated_at,
                "original": original,
            }
            archive_updates.append((json.dumps(metadata, ensure_ascii=False, sort_keys=True), str(row["id"])))
            archive_samples.append(sample(row))
            continue
        if missing_durable_metadata(row, metadata):
            classified = classify_memory(str(row["content"] or ""), str(row["target"] or "memory"), str(row["source"] or ""))
            original = {key: metadata.get(key) for key in ("category", "lifecycle", "memory_type", "tier") if key in metadata}
            for key in ("category", "lifecycle", "memory_type", "tier", "kind", "authority", "scope_mode", "sensitivity"):
                if not str(metadata.get(key) or "").strip() and classified.get(key) is not None:
                    metadata[key] = classified.get(key)
            for key in ("confidence", "importance", "trust", "source_trust"):
                if metadata.get(key) is None and classified.get(key) is not None:
                    metadata[key] = classified.get(key)
            metadata["legacy_hygiene"] = {
                "action": "normalize_durable_metadata",
                "version": SCRIPT_VERSION,
                "migrated_at": migrated_at,
                "original": original,
            }
            normalize_updates.append((json.dumps(metadata, ensure_ascii=False, sort_keys=True), str(row["id"])))
            normalize_samples.append(sample(row))
    return archive_updates, normalize_updates, archive_samples, normalize_samples


def count_after(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT id, source, target, content, metadata FROM memories").fetchall()
    scratch = 0
    missing = 0
    archived = 0
    for row in rows:
        metadata = load_metadata(row["metadata"])
        if is_legacy_scratch(row, metadata):
            scratch += 1
        if missing_durable_metadata(row, metadata):
            missing += 1
        if str(metadata.get("lifecycle") or "").strip().lower() == "archived":
            archived += 1
    return {"legacy_scratch_remaining": scratch, "durable_missing_lifecycle_or_category": missing, "archived_rows": archived}


def main() -> int:
    args = parse_args()
    if args.db:
        db_path = Path(args.db).expanduser().resolve()
    else:
        db_path = Path(args.hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"SQLite truth DB not found: {db_path}"}, ensure_ascii=False))
        return 1
    migrated_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=30000")
    try:
        rows = conn.execute("SELECT id, source, target, content, metadata FROM memories ORDER BY updated_at ASC, id ASC").fetchall()
        before = count_after(conn)
        archive_updates, normalize_updates, archive_samples, normalize_samples = planned_updates(rows, migrated_at=migrated_at)
        backup = ""
        if args.apply and (archive_updates or normalize_updates):
            if not args.no_backup:
                backup = backup_sqlite(conn, db_path)
            with conn:
                conn.executemany("UPDATE memories SET metadata = ? WHERE id = ?", archive_updates)
                conn.executemany("UPDATE memories SET metadata = ? WHERE id = ?", normalize_updates)
        after = count_after(conn)
        result = {
            "ok": True,
            "dry_run": not bool(args.apply),
            "db": str(db_path),
            "backup": backup,
            "planned_archive_legacy_scratch": len(archive_updates),
            "planned_normalize_durable_metadata": len(normalize_updates),
            "applied_archive_legacy_scratch": len(archive_updates) if args.apply else 0,
            "applied_normalize_durable_metadata": len(normalize_updates) if args.apply else 0,
            "before": before,
            "after": after,
            "archive_samples": archive_samples[: max(0, int(args.limit))],
            "normalize_samples": normalize_samples[: max(0, int(args.limit))],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
