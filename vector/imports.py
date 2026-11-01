"""Resumable local import of existing embeddings, with no embedding client.

Old stores are read only. Each page commits its vector identities and cursor
together. A second full inventory catches changes before activation; missing
or unreadable input leaves recall explicitly degraded and preserves the input.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from ..core.deadline import remaining_seconds
from ..core.failure_policy import CAPABILITY_RECHECK
from . import VectorStoreCompatibilityError


def legacy_sources(directory: Path, table: str) -> list[tuple[str, Path]]:
    """Discover only the two documented companion layouts in this generation."""
    sources = []
    if (directory / "vector.sqlite3").is_file():
        sources.append(("sqlite", directory / "vector.sqlite3"))
    if (directory / "lancedb").is_dir():
        sources.append(("lance", directory / "lancedb"))
    return sources


def _read_timeout() -> float:
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise TimeoutError("vector import deadline exhausted")
    return 5.0 if remaining is None else min(5.0, remaining)


def _read_expired() -> int:
    remaining = remaining_seconds()
    return int(remaining is not None and remaining <= 0)


def _sqlite_page(path: Path, offset: int, limit: int, dimensions: int, table: str) -> tuple[list[dict], str]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_read_timeout())) as conn:
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(_read_expired, 1000)
        meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
        if int(meta.get("dimensions", 0)) != dimensions or meta.get("table_name") != table:
            raise VectorStoreCompatibilityError("legacy SQLite generation identity mismatch")
        rows = conn.execute("SELECT * FROM vector_records ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        result = []
        for item in rows:
            _read_timeout()
            row = dict(item)
            row["vector"] = json.loads(row.pop("vector_json"))
            row["content"], row["summary"] = "", ""
            result.append(row)
    files = (path, path.with_name(path.name + "-wal"))
    fingerprint = str([(p.stat().st_size, p.stat().st_mtime_ns) for p in files if p.exists()])
    return result, fingerprint


def _lance_page(path: Path, offset: int, limit: int, dimensions: int, table: str) -> tuple[list[dict], str]:
    if not (path / f"{table}.lance").is_dir():
        raise FileNotFoundError("legacy Lance table files are missing")
    # This is the only production import of the optional legacy reader.
    import lancedb

    db = lancedb.connect(str(path))
    source = db.open_table(table)
    if source.schema.field("vector").type.list_size != dimensions:
        raise VectorStoreCompatibilityError("legacy Lance dimensions mismatch")
    fingerprint = str(source.version)
    source.checkout(int(fingerprint))
    rows = source.search().limit(limit).offset(offset).to_list()
    for row in rows:
        row["content"], row["summary"] = "", ""
    return rows, fingerprint


def visible_in_truth(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """Import only classifiable objects not behind the truth database's deletion fence."""
    metadata = json.loads(row["target"])
    kind, ref, revision = metadata["object_kind"], metadata["object_ref"], metadata["object_revision"]
    if metadata["vector_id"] != row["id"] or type(revision) is not int:
        raise VectorStoreCompatibilityError("unclassifiable legacy vector identity")
    blocked = conn.execute(
        "SELECT 1 FROM object_blocks WHERE object_kind=? AND object_ref=? AND read_blocked=1", (kind, ref)
    ).fetchone()
    if blocked:
        return False
    if kind == "event":
        return (
            conn.execute(
                "SELECT 1 FROM source_events WHERE event_id=? AND source_revision=? AND read_blocked=0", (ref, revision)
            ).fetchone()
            is not None
        )
    heads = {
        "claim": ("claim_versions", "claim_id"),
        "episode": ("episode_versions", "episode_id"),
        "artifact": ("artifact_versions", "artifact_id"),
        "reference": ("reference_versions", "reference_id"),
    }
    if kind not in heads:
        raise VectorStoreCompatibilityError("unsupported legacy vector object")
    table, key = heads[kind]
    return conn.execute(f"SELECT 1 FROM {table} WHERE {key}=? AND revision=?", (ref, revision)).fetchone() is not None


def seed(store) -> None:
    """Mark detected legacy stores before the first reader can use an incomplete import."""
    conn = store._require_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vector_import_owned(source TEXT,id TEXT,seq INTEGER,PRIMARY KEY(source,id))"
    )
    for backend, path in legacy_sources(store.db_path.parent, store.table_name):
        if path.resolve() == store.db_path.resolve():
            continue
        conn.execute("DELETE FROM vector_imports WHERE source='missing:legacy-vectors'")
        conn.execute(
            "INSERT OR IGNORE INTO vector_imports(source,watermark,state) VALUES(?,0,'copy')",
            (backend + ":" + str(path),),
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vector_import_expected(source TEXT,id TEXT,identity TEXT,PRIMARY KEY(source,id))"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS vector_import_identity ON vector_import_expected(id,identity)")


