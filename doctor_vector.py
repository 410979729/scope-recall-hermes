"""Vector companion doctor checks for LanceDB/SQLite vector readiness and consistency with SQLite truth rows.

Vector state is rebuildable companion data; failures should mark repair needs without deleting truth rows."""

from __future__ import annotations

from collections import Counter
import json
import sqlite3
from pathlib import Path
from typing import Any

from .lifecycle_policy import DURABLE_HIDDEN_LIFECYCLES, ordinary_recall_lifecycle_visible
from .capture_filters import sanitize_report_text
from .vector_generation import resolve_generation_storage_root
from .vector_generation_preflight import validate_generation_for_activation
from .vector_store import native_vector_dependency_status, normalize_vector_backend

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


def _active_generation_manifest_read_only(hermes_home: Path) -> dict[str, Any] | None:
    """Return the pointer-selected generation without mutating old schemas."""

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not {"vector_generation_state", "vector_generations"} <= tables:
            return None
        row = conn.execute(
            """
            SELECT g.*
            FROM vector_generation_state AS s
            JOIN vector_generations AS g ON g.generation_id = s.value
            WHERE s.key = 'current_generation'
            """
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def sqlite_truth_vector_categories(hermes_home: Path, *, index_general: bool = False) -> dict[str, set[str]]:
    """Classify truth IDs for vector consistency without conflating debt types."""

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    categories: dict[str, set[str]] = {
        "all": set(),
        "indexable": set(),
        "terminal_hidden": set(),
        "policy_excluded": set(),
    }
    if not db_path.exists():
        return categories
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, target, metadata FROM memories").fetchall()
    finally:
        conn.close()
    for row in rows:
        memory_id = str(row["id"] or "")
        if not memory_id:
            continue
        categories["all"].add(memory_id)
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        lifecycle = str(metadata.get("lifecycle") or "").strip().lower() if isinstance(metadata, dict) else ""
        if lifecycle in DURABLE_HIDDEN_LIFECYCLES:
            categories["terminal_hidden"].add(memory_id)
        elif not ordinary_recall_lifecycle_visible(
            lifecycle=lifecycle,
            target=str(row["target"] or ""),
        ):
            categories["policy_excluded"].add(memory_id)
        elif not index_general and str(row["target"] or "") == "general":
            categories["policy_excluded"].add(memory_id)
        else:
            categories["indexable"].add(memory_id)
    return categories


def sqlite_indexable_memory_ids(hermes_home: Path, *, index_general: bool = False) -> set[str]:
    return sqlite_truth_vector_categories(hermes_home, index_general=index_general)["indexable"]


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
    if vector_ids is not None:
        counts = Counter(str(memory_id) for memory_id in vector_ids if str(memory_id))
        duplicate_ids = sorted(memory_id for memory_id, count in counts.items() if count > 1)
        duplicate_rows = sum(count - 1 for count in counts.values() if count > 1)
        payload["duplicate_rows"] = duplicate_rows
        payload["duplicate_id_count"] = len(duplicate_ids)
        payload["duplicate_id_samples"] = duplicate_ids[:20]
        if duplicate_rows:
            payload.update({"status": "needs_repair", "ready": False})
            message = (
                "vector companion has "
                f"{duplicate_rows} duplicate physical row(s) across "
                f"{len(duplicate_ids)} id(s)"
            )
            recommendations.append(
                "Do not rewrite the active vector table in place; build and validate a shadow generation from SQLite truth, then activate it with CAS."
            )
            return payload, {"ok": False, "failures": [message]}, recommendations

    truth_db_present = sqlite_truth_db_exists(hermes_home)
    truth_categories = sqlite_truth_vector_categories(hermes_home, index_general=index_general) if truth_db_present else {
        "all": set(),
        "indexable": set(),
        "terminal_hidden": set(),
        "policy_excluded": set(),
    }
    expected_ids = truth_categories["indexable"]
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
        terminal_hidden_ids = sorted(vector_id_set & truth_categories["terminal_hidden"])
        policy_excluded_ids = sorted(vector_id_set & truth_categories["policy_excluded"])
        orphan_ids = sorted(vector_id_set - truth_categories["all"])
        stale_ids = sorted(vector_id_set - expected_ids)
        missing_ids = sorted(expected_ids - vector_id_set)
        payload.update(
            {
                "terminal_hidden_vector_id_count": len(terminal_hidden_ids),
                "terminal_hidden_vector_id_samples": terminal_hidden_ids[:20],
                "policy_excluded_vector_id_count": len(policy_excluded_ids),
                "policy_excluded_vector_id_samples": policy_excluded_ids[:20],
                "orphan_vector_id_count": len(orphan_ids),
                "orphan_vector_id_samples": orphan_ids[:20],
                "stale_vector_id_count": len(stale_ids),
                "missing_vector_id_count": len(missing_ids),
                "stale_vector_id_samples": stale_ids[:20],
                "missing_vector_id_samples": missing_ids[:20],
            }
        )
        if stale_ids:
            payload.update({"status": "needs_repair", "ready": False})
            message = f"vector companion has {len(stale_ids)} stale id(s) outside active SQLite truth policy"
            recommendations.append(
                "Vector companion contains stale ids outside active SQLite truth policy; inspect the classified debt before cleanup or rebuild."
            )
            if terminal_hidden_ids:
                recommendations.append(
                    "Vector companion contains terminal-hidden SQLite truth IDs; run scripts/repair.hidden_vector_companions.py --dry-run, then apply only after backup and operator approval."
                )
            if policy_excluded_ids:
                recommendations.append(
                    "Vector companion contains policy-excluded truth IDs; verify index_general policy before cleanup or rebuild."
                )
            if orphan_ids:
                recommendations.append(
                    "Vector companion contains IDs absent from SQLite truth; run the classified hidden/orphan companion repair."
                )
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
    storage_root: Path | None = None,
    table_name: str = "memories",
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect LanceDB vector companion status and compare it with SQLite truth expectations.

    The report highlights missing, duplicate, or dimension-mismatched vector rows as repair debt."""
    recommendations: list[str] = []
    vector_dir = (storage_root or (hermes_home / "scope-recall")) / "lancedb"
    if not vector_dir.exists():
        recommendations.append("LanceDB companion directory is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"backend": "lancedb", "path": str(vector_dir), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"LanceDB directory not found: {vector_dir}"]}, recommendations

    native_status = native_vector_dependency_status()
    if not bool(native_status.get("safe")):
        returncode = native_status.get("returncode")
        payload = {
            "backend": "lancedb",
            "path": str(vector_dir),
            "status": "needs_repair",
            "ready": False,
            "error": "LanceDB/PyArrow native dependency probe was unsafe",
            "native_dependency": {
                "safe": False,
                "returncode": returncode,
            },
        }
        recommendations.append(
            "Do not import the current LanceDB/PyArrow wheels in-process; install CPU-compatible wheels or use sqlite-bruteforce fallback."
        )
        return payload, {
            "ok": False,
            "failures": [f"LanceDB/PyArrow native dependency probe failed (returncode={returncode})"],
        }, recommendations

    try:
        import lancedb  # type: ignore

        db = lancedb.connect(str(vector_dir))
        table_names = lancedb_table_names(db)
        if table_name not in table_names:
            recommendations.append(f"LanceDB table {table_name!r} is missing; run scripts/repair.vector_index.py.")
            payload = {"backend": "lancedb", "path": str(vector_dir), "status": "needs_repair", "ready": False, "tables": table_names}
            return payload, {"ok": False, "failures": [f"LanceDB table {table_name!r} is missing"]}, recommendations
        table = db.open_table(table_name)
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
    storage_root: Path | None = None,
    expected_table_name: str = "memories",
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect the SQLite brute-force vector companion for consistency with truth rows.

    A bad companion should report repair-needed status, not delete or rewrite durable memories."""
    recommendations: list[str] = []
    vector_path = (storage_root or (hermes_home / "scope-recall")) / "vector.sqlite3"
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
        if table_name and expected_table_name and table_name != expected_table_name:
            error = (
                "table mismatch: sqlite-bruteforce companion has "
                f"{table_name!r}, active generation expects {expected_table_name!r}"
            )
            recommendations.append(
                "sqlite-bruteforce companion table identity does not match the active generation; build a shadow generation."
            )
            payload.update({"status": "needs_repair", "ready": False, "error": error})
            return payload, {"ok": False, "failures": [*consistency_failures, error]}, recommendations
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


def _backend_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    backend: str = "lancedb",
    fallback_backend: str = "",
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    storage_root = hermes_home / "scope-recall"
    table_name = "memories"
    physical_expected = dict(expected_embedder or {})
    manifest = _active_generation_manifest_read_only(hermes_home)
    normalized = normalize_vector_backend(backend or "lancedb")
    if manifest is not None:
        try:
            storage_root = resolve_generation_storage_root(
                hermes_home / "scope-recall",
                manifest.get("storage_path"),
            )
        except Exception as exc:
            payload = {
                "backend": normalize_vector_backend(manifest.get("backend") or normalized),
                "status": "needs_repair",
                "ready": False,
                "generation_id": str(manifest.get("generation_id") or ""),
                "error": str(exc),
            }
            return payload, {"ok": False, "failures": [f"active generation storage path is invalid: {exc}"]}, [
                "Repair the active vector generation manifest before opening or deleting any companion."
            ]
        normalized = normalize_vector_backend(manifest.get("backend") or normalized)
        table_name = str(manifest.get("table_name") or "memories")
        physical_expected["dimensions"] = int(manifest.get("dimensions") or 0)
    fallback = normalize_vector_backend(fallback_backend or "") if str(fallback_backend or "").strip() else ""

    if normalized == "sqlite-bruteforce":
        payload, check, recommendations = sqlite_vector_report(
            hermes_home,
            expected_embedder=physical_expected,
            index_general=index_general,
            storage_root=storage_root,
            expected_table_name=table_name,
        )
    elif normalized == "lancedb":
        primary_payload, primary_check, primary_recommendations = lancedb_vector_report(
            hermes_home,
            expected_embedder=physical_expected,
            index_general=index_general,
            storage_root=storage_root,
            table_name=table_name,
        )
        if primary_check.get("ok") or fallback != "sqlite-bruteforce":
            payload, check, recommendations = primary_payload, primary_check, primary_recommendations
        else:
            fallback_payload, fallback_check, fallback_recommendations = sqlite_vector_report(
                hermes_home,
                expected_embedder=physical_expected,
                index_general=index_general,
                storage_root=storage_root,
                expected_table_name=table_name,
            )
            recommendations = [
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
                check = {"ok": True, "failures": []}
            elif (
                str(fallback_payload.get("status") or "") == "missing"
                and sqlite_indexable_memory_count(
                    hermes_home,
                    index_general=index_general,
                )
                == 0
            ):
                recommendations.insert(
                    0,
                    "Configured sqlite-bruteforce fallback is not initialized, but SQLite truth has no indexable truth rows; initialize the companion on first use.",
                )
                payload = {
                    "backend": "lancedb",
                    "status": "fallback_not_initialized",
                    "ready": False,
                    "primary": primary_payload,
                    "fallback_backend": "sqlite-bruteforce",
                    "fallback": fallback_payload,
                }
                check = {"ok": True, "failures": []}
            else:
                payload = {
                    "backend": "lancedb",
                    "status": "needs_repair",
                    "ready": False,
                    "primary": primary_payload,
                    "fallback_backend": "sqlite-bruteforce",
                    "fallback": fallback_payload,
                }
                check = {
                    "ok": False,
                    "failures": [*primary_check.get("failures", []), *fallback_check.get("failures", [])],
                }
    else:
        payload = {"backend": normalized, "status": "unsupported", "ready": False}
        check = {"ok": False, "failures": [f"unsupported vector backend: {normalized}"]}
        recommendations = ["Set vector.backend to 'lancedb' or 'sqlite-bruteforce'."]

    if manifest is not None:
        payload["generation_id"] = str(manifest.get("generation_id") or "")
        payload["storage_root"] = str(storage_root)
        legacy_root = hermes_home / "scope-recall"
        if storage_root != legacy_root:
            legacy: dict[str, Any] = {}
            if (legacy_root / "vector.sqlite3").exists():
                legacy_payload, legacy_check, _ = sqlite_vector_report(
                    hermes_home,
                    expected_embedder=expected_embedder,
                    index_general=index_general,
                    storage_root=legacy_root,
                )
                legacy["sqlite-bruteforce"] = {"payload": legacy_payload, "check": legacy_check}
            if (legacy_root / "lancedb").exists():
                legacy_payload, legacy_check, _ = lancedb_vector_report(
                    hermes_home,
                    expected_embedder=expected_embedder,
                    index_general=index_general,
                    storage_root=legacy_root,
                )
                legacy["lancedb"] = {"payload": legacy_payload, "check": legacy_check}
            if legacy:
                payload["legacy_root_companions"] = legacy
                if any(not bool(item["check"].get("ok")) for item in legacy.values()):
                    recommendations.append(
                        "Inactive legacy root companion has consistency debt; inspect it before retirement or secure deletion."
                    )
    return payload, check, recommendations


def vector_generation_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None,
    backend: str,
    fallback_backend: str = "",
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Inspect generation identity, migration state, and durable replay debt."""

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "absent", "registered": False}, {"ok": True, "failures": []}, []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    recommendations: list[str] = []
    failures: list[str] = []
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        required = {"vector_generations", "vector_generation_state", "vector_migration_receipts", "vector_outbox"}
        if not required <= tables:
            recommendations.append(
                "Vector companion is still in legacy-unregistered mode; run a zero-write generation plan and register the legacy generation before migration."
            )
            return (
                {"status": "legacy_unregistered", "registered": False, "missing_tables": sorted(required - tables)},
                {"ok": True, "failures": []},
                recommendations,
            )
        pointer = conn.execute(
            "SELECT value, updated_at FROM vector_generation_state WHERE key = 'current_generation'"
        ).fetchone()
        if pointer is None:
            recommendations.append(
                "Vector generation tables are initialized but the legacy companion is not registered; register it before any model/backend migration."
            )
            return (
                {"status": "legacy_unregistered", "registered": False, "current_generation_id": ""},
                {"ok": True, "failures": []},
                recommendations,
            )
        current_id = str(pointer["value"] or "")
        current = conn.execute(
            "SELECT * FROM vector_generations WHERE generation_id = ?",
            (current_id,),
        ).fetchone()
        if current is None:
            failures.append(f"current vector generation manifest is missing: {current_id}")
            return (
                {"status": "generation_incomplete", "registered": True, "current_generation_id": current_id},
                {"ok": False, "failures": failures},
                ["Restore the missing manifest or CAS-activate a validated READY generation."],
            )
        current_payload = {key: current[key] for key in current.keys() if key != "metadata"}
        if str(current["status"] or "") != "active":
            failures.append(f"current vector generation {current_id} has status {current['status']!r}, expected 'active'")
        primary_expected = dict(expected_embedder or {})
        raw_fallback_expected = primary_expected.get("fallback")
        fallback_expected = (
            dict(raw_fallback_expected)
            if isinstance(raw_fallback_expected, dict)
            else {}
        )
        current_keys = set(current.keys())

        def current_value(key: str, default: Any = "") -> Any:
            return current[key] if key in current_keys else default

        current_backend = str(current["backend"] or "").lower()
        expected = primary_expected
        expected_backend = normalize_vector_backend(backend or "lancedb")
        active_embedder_source = "primary"

        def identity_matches(candidate: dict[str, Any], candidate_backend: str) -> bool:
            return all(
                (
                    current_backend == normalize_vector_backend(candidate_backend)
                    if key == "backend"
                    else int(current["dimensions"] or 0) == int(candidate.get("dimensions") or 0)
                    if key == "dimensions"
                    else bool(current_value("request_dimensions", 0))
                    == bool(candidate.get("request_dimensions", False))
                    if key == "request_dimensions"
                    else str(current_value(key, "") or "")
                    == str(candidate.get(key) or "")
                    if key in {"model", "document_prefix", "query_prefix"}
                    else str(current_value(key, "") or "").lower()
                    == str(candidate.get(key) or "").lower()
                )
                for key in (
                    "backend",
                    "provider",
                    "model",
                    "dimensions",
                    "metric",
                    "prompt_profile",
                    "document_prefix",
                    "query_prefix",
                    "request_dimensions",
                )
            )

        allowed_backends = [backend]
        if fallback_backend and normalize_vector_backend(fallback_backend) not in {
            normalize_vector_backend(item) for item in allowed_backends
        }:
            allowed_backends.append(fallback_backend)
        identity_candidates = [("primary", primary_expected)]
        if fallback_expected:
            identity_candidates.append(("fallback", fallback_expected))
        matched_identity = False
        for candidate_source, candidate_expected in identity_candidates:
            for candidate_backend in allowed_backends:
                if not identity_matches(candidate_expected, candidate_backend):
                    continue
                expected = candidate_expected
                expected_backend = normalize_vector_backend(candidate_backend)
                active_embedder_source = candidate_source
                matched_identity = True
                break
            if matched_identity:
                break

        mismatches: list[str] = []
        comparisons = {
            "backend": (current_backend, expected_backend),
            "provider": (str(current["provider"] or "").lower(), str(expected.get("provider") or "").lower()),
            "model": (str(current["model"] or ""), str(expected.get("model") or "")),
            "dimensions": (int(current["dimensions"] or 0), int(expected.get("dimensions") or 0)),
            "metric": (str(current["metric"] or "cosine").lower(), str(expected.get("metric") or "cosine").lower()),
            "prompt_profile": (
                str(current["prompt_profile"] or "default-v1"),
                str(expected.get("prompt_profile") or "default-v1"),
            ),
            "document_prefix": (
                str(current_value("document_prefix", "") or ""),
                str(expected.get("document_prefix") or ""),
            ),
            "query_prefix": (
                str(current_value("query_prefix", "") or ""),
                str(expected.get("query_prefix") or ""),
            ),
            "request_dimensions": (
                bool(current_value("request_dimensions", 0)),
                bool(expected.get("request_dimensions", False)),
            ),
        }
        exact_keys = {"metric", "prompt_profile", "document_prefix", "query_prefix", "request_dimensions"}
        for key, (actual, wanted) in comparisons.items():
            if (key in exact_keys or wanted not in {"", 0}) and actual != wanted:
                mismatches.append(f"{key}: current={actual!r}, configured={wanted!r}")
        if mismatches:
            failures.append("configured embedder does not match current generation: " + "; ".join(mismatches))
        provider_available = bool(expected.get("available", True))
        if not provider_available:
            failures.append(f"configured {active_embedder_source} embedding provider is unavailable")
            recommendations.append(
                f"Restore the configured {active_embedder_source} embedding provider for the active generation before vector query or write operations."
            )
        generation_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM vector_generations GROUP BY status"
            ).fetchall()
        }
        ready_generation_preflights: list[dict[str, Any]] = []
        ready_generation_preflight_failures: list[dict[str, str]] = []
        storage_dir = hermes_home / "scope-recall"
        ready_rows = conn.execute(
            "SELECT * FROM vector_generations WHERE status = 'ready' ORDER BY generation_id"
        ).fetchall()
        for ready_row in ready_rows:
            ready_manifest = dict(ready_row)
            ready_id = str(ready_manifest.get("generation_id") or "")
            try:
                ready_generation_preflights.append(
                    validate_generation_for_activation(storage_dir, conn, ready_manifest)
                )
            except Exception as exc:
                safe_error = sanitize_report_text(str(exc))[:300] or type(exc).__name__
                ready_generation_preflight_failures.append(
                    {"generation_id": ready_id, "error": safe_error}
                )
        if ready_generation_preflight_failures:
            recommendations.append(
                "Inactive READY rollback inventory failed physical preflight; it cannot be activated until rebuilt from a current truth cohort."
            )
        outbox_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM vector_outbox WHERE generation_id=? GROUP BY status",
                (current_id,),
            ).fetchall()
        }
        inactive_outbox_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM vector_outbox WHERE generation_id<>? GROUP BY status",
                (current_id,),
            ).fetchall()
        }
        reconciliation: dict[str, Any] = {"status": "schema_missing"}
        if "vector_reconciliation_state" in tables:
            reconciliation_row = conn.execute(
                """
                SELECT status, cursor_updated_at, cursor_memory_id,
                       upper_updated_at, upper_memory_id, cycle_number,
                       processed_rows, enqueued_events, next_cycle_at,
                       started_at, last_progress_at, completed_at, last_error
                FROM vector_reconciliation_state
                WHERE generation_id=?
                """,
                (current_id,),
            ).fetchone()
            if reconciliation_row is None:
                reconciliation = {"status": "not_started"}
            else:
                reconciliation = dict(reconciliation_row)
                reconciliation["last_error"] = sanitize_report_text(
                    str(reconciliation.get("last_error") or "")
                )[:300]
        reconciliation_failed = str(reconciliation.get("status") or "") == "failed"
        if reconciliation_failed:
            failures.append("vector reconciliation watermark is in failed state")
            recommendations.append(
                "Inspect the vector reconciliation watermark error, repair the underlying SQLite/outbox fault, and resume the bounded cycle without deleting its cursor."
            )
        elif str(reconciliation.get("status") or "") == "running":
            recommendations.append(
                "Vector reconciliation cycle is in progress; keep bounded maintenance running until the watermark reaches idle."
            )
        building = int(generation_counts.get("building", 0))
        backlog = sum(int(outbox_counts.get(status, 0)) for status in ("pending", "processing", "retry"))
        dead_letters = int(outbox_counts.get("dead_letter", 0))
        failed_receipts = int(
            conn.execute("SELECT COUNT(*) FROM vector_migration_receipts WHERE status = 'failed'").fetchone()[0]
        )
        if building:
            failures.append(f"{building} vector generation(s) remain in building state")
            recommendations.append("Resume or explicitly fail incomplete vector generation builds; never switch the pointer to an incomplete generation.")
        if backlog:
            failures.append(f"vector outbox has {backlog} replay event(s) pending")
            recommendations.append("Replay the durable vector outbox against the current compatible generation and verify it drains to zero.")
        if dead_letters:
            failures.append(f"vector outbox has {dead_letters} dead-letter event(s)")
            recommendations.append("Inspect and explicitly requeue or resolve dead-letter vector events after fixing the underlying provider/store failure.")
        if failed_receipts:
            recommendations.append(f"Review {failed_receipts} failed vector migration receipt(s); they do not change the current pointer but remain operator debt.")
        status = "ready"
        if mismatches:
            status = "identity_mismatch"
        elif not provider_available:
            status = "provider_unavailable"
        elif building:
            status = "generation_incomplete"
        elif dead_letters:
            status = "outbox_dead_letter"
        elif backlog:
            status = "outbox_backlog"
        elif reconciliation_failed:
            status = "reconciliation_failed"
        payload = {
            "status": status,
            "registered": True,
            "current_generation_id": current_id,
            "pointer_updated_at": str(pointer["updated_at"] or ""),
            "current": current_payload,
            "identity_mismatches": mismatches,
            "provider_available": provider_available,
            "active_embedder_source": active_embedder_source,
            "generation_status_counts": generation_counts,
            "ready_generation_preflights": ready_generation_preflights,
            "ready_generation_preflight_failures": ready_generation_preflight_failures,
            "outbox_status_counts": outbox_counts,
            "inactive_outbox_status_counts": inactive_outbox_counts,
            "outbox_backlog": backlog,
            "outbox_dead_letters": dead_letters,
            "reconciliation": reconciliation,
            "failed_migration_receipts": failed_receipts,
        }
        return payload, {"ok": not failures, "failures": failures}, recommendations
    finally:
        conn.close()


def vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    backend: str = "lancedb",
    fallback_backend: str = "",
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    payload, check, recommendations = _backend_vector_report(
        hermes_home,
        expected_embedder=expected_embedder,
        backend=backend,
        fallback_backend=fallback_backend,
        index_general=index_general,
    )
    generation_payload, generation_check, generation_recommendations = vector_generation_report(
        hermes_home,
        expected_embedder=expected_embedder,
        backend=backend,
        fallback_backend=fallback_backend,
    )
    payload["generation"] = generation_payload
    failures = [
        *[str(item) for item in (check.get("failures") or [])],
        *[str(item) for item in (generation_check.get("failures") or [])],
    ]
    return payload, {"ok": not failures, "failures": failures}, [*recommendations, *generation_recommendations]


def disabled_vector_report() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    payload = {"enabled": False, "status": "disabled", "ready": False}
    return payload, {"ok": True, "failures": []}, []
