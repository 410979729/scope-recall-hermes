#!/usr/bin/env python3
"""Inspect scope-recall source metadata and runtime storage health.

The doctor is intentionally read-only. It treats SQLite as the truth layer and
any configured vector backend as an optional rebuildable companion, then emits a
compact JSON report that operators can use before running repair or release
checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

try:
    from scope_recall.capture_filters import redact_secret_like_text
except Exception:  # pragma: no cover - keeps the standalone doctor script usable from source checkouts
    def redact_secret_like_text(text: Any) -> str:
        value = "" if text is None else str(text)
        value = re.sub(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            "[REDACTED_SECRET]",
            value,
        )
        value = re.sub(
            r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
            r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
            "[REDACTED_SECRET]",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"s" r"k-[A-Za-z0-9][A-Za-z0-9_-]{18,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"g" r"h[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=]{16,}", "[REDACTED_SECRET]", value, flags=re.IGNORECASE)
        return value

DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect scope-recall source/runtime health")
    parser.add_argument("--json", action="store_true", help="emit JSON output (default; accepted for operator convenience)")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT), help="scope-recall source checkout")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path to inspect")
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def plugin_yaml_version(text: str) -> str:
    match = re.search(r"^version:\s*([^\s#]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile_dotenv(hermes_home: Path) -> set[str]:
    env_path = hermes_home / ".env"
    loaded: set[str] = set()
    if not env_path.exists():
        return loaded
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not value:
            continue
        loaded.add(key)
    return loaded


def load_runtime_config(source_root: Path, hermes_home: Path) -> dict[str, Any]:
    profile_env_keys = load_profile_dotenv(hermes_home)
    config: dict[str, Any] = {}
    for path in (source_root / "config.json", hermes_home / "scope-recall" / "config.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(read_text(path))
        except Exception:
            continue
        if isinstance(raw, dict):
            config = deep_merge(config, raw)
    if profile_env_keys:
        config["_profile_env_keys"] = sorted(profile_env_keys)
    return config


def coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def embedder_config_available(config: dict[str, Any], *, profile_env_keys: set[str] | None = None) -> bool:
    profile_env_keys = profile_env_keys or set()
    provider = str(config.get("provider") or "local-hash").strip().lower()
    if provider in {"local-hash", "local-debug"}:
        return True
    if provider in {"openai-compatible", "openai"}:
        if coerce_list(config.get("api_key")):
            return True
        return any(
            os.getenv(name, "").strip() or name in profile_env_keys
            for name in coerce_list(config.get("api_key_env") or "OPENAI_API_KEY")
        )
    if provider in {"sentence-transformers", "local-model", "local-embedding", "huggingface"}:
        try:
            import importlib.util

            return importlib.util.find_spec("sentence_transformers") is not None
        except Exception:
            return False
    return True


def expected_embedder_from_config(config: dict[str, Any]) -> dict[str, Any]:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    if vector_config.get("enabled") is False:
        return {}
    raw_primary = vector_config.get("embedder")
    raw_fallback = vector_config.get("fallback_embedder")
    primary: dict[str, Any] = raw_primary if isinstance(raw_primary, dict) else {}
    fallback: dict[str, Any] = raw_fallback if isinstance(raw_fallback, dict) else {}
    profile_env_keys = set(coerce_list(config.get("_profile_env_keys")))
    source = "embedder"
    selected: dict[str, Any] = dict(primary)
    if selected and not embedder_config_available(selected, profile_env_keys=profile_env_keys) and fallback and embedder_config_available(fallback, profile_env_keys=profile_env_keys):
        selected = dict(fallback)
        source = "fallback_embedder"
    if not selected:
        return {}
    return {
        "source": source,
        "provider": str(selected.get("provider") or ""),
        "model": str(selected.get("model") or ""),
        "dimensions": int(selected.get("dimensions") or 0),
    }


def vector_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    return vector_config.get("enabled") is not False


def vector_backend_from_config(config: dict[str, Any]) -> str:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    backend = str(vector_config.get("backend") or "lancedb").strip().lower()
    return "sqlite-bruteforce" if backend == "sqlite" else backend


def source_report(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    recommendations: list[str] = []

    pyproject_path = source_root / "pyproject.toml"
    plugin_path = source_root / "plugin.yaml"
    readme_path = source_root / "README.md"
    changelog_path = source_root / "CHANGELOG.md"

    pyproject_version = ""
    plugin_version = ""
    readme_versions: list[str] = []
    changelog_has_version = False

    try:
        pyproject_version = tomllib.loads(read_text(pyproject_path))["project"]["version"]
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read pyproject version: {exc}")

    try:
        plugin_version = plugin_yaml_version(read_text(plugin_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read plugin.yaml version: {exc}")

    try:
        readme_versions = re.findall(r"Version `([^`]+)`", read_text(readme_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read README public version: {exc}")

    try:
        changelog_has_version = f"## [{pyproject_version}]" in read_text(changelog_path)
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read CHANGELOG version section: {exc}")

    if pyproject_version and plugin_version and pyproject_version != plugin_version:
        failures.append(f"pyproject/plugin version mismatch: {pyproject_version} != {plugin_version}")
    if pyproject_version and readme_versions != [pyproject_version]:
        failures.append(f"README public versions {readme_versions!r} do not match {pyproject_version}")
    if pyproject_version and not changelog_has_version:
        failures.append(f"CHANGELOG is missing ## [{pyproject_version}] section")

    if failures:
        recommendations.append("Align pyproject.toml, plugin.yaml, README.md, and CHANGELOG.md before release.")

    source = {
        "root": str(source_root),
        "pyproject_version": pyproject_version,
        "plugin_version": plugin_version,
        "readme_public_versions": readme_versions,
        "changelog_has_version": changelog_has_version,
    }
    check = {"ok": not failures, "failures": failures}
    return source, check, recommendations


def sqlite_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not db_path.exists():
        recommendations.append(
            "SQLite truth DB is missing; initialize scope-recall or restore memory.sqlite3 before running scripts/repair.vector_index.py."
        )
        sqlite_payload = {"path": str(db_path), "status": "missing", "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    try:
        conn = sqlite3.connect(db_path)
        try:
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            memory_count = 0
            if "memories" in tables:
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before rebuilding the vector companion.")
        sqlite_payload = {"path": str(db_path), "status": "error", "error": str(exc), "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB error: {exc}"]}, recommendations

    sqlite_payload = {"path": str(db_path), "status": "ready", "memory_count": memory_count, "tables": tables}
    return sqlite_payload, {"ok": True, "failures": []}, recommendations


def journal_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_journal = config.get("journal")
    journal_config: dict[str, Any] = raw_journal if isinstance(raw_journal, dict) else {}
    value = journal_config.get("enabled", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def journal_report(hermes_home: Path, *, enabled: bool = True) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not enabled:
        return {"enabled": False, "status": "disabled"}, {"ok": True, "failures": []}, recommendations
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    required_tables = {"journal_entries", "journal_digest_runs", "memory_journal_sources", "journal_rejections"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin or run journal digest once to create the journal/provenance schema.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                }, {"ok": False, "failures": [f"journal tables missing: {missing}"]}, recommendations

            total_entries = int(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
            unprocessed_entries = int(
                conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
            )
            processed_entries = max(0, total_entries - unprocessed_entries)
            digest_runs = int(conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
            source_links = int(conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0])
            rejections = int(conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0])
            orphan_sources = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_journal_sources AS s
                    LEFT JOIN memories AS m ON m.id = s.memory_id
                    WHERE m.id IS NULL
                    """
                ).fetchone()[0]
            )
            oldest_unprocessed = conn.execute(
                """
                SELECT created_at FROM journal_entries
                WHERE processed_run_id IS NULL OR processed_run_id = ''
                ORDER BY created_at ASC LIMIT 1
                """
            ).fetchone()
            last_run = conn.execute(
                """
                SELECT id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped
                FROM journal_digest_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting journal/provenance status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"journal health error: {exc}"]}, recommendations

    failures: list[str] = []
    if orphan_sources:
        failures.append(f"memory_journal_sources contains {orphan_sources} orphan link(s)")
        recommendations.append("Run hygiene/repair or delete orphan memory_journal_sources before release.")
    if unprocessed_entries:
        recommendations.append("Run scripts/journal-digest.py to promote staged journal entries into durable memories.")

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready" if not failures else "needs_repair",
        "tables": sorted(required_tables),
        "entries": {
            "total": total_entries,
            "processed": processed_entries,
            "unprocessed": unprocessed_entries,
            "oldest_unprocessed": oldest_unprocessed["created_at"] if oldest_unprocessed else "",
        },
        "digest_runs": digest_runs,
        "last_digest_run": dict(last_run) if last_run else {},
        "source_links": source_links,
        "rejections": rejections,
        "orphan_source_links": orphan_sources,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations


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


def lancedb_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
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
        dimensions = vector_dimensions(table)
        search_smoke = run_vector_search_smoke(table, dimensions=dimensions, row_count=row_count)
        payload = {
            "backend": "lancedb",
            "path": str(vector_dir),
            "status": "ready",
            "ready": True,
            "tables": table_names,
            "row_count": row_count,
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
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
            return payload, {"ok": False, "failures": [error]}, recommendations
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
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
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
            return payload, {"ok": False, "failures": [error]}, recommendations
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
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    normalized = "sqlite-bruteforce" if str(backend or "lancedb").strip().lower() == "sqlite" else str(backend or "lancedb").strip().lower()
    if normalized == "sqlite-bruteforce":
        return sqlite_vector_report(hermes_home, expected_embedder=expected_embedder)
    if normalized == "lancedb":
        return lancedb_vector_report(hermes_home, expected_embedder=expected_embedder)
    payload = {"backend": normalized, "status": "unsupported", "ready": False}
    return payload, {"ok": False, "failures": [f"unsupported vector backend: {normalized}"]}, ["Set vector.backend to 'lancedb' or 'sqlite-bruteforce'."]


def experience_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    required_tables = {
        "task_episodes",
        "procedural_playbooks",
        "procedural_playbooks_fts",
        "playbook_versions",
        "experience_runs",
        "reflection_events",
        "fact_freshness",
        "skill_anchors",
        "skill_conflicts",
    }
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, [
            "Initialize scope-recall with the current plugin to create Experience Kernel tables."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin so ensure_schema() creates Experience Kernel tables.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                }, {"ok": False, "failures": [f"experience tables missing: {missing}"]}, recommendations
            playbook_total = int(conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0])
            playbook_by_status = {
                redact_secret_like_text(row["status"]): int(row["count"])
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM procedural_playbooks GROUP BY status")
            }
            run_total = int(conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0])
            run_by_outcome = {
                redact_secret_like_text(row["outcome"]): int(row["count"])
                for row in conn.execute("SELECT outcome, COUNT(*) AS count FROM experience_runs GROUP BY outcome")
            }
            stale_facts = int(conn.execute("SELECT COUNT(*) FROM fact_freshness WHERE status IN ('stale', 'needs_live_check')").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting Experience Kernel status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"experience health error: {exc}"]}, recommendations

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready",
        "tables": sorted(required_tables),
        "playbooks": {"total": playbook_total, "by_status": dict(sorted(playbook_by_status.items()))},
        "runs": {"total": run_total, "by_outcome": dict(sorted(run_by_outcome.items()))},
        "stale_facts": stale_facts,
    }
    return payload, {"ok": True, "failures": []}, recommendations


def disabled_vector_report() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    payload = {"enabled": False, "status": "disabled", "ready": False}
    return payload, {"ok": True, "failures": []}, []


def main() -> int:
    args = parse_args()
    source_root = Path(args.source_root).expanduser().resolve()
    source, source_check, recommendations = source_report(source_root)
    checks: dict[str, Any] = {"source_metadata": source_check}
    payload: dict[str, Any] = {"source": source, "checks": checks, "recommendations": recommendations, "runtime": {}}

    if args.hermes_home:
        hermes_home = Path(args.hermes_home).expanduser().resolve()
        runtime_config = load_runtime_config(source_root, hermes_home)
        expected_embedder = expected_embedder_from_config(runtime_config)
        sqlite_payload, sqlite_check, sqlite_recommendations = sqlite_report(hermes_home)
        journal_payload, journal_check, journal_recommendations = journal_report(hermes_home, enabled=journal_enabled_from_config(runtime_config))
        experience_payload, experience_check, experience_recommendations = experience_report(hermes_home)
        if vector_enabled_from_config(runtime_config):
            backend = vector_backend_from_config(runtime_config)
            vector_payload, vector_check, vector_recommendations = vector_report(hermes_home, expected_embedder=expected_embedder, backend=backend)
        else:
            backend = "disabled"
            vector_payload, vector_check, vector_recommendations = disabled_vector_report()
        vector_payload.setdefault("backend", backend)
        payload["runtime"] = {
            "hermes_home": str(hermes_home),
            "expected_embedder": expected_embedder,
            "vector_backend": backend,
            "sqlite": sqlite_payload,
            "journal": journal_payload,
            "experience": experience_payload,
            "vector": vector_payload,
        }
        checks["sqlite_truth"] = sqlite_check
        checks["journal_provenance"] = journal_check
        checks["experience_kernel"] = experience_check
        checks["vector_companion"] = vector_check
        recommendations.extend(sqlite_recommendations)
        recommendations.extend(journal_recommendations)
        recommendations.extend(experience_recommendations)
        recommendations.extend(vector_recommendations)

    payload["ok"] = all(bool(check.get("ok")) for check in checks.values())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
