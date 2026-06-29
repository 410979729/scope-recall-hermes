from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

def lancedb_table_names(db: Any) -> list[str]:
    """Return table names across LanceDB list_tables API shapes."""
    list_tables = getattr(db, "list_tables", None)
    raw_tables: Any = list_tables() if callable(list_tables) else db.table_names()
    if isinstance(raw_tables, dict):
        raw_tables = raw_tables.get("tables", [])
    elif hasattr(raw_tables, "tables"):
        raw_tables = getattr(raw_tables, "tables")
    else:
        raw_items = list(raw_tables)
        if raw_items and all(isinstance(item, tuple) and len(item) == 2 for item in raw_items):
            mapped_items = dict(raw_items)
            if "tables" in mapped_items:
                raw_tables = mapped_items["tables"]
            else:
                raw_tables = raw_items
        else:
            raw_tables = raw_items
    return [str(name) for name in raw_tables]


def lancedb_vector_ids(table: Any) -> list[str]:
    rows: list[dict[str, Any]] = []
    if hasattr(table, "to_list"):
        try:
            rows = list(table.to_list(columns=["id"]))
        except TypeError:
            rows = list(table.to_list())
    elif hasattr(table, "to_arrow"):
        arrow_table = table.to_arrow()
        if "id" in getattr(arrow_table, "column_names", []):
            arrow_table = arrow_table.select(["id"])
        rows = arrow_table.to_pylist()
    elif hasattr(table, "to_pandas"):
        frame = table.to_pandas()
        if "id" in getattr(frame, "columns", []):
            frame = frame[["id"]]
        rows = frame.to_dict(orient="records")
    return [str(row.get("id") or "") for row in rows if str(row.get("id") or "")]


def vector_dimensions(table: Any) -> int:
    try:
        vector_field = table.schema.field("vector")
        return int(getattr(vector_field.type, "list_size", 0) or 0)
    except Exception:
        return 0


def run_vector_search_smoke(table: Any, *, dimensions: int, row_count: int) -> str:
    if row_count <= 0:
        return "skipped_empty"
    if dimensions <= 0 or not hasattr(table, "search"):
        return "skipped_no_dimension"
    query = table.search([0.0] * dimensions)
    if hasattr(query, "limit"):
        query = query.limit(1)
    if hasattr(query, "to_list"):
        query.to_list()
    elif hasattr(query, "to_arrow"):
        query.to_arrow()
    return "ok"


def sqlite_truth_db_exists(hermes_home: Path) -> bool:
    return (hermes_home / "scope-recall" / "memory.sqlite3").exists()


def sqlite_indexable_memory_ids(hermes_home: Path, *, index_general: bool = False) -> set[str]:
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, target, metadata FROM memories").fetchall()
    finally:
        conn.close()
    memory_ids: set[str] = set()
    for row in rows:
        if not index_general and str(row["target"] or "") == "general":
            continue
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        lifecycle = str(metadata.get("lifecycle") or "").strip().lower() if isinstance(metadata, dict) else ""
        if lifecycle in {"superseded", "obsolete", "rejected", "archived"}:
            continue
        memory_id = str(row["id"] or "")
        if memory_id:
            memory_ids.add(memory_id)
    return memory_ids


def sqlite_indexable_memory_count(hermes_home: Path, *, index_general: bool = False) -> int:
    return len(sqlite_indexable_memory_ids(hermes_home, index_general=index_general))


def apply_vector_truth_consistency(
    payload: dict[str, Any],
    *,
    hermes_home: Path,
    index_general: bool,
    recommendations: list[str],
    vector_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]] | None:
    truth_db_present = sqlite_truth_db_exists(hermes_home)
    expected_ids = sqlite_indexable_memory_ids(hermes_home, index_general=index_general) if truth_db_present else set()
    expected_rows = len(expected_ids)
    payload["expected_indexable_rows"] = expected_rows
    payload["sqlite_truth_present"] = truth_db_present
    row_count = int(payload.get("row_count") or 0)
    if not truth_db_present:
        return None
    if expected_rows > 0 and row_count <= 0:
        payload.update({"status": "needs_repair", "ready": False})
        message = "vector companion is empty while SQLite truth has indexable active memories"
        recommendations.append("Vector companion is empty but SQLite truth has active indexable rows; run scripts/repair.vector_index.py.")
        return payload, {"ok": False, "failures": [message]}, recommendations
    if vector_ids is not None:
        vector_id_set = {str(memory_id) for memory_id in vector_ids if str(memory_id)}
        stale_ids = sorted(vector_id_set - expected_ids)
        missing_ids = sorted(expected_ids - vector_id_set)
        payload["stale_vector_id_count"] = len(stale_ids)
        payload["missing_vector_id_count"] = len(missing_ids)
        payload["stale_vector_id_samples"] = stale_ids[:20]
        payload["missing_vector_id_samples"] = missing_ids[:20]
        if stale_ids:
            payload.update({"status": "needs_repair", "ready": False})
            message = f"vector companion has {len(stale_ids)} stale id(s) not present in active SQLite truth"
            recommendations.append("Vector companion contains stale ids for missing or lifecycle-hidden SQLite rows; run scripts/repair.vector_index.py to rebuild from active SQLite truth.")
            return payload, {"ok": False, "failures": [message]}, recommendations
        if missing_ids:
            payload.update({"status": "needs_repair", "ready": False})
            message = f"vector companion is missing {len(missing_ids)} active SQLite truth id(s)"
            recommendations.append("Vector companion is missing active SQLite truth rows; run scripts/repair.vector_index.py to rebuild the companion.")
            return payload, {"ok": False, "failures": [message]}, recommendations
    if expected_rows > 0 and row_count < expected_rows:
        payload["status"] = "degraded"
        recommendations.append("Vector companion has fewer rows than active SQLite truth; schedule scripts/repair.vector_index.py to rebuild the companion.")
    return None


