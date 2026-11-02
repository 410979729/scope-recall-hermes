"""Read third-party summaries as unconfirmed material, regardless of their labels."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from scope_recall.contracts import ContractError
from scope_recall.core.memory_import import ImportRecord

from .import_read import read_bytes, timestamp

_SUMMARY_FIELDS = {
    "observations": ("title", "subtitle", "narrative", "text", "facts"),
    "session_summaries": ("request", "investigated", "learned", "completed", "next_steps", "notes", "summary_text"),
}


def read_claude_mem(path: Path):
    """Stream observations and session summaries through a read-only SQLite connection."""
    if path.is_symlink() or not path.is_file():
        raise ContractError("INPUT_INVALID", "import_sqlite")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "observations" not in tables:
            raise ContractError("INPUT_INVALID", "import_observations")
        for table, fields in _SUMMARY_FIELDS.items():
            if table not in tables:
                continue
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                values = dict(row)
                if any(values.get(f) is not None and not isinstance(values[f], str) for f in fields):
                    raise ContractError("INPUT_INVALID", "import_sqlite_text")
                text = "\n".join(values[f] for f in fields if values.get(f))
                if not text.strip():
                    continue
                occurred = timestamp(values.get("created_at")) or timestamp(
                    values.get("created_at_epoch"), milliseconds=True
                )
                yield ImportRecord(
                    f"{table}/{row['id']}",
                    text,
                    "assistant",
                    occurred,
                    values.get("project"),
                    values.get("memory_session_id") or values.get("sdk_session_id"),
                    "assistant_visible",
                )


def read_mem0(path: Path):
    """Read get_all memory records or a schema-defined structured JSON export."""
    document = json.loads(read_bytes(path).decode("utf-8-sig"))
    items = document.get("results", document.get("memories")) if isinstance(document, dict) else document
    if items is None and isinstance(document, dict):
        # Structured exports have a caller-defined schema and no original message
        # actors or dates. Preserve the object as unknown-source material.
        yield ImportRecord("structured-export", json.dumps(document, ensure_ascii=False, sort_keys=True), "unknown")
        return
    if not isinstance(items, list):
        raise ContractError("INPUT_INVALID", "import_mem0")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("memory"), str)
        ):
            raise ContractError("INPUT_INVALID", "import_mem0_record")
        metadata = item.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ContractError("INPUT_INVALID", "import_mem0_metadata")
        yield ImportRecord(
            item["id"],
            item["memory"],
            "assistant",
            timestamp(item.get("updated_at") or item.get("created_at")),
            metadata.get("project_id"),
            item.get("run_id"),
            "assistant_visible",
        )
