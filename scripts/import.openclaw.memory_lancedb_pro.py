#!/usr/bin/env python3
from __future__ import annotations

"""Import historical OpenClaw `memory-lancedb-pro` records into scope-recall.

This importer is conservative and idempotent:
- it never runs automatically
- it never overwrites scope-recall truth rows blindly
- repeated runs of the same source rows should not create duplicates
"""

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import lancedb

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_script_runtime"
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

from scope_recall_script_runtime.models import ImportedMemoryRow, build_import_fingerprint, json_dumps_stable, normalize_import_timestamp



def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="Path to OpenClaw memory/lancedb-pro directory")
    p.add_argument("--hermes-home", required=True, help="Target Hermes home containing scope-recall/")
    p.add_argument("--scope-prefix", default="imported.openclaw", help="Prefix for generated scope ids")
    p.add_argument("--dry-run", action="store_true", help="Inspect only; do not write target SQLite")
    return p.parse_args()



def map_row(row: dict[str, Any], scope_prefix: str) -> ImportedMemoryRow:
    raw_scope = str(row.get("scope") or "unknown")
    category = str(row.get("category") or "memory")
    content = str(row.get("text") or "").strip()
    updated_at = normalize_import_timestamp(row.get("timestamp"))
    metadata = row.get("metadata")
    metadata_text = metadata if isinstance(metadata, str) else json_dumps_stable(metadata or {})
    fingerprint = build_import_fingerprint(
        raw_scope=raw_scope,
        category=category,
        text=content,
        timestamp=updated_at,
        metadata_text=metadata_text,
    )
    return ImportedMemoryRow(
        id=f"openclaw:{fingerprint}",
        scope_id=f"{scope_prefix}|{raw_scope}",
        platform="imported-openclaw",
        user_id="",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="openclaw-import",
        agent_workspace="scope-recall",
        session_id="openclaw-import",
        source="openclaw-import",
        target=category,
        content=content,
        summary=content[:220],
        created_at=updated_at,
        updated_at=updated_at,
        import_metadata=metadata_text,
        import_fingerprint=fingerprint,
    )



def ensure_target_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            platform TEXT,
            user_id TEXT,
            chat_id TEXT,
            thread_id TEXT,
            gateway_session_key TEXT,
            agent_identity TEXT,
            agent_workspace TEXT,
            session_id TEXT,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_recalled_turn INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            memory_id UNINDEXED,
            content,
            summary
        );
        CREATE TABLE IF NOT EXISTS import_ledger (
            import_fingerprint TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            source_path TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_import_ledger_memory_id
            ON import_ledger(memory_id);
        """
    )
    conn.commit()



def import_rows(conn: sqlite3.Connection, rows: list[ImportedMemoryRow], source_path: Path) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for row in rows:
        ledger_hit = conn.execute(
            "SELECT 1 FROM import_ledger WHERE import_fingerprint = ?",
            (row.import_fingerprint,),
        ).fetchone()
        if ledger_hit:
            skipped += 1
            continue
        before_changes = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO memories (
                id, scope_id, platform, user_id, chat_id, thread_id, gateway_session_key,
                agent_identity, agent_workspace, session_id, source, target, content, summary,
                created_at, updated_at, last_recalled_turn
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                row.id,
                row.scope_id,
                row.platform,
                row.user_id,
                row.chat_id,
                row.thread_id,
                row.gateway_session_key,
                row.agent_identity,
                row.agent_workspace,
                row.session_id,
                row.source,
                row.target,
                row.content,
                row.summary,
                row.created_at,
                row.updated_at,
            ),
        )
        inserted_memory = conn.total_changes > before_changes
        before_fts = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
            (row.id, row.content, row.summary),
        )
        inserted_fts = conn.total_changes > before_fts
        before_ledger = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO import_ledger (
                import_fingerprint, source_kind, source_scope, source_path, memory_id, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.import_fingerprint,
                "openclaw-memory-lancedb-pro",
                row.scope_id,
                str(source_path),
                row.id,
                row.updated_at,
            ),
        )
        inserted_ledger = conn.total_changes > before_ledger
        if inserted_memory or inserted_fts or inserted_ledger:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped



def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser()
    hermes_home = Path(args.hermes_home).expanduser()
    target_dir = hermes_home / "scope-recall"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_db = target_dir / "memory.sqlite3"

    if not source.exists():
        print(json.dumps({"ok": False, "error": f"source not found: {source}"}, ensure_ascii=False))
        return 1

    db = lancedb.connect(str(source))
    listed = db.list_tables()
    tables = list(getattr(listed, "tables", listed))
    if "memories" not in tables:
        print(json.dumps({"ok": False, "error": f"memories table missing in {source}", "tables": tables}, ensure_ascii=False))
        return 1

    table = db.open_table("memories")
    if hasattr(table, "to_list"):
        rows = table.to_list()
    else:
        rows = table.to_pandas().to_dict(orient="records")
    mapped = [map_row(row, args.scope_prefix) for row in rows if str(row.get("text") or "").strip()]

    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "source": str(source),
            "target_db": str(target_db),
            "rows_found": len(rows),
            "rows_mappable": len(mapped),
            "sample": [row.__dict__ for row in mapped[:2]],
        }, ensure_ascii=False))
        return 0

    conn = sqlite3.connect(target_db)
    try:
        ensure_target_schema(conn)
        inserted, skipped = import_rows(conn, mapped, source)
    finally:
        conn.close()

    print(json.dumps({
        "ok": True,
        "source": str(source),
        "target_db": str(target_db),
        "rows_seen": len(mapped),
        "rows_inserted": inserted,
        "rows_skipped": skipped,
        "idempotent": True,
        "note": "Reinitialize scope-recall after import so its LanceDB companion can sync from SQLite truth.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