def lancedb_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    vector_dir = hermes_home / "scope-recall" / "lancedb"
    if not vector_dir.exists():
        recommendations.append("LanceDB companion directory is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"backend": "lancedb", "path": str(vector_dir), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"LanceDB directory not found: {vector_dir}"]}, recommendations

    try:
        import lancedb  # type: ignore

        db = lancedb.connect(str(vector_dir))
        table_names = lancedb_table_names(db)
        if "memories" not in table_names:
            recommendations.append("LanceDB table 'memories' is missing; run scripts/repair.vector_index.py.")
            payload = {"backend": "lancedb", "path": str(vector_dir), "status": "needs_repair", "ready": False, "tables": table_names}
            return payload, {"ok": False, "failures": ["LanceDB table 'memories' is missing"]}, recommendations
        table = db.open_table("memories")
        row_count = int(table.count_rows())
        vector_ids = lancedb_vector_ids(table)
        dimensions = vector_dimensions(table)
        search_smoke = run_vector_search_smoke(table, dimensions=dimensions, row_count=row_count)
        payload = {
            "backend": "lancedb",
            "path": str(vector_dir),
            "status": "ready",
            "ready": True,
            "tables": table_names,
            "row_count": row_count,
            "unique_id_count": len(set(vector_ids)),
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
        consistency = apply_vector_truth_consistency(payload, hermes_home=hermes_home, index_general=index_general, recommendations=recommendations, vector_ids=vector_ids)
        consistency_failures: list[str] = []
        if consistency is not None:
            payload, consistency_check, recommendations = consistency
            consistency_failures = [str(item) for item in (consistency_check.get("failures") or [])]
        if dimensions and expected_dimensions and dimensions != expected_dimensions:
            error = f"dimension mismatch: LanceDB table has {dimensions}, active/configured embedder expects {expected_dimensions}"
            recommendations.append("LanceDB companion dimensions do not match the active/configured embedder; run scripts/repair.vector_index.py to rebuild from SQLite truth.")
            payload.update(
                {
                    "status": "needs_repair",
                    "ready": False,
                    "error": error,
                    "expected_embedder": dict(expected_embedder or {}),
                }
            )
            return payload, {"ok": False, "failures": [*consistency_failures, error]}, recommendations
        if consistency is not None:
            return payload, {"ok": False, "failures": consistency_failures}, recommendations
        return payload, {"ok": True, "failures": []}, recommendations
    except Exception as exc:
        recommendations.append("LanceDB companion is unreadable; run scripts/repair.vector_index.py to rebuild it from SQLite truth.")
        payload = {"backend": "lancedb", "path": str(vector_dir), "status": "needs_repair", "ready": False, "error": str(exc)}
        return payload, {"ok": False, "failures": [f"LanceDB error: {exc}"]}, recommendations


def sqlite_vector_search_smoke(conn: sqlite3.Connection, *, dimensions: int, row_count: int) -> str:
    if row_count <= 0:
        return "skipped_empty"
    row = conn.execute("SELECT vector_json FROM vector_records ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return "skipped_empty"
    vector = json.loads(str(row["vector_json"] or "[]"))
    if dimensions and len(vector) != dimensions:
        raise RuntimeError(f"stored vector has {len(vector)} dimensions, vector_meta expects {dimensions}")
    # Touch numeric values to catch malformed JSON without mutating the store.
    sum(float(item) * float(item) for item in vector)
    return "ok"


def sqlite_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    vector_path = hermes_home / "scope-recall" / "vector.sqlite3"
    if not vector_path.exists():
        recommendations.append("sqlite-bruteforce companion DB is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"sqlite-bruteforce companion DB not found: {vector_path}"]}, recommendations

    try:
        conn = sqlite3.connect(f"file:{vector_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            required = {"vector_records", "vector_meta"}
            missing = sorted(required - set(tables))
            if missing:
                recommendations.append("sqlite-bruteforce companion schema is incomplete; run scripts/repair.vector_index.py.")
                payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "needs_repair", "ready": False, "tables": tables}
                return payload, {"ok": False, "failures": [f"sqlite-bruteforce tables missing: {missing}"]}, recommendations
            row_count = int(conn.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])
            vector_ids = [str(row["id"] or "") for row in conn.execute("SELECT id FROM vector_records").fetchall() if str(row["id"] or "")]
            meta = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key, value FROM vector_meta").fetchall()}
            dimensions = int(meta.get("dimensions") or 0)
            table_name = str(meta.get("table_name") or "")
            search_smoke = sqlite_vector_search_smoke(conn, dimensions=dimensions, row_count=row_count)
        finally:
            conn.close()

        payload = {
            "backend": "sqlite-bruteforce",
            "path": str(vector_path),
            "status": "ready",
            "ready": True,
            "tables": tables,
            "table": table_name,
            "row_count": row_count,
            "unique_id_count": len(set(vector_ids)),
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
        consistency = apply_vector_truth_consistency(payload, hermes_home=hermes_home, index_general=index_general, recommendations=recommendations, vector_ids=vector_ids)
        consistency_failures: list[str] = []
        if consistency is not None:
            payload, consistency_check, recommendations = consistency
            consistency_failures = [str(item) for item in (consistency_check.get("failures") or [])]
        if dimensions and expected_dimensions and dimensions != expected_dimensions:
            error = f"dimension mismatch: sqlite-bruteforce companion has {dimensions}, active/configured embedder expects {expected_dimensions}"
            recommendations.append("sqlite-bruteforce companion dimensions do not match the active/configured embedder; run scripts/repair.vector_index.py to rebuild from SQLite truth.")
            payload.update(
                {
                    "status": "needs_repair",
                    "ready": False,
                    "error": error,
                    "expected_embedder": dict(expected_embedder or {}),
                }
            )
            return payload, {"ok": False, "failures": [*consistency_failures, error]}, recommendations
        if consistency is not None:
            return payload, {"ok": False, "failures": consistency_failures}, recommendations
        return payload, {"ok": True, "failures": []}, recommendations
    except Exception as exc:
        recommendations.append("sqlite-bruteforce companion is unreadable; run scripts/repair.vector_index.py to rebuild it from SQLite truth.")
        payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "needs_repair", "ready": False, "error": str(exc)}
        return payload, {"ok": False, "failures": [f"sqlite-bruteforce error: {exc}"]}, recommendations


def vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    backend: str = "lancedb",
    fallback_backend: str = "",
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    normalized = "sqlite-bruteforce" if str(backend or "lancedb").strip().lower() == "sqlite" else str(backend or "lancedb").strip().lower()
    fallback = "sqlite-bruteforce" if str(fallback_backend or "").strip().lower() == "sqlite" else str(fallback_backend or "").strip().lower()
    if normalized == "sqlite-bruteforce":
        return sqlite_vector_report(hermes_home, expected_embedder=expected_embedder, index_general=index_general)
    if normalized == "lancedb":
        primary_payload, primary_check, primary_recommendations = lancedb_vector_report(hermes_home, expected_embedder=expected_embedder, index_general=index_general)
        if primary_check.get("ok") or fallback != "sqlite-bruteforce":
            return primary_payload, primary_check, primary_recommendations
        fallback_payload, fallback_check, fallback_recommendations = sqlite_vector_report(hermes_home, expected_embedder=expected_embedder, index_general=index_general)
        combined_recommendations = [
            "Primary LanceDB companion is unavailable; using configured sqlite-bruteforce fallback companion for doctor health.",
            *primary_recommendations,
            *fallback_recommendations,
        ]
        if fallback_check.get("ok"):
            payload = {
                "backend": "lancedb",
                "status": "fallback_ready",
                "ready": True,
                "primary": primary_payload,
                "fallback_backend": "sqlite-bruteforce",
                "fallback": fallback_payload,
            }
            return payload, {"ok": True, "failures": []}, combined_recommendations
        payload = {
            "backend": "lancedb",
            "status": "needs_repair",
            "ready": False,
            "primary": primary_payload,
            "fallback_backend": "sqlite-bruteforce",
            "fallback": fallback_payload,
        }
        return payload, {"ok": False, "failures": [*primary_check.get("failures", []), *fallback_check.get("failures", [])]}, combined_recommendations
    payload = {"backend": normalized, "status": "unsupported", "ready": False}
    return payload, {"ok": False, "failures": [f"unsupported vector backend: {normalized}"]}, ["Set vector.backend to 'lancedb' or 'sqlite-bruteforce'."]


def disabled_vector_report() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    payload = {"enabled": False, "status": "disabled", "ready": False}
    return payload, {"ok": True, "failures": []}, []
