#!/usr/bin/env python3
"""Inspect scope-recall source metadata and runtime storage health.

The doctor is intentionally read-only. It treats SQLite as the truth layer and
LanceDB as an optional rebuildable companion, then emits a compact JSON report
that operators can use before running repair or release checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tomllib
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect scope-recall source/runtime health")
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



def load_runtime_config(source_root: Path, hermes_home: Path) -> dict[str, Any]:
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
    return config



def coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []



def embedder_config_available(config: dict[str, Any]) -> bool:
    provider = str(config.get("provider") or "local-hash").strip().lower()
    if provider in {"local-hash", "local-debug"}:
        return True
    if provider in {"openai-compatible", "openai"}:
        if coerce_list(config.get("api_key")):
            return True
        return any(os.getenv(name, "").strip() for name in coerce_list(config.get("api_key_env") or "OPENAI_API_KEY"))
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
    source = "embedder"
    selected: dict[str, Any] = dict(primary)
    if selected and not embedder_config_available(selected) and fallback and embedder_config_available(fallback):
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
        recommendations.append("Repair or restore the SQLite truth DB before rebuilding the LanceDB companion.")
        sqlite_payload = {"path": str(db_path), "status": "error", "error": str(exc), "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB error: {exc}"]}, recommendations

    sqlite_payload = {"path": str(db_path), "status": "ready", "memory_count": memory_count, "tables": tables}
    return sqlite_payload, {"ok": True, "failures": []}, recommendations


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



def vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    vector_dir = hermes_home / "scope-recall" / "lancedb"
    if not vector_dir.exists():
        recommendations.append("LanceDB companion directory is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"path": str(vector_dir), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"LanceDB directory not found: {vector_dir}"]}, recommendations

    try:
        import lancedb  # type: ignore

        db = lancedb.connect(str(vector_dir))
        table_names = lancedb_table_names(db)
        if "memories" not in table_names:
            recommendations.append("LanceDB table 'memories' is missing; run scripts/repair.vector_index.py.")
            payload = {"path": str(vector_dir), "status": "needs_repair", "ready": False, "tables": table_names}
            return payload, {"ok": False, "failures": ["LanceDB table 'memories' is missing"]}, recommendations
        table = db.open_table("memories")
        row_count = int(table.count_rows())
        dimensions = vector_dimensions(table)
        search_smoke = run_vector_search_smoke(table, dimensions=dimensions, row_count=row_count)
        payload = {
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
        payload = {"path": str(vector_dir), "status": "needs_repair", "ready": False, "error": str(exc)}
        return payload, {"ok": False, "failures": [f"LanceDB error: {exc}"]}, recommendations


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
        vector_payload, vector_check, vector_recommendations = vector_report(hermes_home, expected_embedder=expected_embedder)
        payload["runtime"] = {
            "hermes_home": str(hermes_home),
            "expected_embedder": expected_embedder,
            "sqlite": sqlite_payload,
            "vector": vector_payload,
        }
        checks["sqlite_truth"] = sqlite_check
        checks["lancedb_companion"] = vector_check
        recommendations.extend(sqlite_recommendations)
        recommendations.extend(vector_recommendations)

    payload["ok"] = all(bool(check.get("ok")) for check in checks.values())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
