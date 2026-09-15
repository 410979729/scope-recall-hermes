#!/usr/bin/env python3
"""Plan, build, or activate a non-destructive Scope Recall vector generation."""

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
PACKAGE_NAME = "scope_recall_vector_migration_runtime"
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

from scope_recall_vector_migration_runtime.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall_vector_migration_runtime.config import load_runtime_config  # noqa: E402
from scope_recall_vector_migration_runtime.embedders import build_embedder  # noqa: E402
from scope_recall_vector_migration_runtime.truth_connection import connect_truth_database  # noqa: E402
from scope_recall_vector_migration_runtime.vector_generation import (  # noqa: E402
    GenerationCompatibilityError,
    GenerationIdentity,
    generation_manifest,
    retire_ready_generation,
)
from scope_recall_vector_migration_runtime.vector_migration import (  # noqa: E402
    build_vector_generation,
    plan_vector_generation,
)
from scope_recall_vector_migration_runtime.vector_store import normalize_vector_backend  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or activate a shadow vector generation")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"))
    parser.add_argument("--generation-id", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--dimensions", type=int, default=0)
    parser.add_argument("--backend", choices=["", "lancedb", "sqlite-bruteforce", "sqlite", "pgvector"], default="")
    parser.add_argument("--metric", default="")
    parser.add_argument("--prompt-profile", default="")
    parser.add_argument("--document-prefix", default=None)
    parser.add_argument("--query-prefix", default=None)
    parser.add_argument("--request-dimensions", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--index-general", action="store_true")
    parser.add_argument("--expected-current", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--activate", action="store_true", help="Activate immediately only when runtime config already identifies this space")
    parser.add_argument("--activate-existing-ready", action="store_true")
    parser.add_argument(
        "--retire-existing-ready",
        action="store_true",
        help="CAS-retire a non-current READY generation; requires --generation-id and --expected-current",
    )
    parser.add_argument(
        "--retirement-reason",
        default="operator-retired-ready-generation",
        help="Audited, sanitized reason stored in generation metadata",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _identity(vector_config: dict[str, Any], retrieval_config: dict[str, Any], embedder: Any, embedder_config: dict[str, Any]) -> GenerationIdentity:
    return GenerationIdentity(
        backend=normalize_vector_backend(vector_config.get("backend") or "lancedb"),
        provider=str(embedder.provider),
        model=str(embedder.model),
        dimensions=int(embedder.dimensions),
        metric=str(retrieval_config.get("metric") or "cosine"),
        prompt_profile=str(embedder_config.get("prompt_profile") or "default-v1"),
        document_prefix=str(embedder_config.get("document_prefix") or ""),
        query_prefix=str(embedder_config.get("query_prefix") or ""),
        request_dimensions=bool(embedder_config.get("request_dimensions", False)),
        table_name=str(vector_config.get("table_name") or "memories"),
    )


def _generation_id(identity: GenerationIdentity) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model = "".join(char if char.isalnum() else "-" for char in identity.model).strip("-")[:40]
    return f"gen-{stamp}-{model}-{identity.fingerprint[:8]}"



def _read_generation_manifest(conn: sqlite3.Connection, generation_id: str) -> dict[str, Any] | None:
    """Read one manifest without initializing or migrating schema."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generations'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        "SELECT * FROM vector_generations WHERE generation_id = ?",
        (generation_id,),
    ).fetchone()
    return dict(row) if row is not None else None



def _read_current_generation_id(conn: sqlite3.Connection) -> str:
    """Read the pointer without initializing or migrating schema."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generation_state'"
    ).fetchone()
    if table is None:
        return ""
    row = conn.execute(
        "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
    ).fetchone()
    return str(row[0] or "") if row else ""



def _plan_generation_retirement(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    expected_current: str,
) -> dict[str, Any]:
    actual_current = _read_current_generation_id(conn)
    if actual_current != expected_current:
        raise GenerationCompatibilityError(
            "current generation CAS conflict before retirement: "
            f"expected {expected_current!r}, actual {actual_current!r}"
        )
    if generation_id == actual_current:
        raise GenerationCompatibilityError("refusing to retire the current generation")
    manifest = _read_generation_manifest(conn, generation_id)
    if manifest is None:
        raise GenerationCompatibilityError(f"generation not found: {generation_id}")
    status = str(manifest.get("status") or "").strip().lower()
    if status != "ready":
        raise GenerationCompatibilityError(
            f"generation {generation_id} is {status!r}, expected 'ready'"
        )
    return {
        "ok": True,
        "status": "planned",
        "dry_run": True,
        "generation_id": generation_id,
        "from_status": "ready",
        "to_status": "retired",
        "current_generation_id": actual_current,
        "storage_path": str(manifest.get("storage_path") or ""),
        "physical_storage_retained": True,
        "writes": [],
    }



def _target_config(config: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    vector_config = dict(config.get("vector") or {})
    retrieval_config = dict(config.get("retrieval") or {})
    embedder_config = dict(vector_config.get("embedder") or {})
    if args.backend:
        vector_config["backend"] = normalize_vector_backend(args.backend)
    if args.provider:
        embedder_config["provider"] = args.provider
    if args.model:
        embedder_config["model"] = args.model
    if args.dimensions:
        embedder_config["dimensions"] = args.dimensions
    if args.prompt_profile:
        embedder_config["prompt_profile"] = args.prompt_profile
    if args.document_prefix is not None:
        embedder_config["document_prefix"] = args.document_prefix
    if args.query_prefix is not None:
        embedder_config["query_prefix"] = args.query_prefix
    if args.request_dimensions:
        embedder_config["request_dimensions"] = True
    if args.metric:
        retrieval_config["metric"] = args.metric
    vector_config["embedder"] = embedder_config
    return vector_config, retrieval_config, embedder_config


def main() -> int:
    args = parse_args()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not db_path.is_file():
        print(
            json.dumps(
                {"ok": False, "status": "blocked", "error": f"SQLite truth DB not found: {db_path}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if args.retire_existing_ready:
        generation_id = str(args.generation_id or "").strip()
        expected_current = str(args.expected_current or "").strip()
        if not generation_id or not expected_current:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "blocked",
                        "error": "--retire-existing-ready requires --generation-id and --expected-current",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        if args.activate or args.activate_existing_ready:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "blocked",
                        "generation_id": generation_id,
                        "error": "retirement cannot be combined with activation",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        if args.apply and args.dry_run:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "blocked",
                        "generation_id": generation_id,
                        "error": "choose either retirement dry-run or --apply, not both",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        conn = connect_truth_database(db_path, mode="rw" if args.apply else "ro")
        try:
            plan = _plan_generation_retirement(
                conn,
                generation_id=generation_id,
                expected_current=expected_current,
            )
            if not args.apply:
                print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            retired = retire_ready_generation(
                conn,
                generation_id,
                expected_current=expected_current,
                reason=str(args.retirement_reason or ""),
            )
            conn.commit()
            payload = {
                **plan,
                "status": "retired",
                "dry_run": False,
                "retired_at": str(retired.get("updated_at") or ""),
                "writes": ["vector_generations.status", "vector_generations.metadata"],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        except Exception as exc:
            conn.rollback()
            safe_error = sanitize_report_text(str(exc)) or "vector generation retirement failed"
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "blocked",
                        "generation_id": generation_id,
                        "error": safe_error,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        finally:
            conn.close()
    config = load_runtime_config(PLUGIN_ROOT, storage_dir)
    runtime_vector_config = dict(config.get("vector") or {})
    runtime_retrieval_config = dict(config.get("retrieval") or {})
    vector_config, retrieval_config, embedder_config = _target_config(config, args)
    embedder = build_embedder(embedder_config)
    target_identity = _identity(vector_config, retrieval_config, embedder, embedder_config)
    generation_id = str(args.generation_id or _generation_id(target_identity))

    writes_truth = bool(args.apply or args.activate_existing_ready)
    conn = connect_truth_database(db_path, mode="rw" if writes_truth else "ro")
    try:
        if not args.apply and not args.activate_existing_ready:
            payload = plan_vector_generation(
                storage_dir,
                conn,
                generation_id=generation_id,
                identity=target_identity,
                index_general=bool(args.index_general or vector_config.get("index_general", False)),
            )
            payload["embedder_available"] = bool(embedder.is_available())
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        current_id = _read_current_generation_id(conn)
        expected_current = str(args.expected_current or current_id)
        if args.activate or args.activate_existing_ready:
            runtime_embedder_config = dict(runtime_vector_config.get("embedder") or {})
            runtime_embedder = build_embedder(runtime_embedder_config)
            runtime_identity = _identity(
                runtime_vector_config,
                runtime_retrieval_config,
                runtime_embedder,
                runtime_embedder_config,
            )
            manifest = generation_manifest(conn, generation_id)
            activation_identity = target_identity
            if manifest is not None:
                activation_identity = GenerationIdentity(
                    backend=str(manifest["backend"]),
                    provider=str(manifest["provider"]),
                    model=str(manifest["model"]),
                    dimensions=int(manifest["dimensions"]),
                    metric=str(manifest["metric"]),
                    prompt_profile=str(manifest["prompt_profile"]),
                    document_prefix=str(manifest.get("document_prefix") or ""),
                    query_prefix=str(manifest.get("query_prefix") or ""),
                    request_dimensions=bool(manifest.get("request_dimensions")),
                    table_name=str(manifest["table_name"]),
                    schema_version=int(manifest["schema_version"]),
                )
            if runtime_identity.fingerprint != activation_identity.fingerprint:
                raise GenerationCompatibilityError(
                    "runtime config does not identify the target generation; atomically update the runtime embedder config before activation"
                )
        if not args.activate_existing_ready and not embedder.is_available():
            raise RuntimeError(f"target embedder {embedder.provider}/{embedder.model} is unavailable")
        payload = build_vector_generation(
            storage_dir,
            conn,
            generation_id=generation_id,
            identity=target_identity,
            embedder=embedder,
            index_general=bool(args.index_general or vector_config.get("index_general", False)),
            batch_size=max(1, int(args.batch_size or 50)),
            activate=bool(args.activate or args.activate_existing_ready),
            expected_current=expected_current,
            activate_existing_ready=bool(args.activate_existing_ready),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        conn.rollback()
        safe_error = sanitize_report_text(str(exc)) or "vector generation operation failed"
        print(
            json.dumps(
                {"ok": False, "status": "blocked", "generation_id": generation_id, "error": safe_error},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