def import_page(store, *, visible: Callable[[dict], bool], max_rows: int = 512) -> dict[str, Any]:
    """Copy or verify one page under the raw writer fence; cursor advancement is atomic."""
    with store._store_locked():
        conn = store._require_conn()
        state = conn.execute(
            "SELECT * FROM vector_imports WHERE state!='complete' AND retry_after<=? ORDER BY source LIMIT 1",
            (time.time(),),
        ).fetchone()
        if state is None:
            pending = conn.execute("SELECT 1 FROM vector_imports WHERE state!='complete' LIMIT 1").fetchone()
            return {"outcome": "held" if pending else "complete", "rows": 0}
        name, offset, phase, detail = state["source"], state["watermark"], state["state"], state["detail"]
        backend, raw_path = name.split(":", 1)
        try:
            if backend not in {"sqlite", "lance"}:
                raise FileNotFoundError("legacy vector input is missing")
            method = _sqlite_page if backend == "sqlite" else _lance_page
            rows, fingerprint = method(Path(raw_path), offset, max_rows, store.dimensions, store.table_name)
            store._execute("BEGIN IMMEDIATE")
            return _commit_page(store, name, offset, phase, detail, rows, fingerprint, visible)
        except Exception as exc:  # noqa: BLE001 - import failures retain their input and durable cursor.
            conn.rollback()
            conn.execute(
                "UPDATE vector_imports SET last_error=?,retry_after=? WHERE source=?",
                (type(exc).__name__, time.time() + CAPABILITY_RECHECK.delay(), name),
            )
            conn.commit()
            return {"outcome": "unreadable", "error": type(exc).__name__, "rows": 0}


def _identity(row: dict) -> str:
    metadata = json.loads(row["target"])
    return json.dumps(
        [
            row["scope_id"],
            metadata.get("object_kind"),
            metadata.get("object_ref"),
            metadata.get("object_revision"),
            metadata.get("embedding_space"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _accept_page(store, name, rows, visible):
    """Copy eligible identities and retain ownership only for rows this import actually created."""
    conn = store._require_conn()
    for row in rows:
        metadata = json.loads(row["target"])
        fenced = conn.execute("SELECT 1 FROM vector_tombstones WHERE id=?", (row["id"],)).fetchone()
        blocked = conn.execute(
            "SELECT 1 FROM purge_fences WHERE scope_id=? AND kind=? AND ref=?",
            (row["scope_id"], metadata.get("object_kind"), metadata.get("object_ref")),
        ).fetchone()
        if fenced or blocked or not visible(row):
            conn.execute("DELETE FROM vector_import_expected WHERE source=? AND id=?", (name, row["id"]))
            actual = conn.execute("SELECT scope_id,target FROM vector_records WHERE id=?", (row["id"],)).fetchone()
            if actual is None or _identity(dict(actual)) == _identity(row):
                store._delete_ids([row["id"]])
            continue
        prior = conn.execute("SELECT seq FROM vector_records WHERE id=?", (row["id"],)).fetchone()
        store._write_rows([row], importing=True)
        actual = conn.execute("SELECT seq,scope_id,target FROM vector_records WHERE id=?", (row["id"],)).fetchone()
        if actual is None or _identity(dict(actual)) != _identity(row):
            raise VectorStoreCompatibilityError("legacy import visible identity mismatch")
        if prior is None:
            conn.execute("INSERT OR REPLACE INTO vector_import_owned VALUES(?,?,?)", (name, row["id"], actual["seq"]))
        conn.execute("INSERT OR REPLACE INTO vector_import_expected VALUES(?,?,?)", (name, row["id"], _identity(row)))


def _verify_inventory(store, name):
    """Remove imported surplus without deleting a newer publication, then compare actual object identities."""
    conn = store._require_conn()
    extra = conn.execute(
        "SELECT o.id,o.seq FROM vector_import_owned o LEFT JOIN vector_import_expected e "
        "ON e.source=o.source AND e.id=o.id WHERE o.source=? AND e.id IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM vector_import_expected other WHERE other.source!=o.source AND other.id=o.id)",
        (name,),
    )
    for row in extra:
        conn.execute("DELETE FROM vector_records WHERE id=? AND seq=?", (row[0], row[1]))
    missing = conn.execute(
        "SELECT 1 FROM vector_import_expected e LEFT JOIN vector_records r ON r.id=e.id "
        "WHERE e.source=? AND (r.id IS NULL OR "
        "json_array(r.scope_id,json_extract(r.target,'$.object_kind'),"
        "json_extract(r.target,'$.object_ref'),json_extract(r.target,'$.object_revision'),"
        "json_extract(r.target,'$.embedding_space')) != e.identity) LIMIT 1",
        (name,),
    ).fetchone()
    if missing:
        raise VectorStoreCompatibilityError("legacy import visible identity mismatch")


def _commit_page(store, name, offset, phase, detail, rows, fingerprint, visible) -> dict[str, Any]:
    conn = store._require_conn()
    conn.execute("UPDATE vector_imports SET last_error='',retry_after=0 WHERE source=?", (name,))
    if detail and detail != fingerprint:
        conn.execute("DELETE FROM vector_import_expected WHERE source=?", (name,))
        conn.execute("UPDATE vector_imports SET watermark=0,state='copy',detail=? WHERE source=?", (fingerprint, name))
        conn.commit()
        return {"outcome": "catchup", "rows": 0}
    _accept_page(store, name, rows, visible)
    if rows:
        conn.execute(
            "UPDATE vector_imports SET watermark=?,detail=? WHERE source=?", (offset + len(rows), fingerprint, name)
        )
        outcome = phase
    elif phase == "copy":
        conn.execute("DELETE FROM vector_import_expected WHERE source=?", (name,))
        conn.execute(
            "UPDATE vector_imports SET watermark=0,state='verify',detail=? WHERE source=?", (fingerprint, name)
        )
        outcome = "verify"
    else:
        _verify_inventory(store, name)
        conn.execute("UPDATE vector_imports SET state='complete',detail=? WHERE source=?", (fingerprint, name))
        pending = conn.execute("SELECT 1 FROM vector_imports WHERE state!='complete' LIMIT 1").fetchone()
        outcome = "copy" if pending else "complete"
    conn.commit()
    return {"outcome": outcome, "rows": len(rows), "watermark": offset + len(rows)}
