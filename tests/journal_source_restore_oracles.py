"""Independent golden oracles for journal/digest/epoch set identity.

These values are computed outside production modules with a different
serialization path (tuple-then-join, not imported helpers). They exist so
tests do not merely mirror production digest/epoch code.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


def _stable_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def independent_journal_set_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Oracle for one journal identity set. Sort keys differ from production."""

    keyed = []
    for row in rows:
        item = (
            str(row.get("created_at") or ""),
            str(row.get("scope_id") or ""),
            str(row.get("session_id") or ""),
            int(row.get("turn_number") or 0),
            str(row.get("role") or ""),
            str(row.get("content_hash") or ""),
        )
        payload = {
            "content_hash": item[5],
            "created_at": item[0],
            "role": item[4],
            "scope_id": item[1],
            "session_id": item[2],
            "turn_number": item[3],
        }
        keyed.append((item, _stable_dump(payload)))
    keyed.sort(key=lambda pair: pair[0])
    return _sha("\n".join(text for _key, text in keyed))


def independent_digest_run_set_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    fields = (
        "id",
        "started_at",
        "finished_at",
        "status",
        "extractor",
        "interval_label",
        "processed_entries",
        "inserted",
        "updated",
        "skipped",
        "error",
        "metadata",
    )
    keyed = []
    for row in rows:
        payload = {field: row.get(field) for field in fields}
        keyed.append(((str(row.get("started_at") or ""), str(row.get("id") or "")), _stable_dump(payload)))
    keyed.sort(key=lambda pair: pair[0])
    return _sha("\n".join(text for _key, text in keyed))


def independent_epoch_digest(payload: Mapping[str, Any]) -> str:
    """Hash an already-bound epoch payload with an independent JSON dump."""

    return _sha(_stable_dump(dict(payload)))


GOLDEN_JOURNAL_VECTOR = (
    {
        "scope_id": "scope-golden",
        "session_id": "session-golden",
        "turn_number": 1,
        "role": "user",
        "content_hash": "0" * 64,
        "created_at": "2026-03-01T00:00:00+00:00",
    },
)
GOLDEN_JOURNAL_DIGEST = "9b31a4faa1d798fef081411a6ef4ade6a26e4ef78ac5507f537f06cc83bae1b6"

GOLDEN_DIGEST_VECTOR = (
    {
        "id": "oracle-run",
        "started_at": "2026-03-01T02:00:00+00:00",
        "finished_at": "2026-03-01T02:01:00+00:00",
        "status": "ok",
        "extractor": "heuristic",
        "interval_label": "oracle",
        "processed_entries": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "error": None,
        "metadata": "{}",
    },
)
GOLDEN_DIGEST_DIGEST = "c4b00810ac7119189d433e271e7f71056527d99169c490f8df018fda346352b6"

GOLDEN_EPOCH_PAYLOAD = {
    "file_sha256": "a" * 64,
    "schema_digest": "b" * 64,
    "sqlite_sequence": {"journal_entries": 7},
    "tables": {
        "journal_entries": {"count": 1, "digest": "c" * 64},
        "memories_fts": {"count": 0, "digest": "d" * 64},
        "operator_operations": {"count": 0, "digest": "e" * 64},
    },
    "user_version": 10802,
}
GOLDEN_EPOCH_DIGEST = "7672e16bd280b15236054eb7aefe0f7c386f7645c4a7321879703922d09fdcad"

_INDEPENDENT_EPOCH_TABLES = (
    "operator_operations",
    "memories_fts",
    "journal_entries",
    "procedural_playbooks",
    "journal_digest_runs",
    "memory_journal_sources",
    "journal_rejections",
    "memories",
)


def _independent_schema_digest(conn: Any) -> str:
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
    ).fetchall()
    payload = [
        {"name": str(row[1]), "sql": str(row[3]), "tbl_name": str(row[2]), "type": str(row[0])}
        for row in rows
    ]
    return _sha(_stable_dump(payload))


def _independent_table_digest(conn: Any, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return _sha("[]")
    columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    records = []
    for row in rows:
        mapping = {}
        for index, column in enumerate(columns):
            value = row[index]
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            mapping[column] = value
        records.append(mapping)
    records.sort(key=_stable_dump)
    return _sha("\n".join(_stable_dump(item) for item in records))


def independent_target_epoch(path: Any) -> dict[str, Any]:
    """Build a target-epoch payload from raw SQLite bytes, not production helpers."""

    import sqlite3
    from pathlib import Path

    db_path = Path(path)
    digest = hashlib.sha256()
    with db_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema_digest = _independent_schema_digest(conn)
        tables = {}
        for table in _INDEPENDENT_EPOCH_TABLES:
            tables[table] = {
                "count": int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
                "digest": _independent_table_digest(conn, table),
            }
        sequence_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'journal_entries'"
        ).fetchone()
        sqlite_sequence = {
            "journal_entries": 0 if sequence_row is None else int(sequence_row[0])
        }
    finally:
        conn.close()
    payload = {
        "file_sha256": digest.hexdigest(),
        "schema_digest": schema_digest,
        "sqlite_sequence": sqlite_sequence,
        "tables": tables,
        "user_version": user_version,
    }
    return {**payload, "epoch_digest": independent_epoch_digest(payload)}
