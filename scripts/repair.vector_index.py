#!/usr/bin/env python3
"""Rebuild the scope-recall vector companion from SQLite truth.

This script is intentionally conservative:
- SQLite remains the authority.
- Existing vector companion data is backed up before rebuild unless --no-backup is passed.
- The script only touches the configured companion path under $HERMES_HOME/scope-recall/ by default.

Run it after stopping/restarting Hermes if you need a clean companion index for
release-grade storage hygiene or after changing embedder dimensions/backends.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_repair_runtime"
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

from scope_recall_repair_runtime.config import load_runtime_config  # noqa: E402
from scope_recall_repair_runtime.embedders import build_embedder  # noqa: E402
from scope_recall_repair_runtime.sqlite_vector_store import SQLiteBruteForceVectorStore  # type: ignore[import-not-found]  # noqa: E402
from scope_recall_repair_runtime.vector_store import LanceVectorStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild scope-recall vector companion from SQLite truth")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--backend", default="", choices=["", "lancedb", "sqlite-bruteforce", "sqlite"], help="Override vector.backend from config")
    parser.add_argument("--dry-run", action="store_true", help="Inspect planned rebuild without writing vector companion data")
    parser.add_argument("--no-backup", action="store_true", help="Do not copy the old vector companion before rebuild")
    return parser.parse_args()


def load_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, scope_id, source, target, content, summary, updated_at FROM memories ORDER BY updated_at ASC"
        ).fetchall()
    finally:
        conn.close()


def vector_text(row: sqlite3.Row) -> str:
    return f"{row['summary']}\n{row['content']}".strip()


def choose_embedder(config: dict[str, Any]):
    vector_config = dict(config.get("vector") or {})
    embedder = build_embedder(dict(vector_config.get("embedder") or {}))
    if not embedder.is_available() and vector_config.get("fallback_embedder"):
        fallback = build_embedder(dict(vector_config.get("fallback_embedder") or {}))
        if fallback.is_available():
            embedder = fallback
    if not embedder.is_available():
        raise RuntimeError(f"embedder {embedder.provider} is not available")
    if embedder.provider == "sentence-transformers" and hasattr(embedder, "_model_or_raise"):
        embedder._model_or_raise()
    return embedder


def normalize_backend(value: str) -> str:
    backend = str(value or "lancedb").strip().lower()
    return "sqlite-bruteforce" if backend == "sqlite" else backend


def backend_from_config(config: dict[str, Any], override: str = "") -> str:
    if override:
        return normalize_backend(override)
    vector_config = dict(config.get("vector") or {})
    return normalize_backend(str(vector_config.get("backend") or "lancedb"))


def vector_target(storage_dir: Path, backend: str) -> Path:
    if backend == "lancedb":
        return storage_dir / "lancedb"
    if backend == "sqlite-bruteforce":
        return storage_dir / "vector.sqlite3"
    raise RuntimeError(f"unsupported vector backend: {backend}")


def sqlite_sidecar_paths(db_path: Path) -> list[Path]:
    return [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]


def backup_existing(target: Path, storage_dir: Path, backend: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S")
    backup_root = storage_dir / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    if backend == "lancedb":
        if not target.exists():
            return ""
        backup = backup_root / f"lancedb.pre-rebuild.{stamp}"
        shutil.copytree(target, backup)
        return str(backup)
    if backend == "sqlite-bruteforce":
        existing = [path for path in sqlite_sidecar_paths(target) if path.exists()]
        if not existing:
            return ""
        backup = backup_root / f"sqlite-vector.pre-rebuild.{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        for path in existing:
            shutil.copy2(path, backup / path.name)
        return str(backup)
    raise RuntimeError(f"unsupported vector backend: {backend}")


def remove_existing(target: Path, backend: str) -> None:
    if backend == "lancedb":
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        return
    if backend == "sqlite-bruteforce":
        target.parent.mkdir(parents=True, exist_ok=True)
        for path in sqlite_sidecar_paths(target):
            path.unlink(missing_ok=True)
        return
    raise RuntimeError(f"unsupported vector backend: {backend}")


def open_store(target: Path, *, backend: str, table_name: str, dimensions: int, metric: str):
    if backend == "lancedb":
        store = LanceVectorStore(target, table_name=table_name, dimensions=dimensions, metric=metric)
        if not store.is_available():
            raise RuntimeError("lancedb/pyarrow is not installed")
        store.open()
        return store
    if backend == "sqlite-bruteforce":
        store = SQLiteBruteForceVectorStore(target, table_name=table_name, dimensions=dimensions, metric=metric)
        store.open()
        return store
    raise RuntimeError(f"unsupported vector backend: {backend}")


def main() -> int:
    args = parse_args()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"

    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"SQLite truth DB not found: {db_path}"}, ensure_ascii=False))
        return 1

    config = load_runtime_config(PLUGIN_ROOT, storage_dir)
    vector_config = dict(config.get("vector") or {})
    backend = backend_from_config(config, args.backend)
    table_name = str(vector_config.get("table_name") or "memories")
    metric = str((config.get("retrieval") or {}).get("metric") or "cosine")
    rows = load_rows(db_path)
    if not bool(vector_config.get("index_general", False)):
        rows = [row for row in rows if str(row["target"]) != "general"]
    embedder = choose_embedder(config)
    target = vector_target(storage_dir, backend)

    plan = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "hermes_home": str(hermes_home),
        "sqlite_db": str(db_path),
        "vector_backend": backend,
        "vector_path": str(target),
        "table": table_name,
        "rows": len(rows),
        "embedder": embedder.describe(),
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    backup_path = ""
    if not args.no_backup:
        backup_path = backup_existing(target, storage_dir, backend)

    remove_existing(target, backend)

    store = open_store(target, backend=backend, table_name=table_name, dimensions=embedder.dimensions, metric=metric)
    try:
        payload: list[dict[str, Any]] = []
        batch_size = 100
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            vectors = embedder.embed_texts(vector_text(row) for row in batch)
            for row, vector in zip(batch, vectors):
                payload.append(
                    {
                        "id": row["id"],
                        "scope_id": row["scope_id"],
                        "source": row["source"],
                        "target": row["target"],
                        "content": row["content"],
                        "summary": row["summary"],
                        "updated_at": row["updated_at"],
                        "vector": vector,
                    }
                )
        if payload:
            store.upsert_records(payload)
        counts = store.audit_counts()
    finally:
        store.close()

    plan.update({"backup": backup_path, "audit": counts})
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
