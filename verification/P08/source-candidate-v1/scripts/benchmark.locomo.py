#!/usr/bin/env python3
"""Run a reproducible, resumable Scope Recall LoCoMo benchmark.

The runner creates one isolated Hermes home per conversation, uses the current
source tree directly, records retrieval evidence and gold evidence recall, and
never counts model/provider failures as wrong answers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from locomo_benchmark_lib import (  # noqa: E402
    build_answer_prompt,
    build_judge_prompt,
    build_query_planner_prompt,
    build_query_variants,
    completed_question_ids,
    conversation_records,
    extract_answer,
    format_evidence,
    managed_provider,
    parse_judgment,
    parse_query_plan,
    question_rows,
    render_record,
    retrieval_metrics,
    score_results,
    session_chunks,
    stratified_question_sample,
    store_chunk,
    store_record,
    summarize_provider_stats,
    validate_ingestion_receipt,
    validate_query_plan_artifacts,
    validate_result_artifacts,
    validate_retrieval_artifacts,
)
from locomo_model_client import CodexModelClient, ModelRouteDriftError  # noqa: E402

SCHEMA_VERSION = "scope_recall_locomo_run.v4"
REPORT_SCHEMA_VERSION = "scope_recall_locomo_report.v4"
MODEL_ROUTE_SCHEMA_VERSION = "scope_recall_locomo_model_route.v2"
MODEL_MAX_ATTEMPTS = 6
CANONICAL_LOCOMO_DATASET_SHA256 = (
    "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
)
CANONICAL_LOCOMO_QUESTION_IDS_SHA256 = (
    "58da029a7ce705dbc88e9962a3b3c3726c84e094df1a8518385c04deac262863"
)
CANONICAL_LOCOMO_CATEGORY_COUNTS = {1: 282, 2: 321, 3: 96, 4: 841}
CANONICAL_LOCOMO_RETRIEVAL_METRIC_QUESTIONS = 1536
_HEARTBEAT_WRITE_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Scope Recall against LoCoMo with resumable evidence receipts"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to a LoCoMo JSON dataset.",
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--auth-path",
        type=Path,
        default=None,
        help="Existing Hermes auth store; required only for model-backed phases.",
    )
    parser.add_argument(
        "--hermes-agent-root",
        type=Path,
        required=True,
        help="Path to the Hermes Agent source root used by this benchmark.",
    )
    parser.add_argument(
        "--phase",
        choices=("all", "retrieve", "evaluate", "report"),
        default="all",
    )
    parser.add_argument("--answer-model", default="gpt-5.3-codex-spark")
    parser.add_argument("--judge-model", default="gpt-5.4-mini")
    parser.add_argument(
        "--query-planner-model",
        default="",
        help="Optional fast model for search-only query expansion.",
    )
    parser.add_argument("--planner-categories", default="1,2,3")
    parser.add_argument("--categories", default="1,2,3,4")
    parser.add_argument("--samples", default="")
    parser.add_argument("--per-category", type=int, default=0)
    parser.add_argument("--max-questions", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--sample-workers",
        type=int,
        default=1,
        help="Parallel isolated conversation homes during ingestion/retrieval.",
    )
    parser.add_argument("--model-rounds", type=int, default=3)
    parser.add_argument("--retrieval-limit", type=int, default=20)
    parser.add_argument("--evidence-max-chars", type=int, default=50000)
    parser.add_argument(
        "--ingest-mode",
        choices=("atomic", "chunks", "both"),
        default="chunks",
    )
    parser.add_argument("--chunk-size", type=int, default=4)
    parser.add_argument("--chunk-overlap", type=int, default=1)
    parser.add_argument(
        "--embedder", choices=("local-hash", "gemini"), default="local-hash"
    )
    parser.add_argument("--no-query-variants", action="store_true")
    parser.add_argument(
        "--evidence-mode", choices=("retrieved", "oracle"), default="retrieved"
    )
    parser.add_argument("--model-timeout", type=float, default=90.0)
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
    temporary.write_bytes(json_bytes(payload))
    os.replace(temporary, path)


def write_heartbeat(run_dir: Path, payload: dict[str, Any]) -> None:
    """Serialize the shared heartbeat replace across sample workers."""

    with _HEARTBEAT_WRITE_LOCK:
        write_json_atomic(run_dir / "heartbeat.json", payload)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="strict")
    lines = raw.splitlines()
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                break
            raise
        if not isinstance(payload, dict):
            raise ValueError(f"non-object JSONL row in {path} at line {index + 1}")
        rows.append(payload)
    return rows


def load_source_package(hermes_agent_root: Path) -> None:
    """Load this checkout as ``scope_recall`` without installing it."""

    if str(hermes_agent_root) not in sys.path:
        sys.path.insert(0, str(hermes_agent_root))
    existing = sys.modules.get("scope_recall")
    if existing is not None:
        loaded_from = Path(str(getattr(existing, "__file__", ""))).resolve()
        if loaded_from == (ROOT / "__init__.py").resolve():
            return
        raise RuntimeError(f"scope_recall was already imported from {loaded_from}")
    spec = importlib.util.spec_from_file_location(
        "scope_recall",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load current Scope Recall source tree")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)


def external_run_directory(path: Path) -> Path:
    """Resolve a benchmark output root and reject source-tree pollution."""

    resolved = Path(path).resolve()
    source_root = ROOT.resolve()
    if resolved == source_root or source_root in resolved.parents:
        raise ValueError("--run-dir must be outside the Scope Recall source tree")
    return resolved


def _framed_digest(parts: list[tuple[bytes, bytes]]) -> str:
    """Hash labeled byte fields without delimiter ambiguity."""

    digest = hashlib.sha256()
    digest.update(b"scope-recall-source-epoch.v2\0")
    for label, payload in parts:
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _worktree_snapshot(
    source_root: Path,
    raw_paths: bytes,
) -> tuple[int, str]:
    """Hash raw worktree bytes, modes, missing files, and symlink targets."""

    records: list[tuple[bytes, bytes]] = []
    for path_bytes in sorted(path for path in raw_paths.split(b"\0") if path):
        relative = path_bytes.decode("utf-8", errors="surrogateescape")
        candidate = source_root / relative
        if candidate.is_symlink():
            kind = b"symlink"
            payload = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            mode = b"120000"
        elif candidate.is_file():
            kind = b"file"
            payload = candidate.read_bytes()
            mode = b"100755" if os.lstat(candidate).st_mode & 0o111 else b"100644"
        elif candidate.exists():
            # Gitlinks are represented by their index entry. The worktree side
            # records the directory presence without walking another repository.
            kind = b"directory"
            payload = b""
            mode = b"160000"
        else:
            kind = b"missing"
            payload = b""
            mode = b"000000"
        record = _framed_digest(
            [
                (b"path", path_bytes),
                (b"kind", kind),
                (b"mode", mode),
                (b"payload", payload),
            ]
        ).encode("ascii")
        records.append((path_bytes, record))
    return len(records), _framed_digest(records)


def dependency_source_epoch(root: Path) -> dict[str, Any]:
    """Return a path/config-independent fingerprint of every candidate byte layer."""

    source_root = Path(root).resolve()

    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments],
            cwd=source_root,
            check=True,
            capture_output=True,
        ).stdout

    try:
        head = git("rev-parse", "HEAD").decode().strip()
        head_tree = git("rev-parse", "HEAD^{tree}").decode().strip()
        index_entries = git("ls-files", "--stage", "-z")
        tracked_paths = git("ls-files", "-z")
        untracked_paths = git("ls-files", "-z", "--others", "--exclude-standard")
        status = git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
        )
        tracked_count, tracked_worktree_sha256 = _worktree_snapshot(
            source_root,
            tracked_paths,
        )
        untracked_count, untracked_sha256 = _worktree_snapshot(
            source_root,
            untracked_paths,
        )
    except Exception as exc:
        return {"error": type(exc).__name__}

    index_sha256 = hashlib.sha256(index_entries).hexdigest()
    candidate_sha256 = _framed_digest(
        [
            (b"head", head.encode("ascii", errors="replace")),
            (b"head_tree", head_tree.encode("ascii", errors="replace")),
            (b"index", bytes.fromhex(index_sha256)),
            (b"tracked_worktree", bytes.fromhex(tracked_worktree_sha256)),
            (b"untracked", bytes.fromhex(untracked_sha256)),
        ]
    )
    return {
        "head": head,
        "head_tree": head_tree,
        "dirty": bool(status),
        "index_count": len([row for row in index_entries.split(b"\0") if row]),
        "index_sha256": index_sha256,
        "tracked_count": tracked_count,
        "tracked_worktree_sha256": tracked_worktree_sha256,
        "untracked_count": untracked_count,
        "untracked_sha256": untracked_sha256,
        "candidate_sha256": candidate_sha256,
    }


def source_epoch() -> dict[str, Any]:
    """Bind a benchmark run to committed, modified, and untracked source bytes."""

    return dependency_source_epoch(ROOT)


def benchmark_config(embedder: str, retrieval_limit: int) -> dict[str, Any]:
    vector_embedder: dict[str, Any]
    if embedder == "gemini":
        vector_embedder = {
            "provider": "openai-compatible",
            "dimensions": 3072,
            "model": "gemini-embedding-001",
            "api_key_env": ["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"],
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "allow_insecure_endpoint": False,
            "request_dimensions": False,
        }
    else:
        vector_embedder = {
            "provider": "local-hash",
            "dimensions": 256,
            "model": "hash-v1",
            "allow_insecure_endpoint": False,
        }
    candidate_pool = max(80, int(retrieval_limit) * 4)
    return {
        "auto_recall": False,
        "auto_capture": False,
        "capture_raw_user": False,
        "enable_tools": True,
        "maintenance_tools_enabled": False,
        "relation_extraction_enabled": False,
        "journal": {
            "enabled": False,
            "background_digest_enabled": False,
            "digest_on_session_end": False,
        },
        "retrieval": {
            "mode": "hybrid",
            "top_k": int(retrieval_limit),
            "candidate_pool": candidate_pool,
            "min_score": 0.02,
            "vector_min_score": 0.02,
            "vector_only_min_score": 0.20,
            "include_general": "same-scope",
            "general_weight": 1.0,
            "metadata_weight": 0.04,
            "entity_scope_filter_enabled": True,
            "fusion_strategy": "rrf",
            "lexical_weight": 0.45,
            "vector_weight": 0.55,
            "rrf_weight": 0.18,
            "bm25_weight": 0.15,
        },
        "vector": {
            "enabled": True,
            "backend": "lancedb",
            "fallback_backend": "sqlite-bruteforce",
            "table_name": "memories",
            "top_k": candidate_pool,
            "sync_mode": "incremental",
            "index_general": False,
            "startup_reconcile_enabled": False,
            "embedder": vector_embedder,
            "fallback_embedder": {
                "provider": "local-hash",
                "dimensions": 256,
                "model": "hash-v1",
                "allow_insecure_endpoint": False,
            },
        },
    }


def selected_questions(
    dataset: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    categories = {
        int(value.strip())
        for value in str(args.categories).split(",")
        if value.strip()
    }
    samples = {
        value.strip() for value in str(args.samples).split(",") if value.strip()
    }
    rows = [
        row
        for row in question_rows(dataset)
        if int(row["category_id"]) in categories
        and (not samples or str(row["sample_id"]) in samples)
    ]
    if args.per_category > 0:
        rows = stratified_question_sample(rows, per_category=args.per_category)
    if args.max_questions > 0:
        rows = rows[: args.max_questions]
    return rows


def ensure_manifest(
    *,
    args: argparse.Namespace,
    dataset: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = args.run_dir / "manifest.json"
    config_digest = hashlib.sha256(json_bytes(config)).hexdigest()
    source = source_epoch()
    if source.get("error"):
        raise RuntimeError(
            f"unable to fingerprint Scope Recall source ({source['error']})"
        )
    hermes_source = dependency_source_epoch(args.hermes_agent_root)
    if hermes_source.get("error"):
        raise RuntimeError(
            f"unable to fingerprint Hermes Agent dependency ({hermes_source['error']})"
        )
    expected = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "source": source,
        "dependencies": {"hermes_agent": hermes_source},
        "dataset": {
            "name": args.dataset.name,
            "bytes": args.dataset.stat().st_size,
            "sha256": sha256_file(args.dataset),
            "sample_count": len(dataset),
        },
        "question_count": len(questions),
        "question_ids_sha256": hashlib.sha256(
            "\n".join(row["question_id"] for row in questions).encode("utf-8")
        ).hexdigest(),
        "models": {
            "answerer": args.answer_model,
            "judge": args.judge_model,
            "query_planner": args.query_planner_model,
        },
        "model_execution": {
            "timeout_seconds": float(args.model_timeout),
            "max_attempts": MODEL_MAX_ATTEMPTS,
            "workers": max(1, int(args.workers)),
            "model_rounds": max(1, int(args.model_rounds)),
            "route_receipt": "model-route.json",
        },
        "strategy": {
            "categories": args.categories,
            "samples": args.samples,
            "per_category": args.per_category,
            "max_questions": args.max_questions,
            "ingest_mode": args.ingest_mode,
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
            "embedder": args.embedder,
            "query_variants": not args.no_query_variants,
            "planner_categories": args.planner_categories,
            "retrieval_limit": args.retrieval_limit,
            "evidence_max_chars": args.evidence_max_chars,
            "evidence_mode": args.evidence_mode,
            "sample_workers": args.sample_workers,
        },
        "config_sha256": config_digest,
        "auth_store_configured": args.auth_path is not None,
        "auth_secret_copied": False,
    }
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        comparable_existing = dict(existing)
        comparable_expected = dict(expected)
        comparable_existing.pop("created_at", None)
        comparable_expected.pop("created_at", None)
        if comparable_existing != comparable_expected:
            raise RuntimeError(
                "run manifest does not match requested source/data/models/strategy; use a new run directory"
            )
        return existing
    write_json_atomic(manifest_path, expected)
    write_json_atomic(args.run_dir / "benchmark-config.json", config)
    return expected


def ensure_model_route_receipt(args: argparse.Namespace) -> dict[str, Any]:
    """Bind model-backed phases to one secret-free transport/account identity."""

    if args.auth_path is None:
        raise RuntimeError("model route receipt requires --auth-path")
    manifest = json.loads((args.run_dir / "manifest.json").read_text(encoding="utf-8"))
    client = CodexModelClient(
        args.auth_path,
        timeout=args.model_timeout,
        max_attempts=MODEL_MAX_ATTEMPTS,
    )
    model_execution = dict(manifest.get("model_execution") or {})
    expected = {
        "schema_version": MODEL_ROUTE_SCHEMA_VERSION,
        "created_at": now_iso(),
        "source_candidate_sha256": str(
            (manifest.get("source") or {}).get("candidate_sha256") or ""
        ),
        "models": dict(manifest.get("models") or {}),
        "timeout_seconds": float(args.model_timeout),
        "max_attempts": MODEL_MAX_ATTEMPTS,
        "workers": int(model_execution.get("workers") or 0),
        "model_rounds": int(model_execution.get("model_rounds") or 0),
        "route": client.route_fingerprint(),
    }
    path = args.run_dir / "model-route.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        comparable_existing = dict(existing)
        comparable_expected = dict(expected)
        comparable_existing.pop("created_at", None)
        comparable_expected.pop("created_at", None)
        if comparable_existing != comparable_expected:
            raise RuntimeError(
                "model route changed within the benchmark run; use a new run directory"
            )
        return existing
    write_json_atomic(path, expected)
    return expected


def reset_incomplete_home(home: Path, run_dir: Path) -> None:
    if not home.exists():
        return
    if run_dir.resolve() not in home.resolve().parents:
        raise RuntimeError(f"refusing to reset home outside run directory: {home}")
    shutil.rmtree(home)


def merge_query_variants(*groups: list[str], limit: int = 7) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            query = " ".join(str(raw or "").strip().split())[:1000]
            normalized = query.casefold()
            if not query or normalized in seen:
                continue
            seen.add(normalized)
            output.append(query)
            if len(output) >= max(1, min(7, int(limit))):
                return output
    return output


def prepare_query_plans(
    *,
    questions: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, list[str]]:
    """Generate resumable search-only plans with deterministic fallback."""

    if not args.query_planner_model or args.no_query_variants:
        return {}
    planner_categories = {
        int(value.strip())
        for value in str(args.planner_categories).split(",")
        if value.strip()
    }
    path = args.run_dir / "query-plans.jsonl"
    existing_rows = load_jsonl(path)
    existing = validate_query_plan_artifacts(
        questions,
        existing_rows,
        planner_model=args.query_planner_model,
        planner_categories=planner_categories,
        require_complete=False,
    )
    pending = [
        question
        for question in questions
        if int(question["category_id"]) in planner_categories
        and question["question_id"] not in existing
    ]
    route_receipt = ensure_model_route_receipt(args)
    client = CodexModelClient(
        args.auth_path,
        timeout=args.model_timeout,
        max_attempts=MODEL_MAX_ATTEMPTS,
        expected_route=(route_receipt.get("route") or None),
    )

    def plan_one(question: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        model_valid = False
        error = ""
        variants: list[str] = []
        try:
            raw = client.complete(
                model=args.query_planner_model,
                system="Return only the requested JSON search plan. Do not answer.",
                user=build_query_planner_prompt(
                    question=question["question"],
                    category=question["category"],
                ),
            )
            variants = parse_query_plan(
                raw,
                primary_query=question["question"],
                max_variants=5,
            )
            model_valid = bool(variants)
            if not model_valid:
                error = "planner output contained no valid variants"
        except ModelRouteDriftError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
        if not variants:
            variants = build_query_variants(
                question["question"], category=question["category"]
            )
        return {
            "question_id": question["question_id"],
            "sample_id": question["sample_id"],
            "category_id": question["category_id"],
            "model": args.query_planner_model,
            "model_valid": model_valid,
            "fallback_used": not model_valid,
            "variants": variants,
            "error": error,
            "latency_seconds": round(time.perf_counter() - started, 6),
            "completed_at": now_iso(),
        }

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(plan_one, question) for question in pending]
            completed = 0
            for future in as_completed(futures):
                row = future.result()
                append_jsonl(path, row)
                existing[row["question_id"]] = row
                completed += 1
                write_heartbeat(
                    args.run_dir,
                    {
                        "phase": "query_plan",
                        "completed": completed,
                        "pending": len(pending),
                        "at": now_iso(),
                    },
                )
    return {
        question_id: list(row.get("variants") or [])
        for question_id, row in existing.items()
    }


def provider_kwargs(home: Path, sample_id: str) -> dict[str, Any]:
    return {
        "session_id": f"locomo-{sample_id}",
        "hermes_home": str(home),
        "platform": "cli",
        "user_id": "locomo-bench-user",
        "chat_id": sample_id,
        "agent_identity": "scope-recall-locomo-bench",
        "agent_workspace": "locomo-bench",
        "agent_context": "primary",
    }


def store_sample(
    *,
    sample: dict[str, Any],
    home: Path,
    receipt_path: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        truth_database = home / "scope-recall" / "memory.sqlite3"
        if receipt.get("complete") is True and truth_database.is_file():
            write_json_atomic(home / "scope-recall" / "config.json", config)
            return receipt
    reset_incomplete_home(home, args.run_dir)
    config_path = home / "scope-recall" / "config.json"
    write_json_atomic(config_path, config)

    from scope_recall.provider import ScopeRecallMemoryProvider

    records = conversation_records(sample)
    memory_map: dict[str, dict[str, Any]] = {}
    stats: dict[str, Any] = {}
    provider = ScopeRecallMemoryProvider()
    with managed_provider(provider, **provider_kwargs(home, str(sample["sample_id"]))):
        if args.ingest_mode in {"atomic", "both"}:
            for event_order, record in enumerate(records, 1):
                memory_id = store_record(provider, record)
                memory_map[memory_id] = {
                    "kind": "atomic",
                    "dia_ids": [record["dia_id"]],
                    "event_order": event_order,
                    "event_time": record["event_time"],
                }
        if args.ingest_mode in {"chunks", "both"}:
            for chunk in session_chunks(
                records,
                chunk_size=args.chunk_size,
                overlap=args.chunk_overlap,
            ):
                memory_id = store_chunk(provider, chunk)
                memory_map[memory_id] = {
                    "kind": "chunk",
                    "dia_ids": list(chunk["dia_ids"]),
                    "event_order": int(chunk["event_order"]),
                    "event_time": str(chunk["event_time"]),
                }
        raw_stats = provider.handle_tool_call("scope_recall_stats", {})
        decoded_stats = json.loads(raw_stats)
        if isinstance(decoded_stats, dict):
            stats = summarize_provider_stats(decoded_stats)

    receipt = {
        "schema_version": "scope_recall_locomo_ingestion.v2",
        "complete": True,
        "completed_at": now_iso(),
        "sample_id": str(sample["sample_id"]),
        "ingest_mode": args.ingest_mode,
        "source_turns": len(records),
        "stored_memories": len(memory_map),
        "memory_map": memory_map,
        "stats": stats,
    }
    write_json_atomic(receipt_path, receipt)
    return receipt


def retrieve_sample(
    *,
    sample: dict[str, Any],
    questions: list[dict[str, Any]],
    home: Path,
    receipt: dict[str, Any],
    output_path: Path,
    query_plans: dict[str, list[str]],
    args: argparse.Namespace,
) -> None:
    existing = load_jsonl(output_path)
    done = set(
        validate_retrieval_artifacts(
            questions,
            existing,
            require_complete=False,
        )
    )
    pending = [row for row in questions if row["question_id"] not in done]
    if not pending:
        return

    from scope_recall.provider import ScopeRecallMemoryProvider

    provider = ScopeRecallMemoryProvider()
    with managed_provider(provider, **provider_kwargs(home, str(sample["sample_id"]))):
        current_stats = json.loads(
            provider.handle_tool_call("scope_recall_stats", {})
        )
        if not isinstance(current_stats, dict) or current_stats.get("error"):
            raise RuntimeError("unable to validate resumable benchmark ingestion home")
        validate_ingestion_receipt(receipt, current_stats)
        for index, question in enumerate(pending, 1):
            deterministic_variants = (
                build_query_variants(
                    question["question"], category=question["category"]
                )
                if not args.no_query_variants
                else []
            )
            variants = merge_query_variants(
                query_plans.get(question["question_id"], []),
                deterministic_variants,
            )
            call_args: dict[str, Any] = {
                "query": question["question"],
                "limit": args.retrieval_limit,
                "recall_mode": "advisory",
                "include_trace": True,
            }
            if variants:
                call_args["query_variants"] = variants
            started = time.perf_counter()
            payload = json.loads(
                provider.handle_tool_call("scope_recall_search", call_args)
            )
            latency = time.perf_counter() - started
            if payload.get("error") or payload.get("ok") is False:
                raise RuntimeError(
                    f"retrieval failed for {question['question_id']}: {payload}"
                )
            results = list(payload.get("results") or [])
            row = dict(question)
            row.update(
                {
                    "query_variants": variants,
                    "retrieval_latency_seconds": round(latency, 6),
                    "results": results,
                    "retrieval_metrics": retrieval_metrics(
                        results,
                        gold_evidence_ids=question["gold_evidence_ids"],
                        memory_map=receipt["memory_map"],
                    ),
                    "funnel_trace": payload.get("funnel_trace") or {},
                    "evidence_set_trace": payload.get("evidence_set_trace") or {},
                }
            )
            append_jsonl(output_path, row)
            write_heartbeat(
                args.run_dir,
                {
                    "phase": "retrieve",
                    "sample_id": sample["sample_id"],
                    "completed_in_sample": len(done) + index,
                    "sample_questions": len(questions),
                    "at": now_iso(),
                },
            )


def run_retrieval(
    *,
    dataset: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    config: dict[str, Any],
    query_plans: dict[str, list[str]],
    args: argparse.Namespace,
) -> None:
    by_sample = {str(sample["sample_id"]): sample for sample in dataset}
    questions_by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question in questions:
        questions_by_sample[str(question["sample_id"])].append(question)
    def process_sample(sample_id: str, sample_questions: list[dict[str, Any]]) -> None:
        sample = by_sample[sample_id]
        home = args.run_dir / "homes" / sample_id
        receipt_path = args.run_dir / "ingestion" / f"{sample_id}.json"
        receipt = store_sample(
            sample=sample,
            home=home,
            receipt_path=receipt_path,
            config=config,
            args=args,
        )
        retrieve_sample(
            sample=sample,
            questions=sample_questions,
            home=home,
            receipt=receipt,
            output_path=args.run_dir / "retrieval" / f"{sample_id}.jsonl",
            query_plans=query_plans,
            args=args,
        )

    items = list(questions_by_sample.items())
    if not items:
        return
    sample_workers = min(max(1, int(args.sample_workers)), len(items))
    if sample_workers == 1:
        for sample_id, sample_questions in items:
            process_sample(sample_id, sample_questions)
        return
    with ThreadPoolExecutor(max_workers=sample_workers) as executor:
        futures = [
            executor.submit(process_sample, sample_id, sample_questions)
            for sample_id, sample_questions in items
        ]
        for future in as_completed(futures):
            future.result()


def all_retrieval_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    retrieval_dir = run_dir / "retrieval"
    if not retrieval_dir.exists():
        return rows
    for path in sorted(retrieval_dir.glob("*.jsonl")):
        rows.extend(load_jsonl(path))
    return rows


def validated_retrieval_rows(
    questions: list[dict[str, Any]],
    retrieval_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate complete retrieval checkpoints with legacy error wording."""

    try:
        return validate_retrieval_artifacts(
            questions,
            retrieval_rows,
            require_complete=True,
        )
    except ValueError as exc:
        raise RuntimeError(f"retrieval checkpoint identity invalid: {exc}") from exc


def oracle_evidence(
    question: dict[str, Any],
    record_index: dict[str, dict[str, dict[str, str]]],
) -> str:
    records = record_index.get(str(question["sample_id"]), {})
    blocks: list[str] = []
    for rank, dia_id in enumerate(question["gold_evidence_ids"], 1):
        record = records.get(str(dia_id))
        if record is not None:
            blocks.append(
                f"[gold_evidence_rank={rank} | evidence_id={dia_id}]\n"
                + render_record(record)
            )
    return "\n\n".join(blocks) if blocks else "(no gold evidence provided)"


def evaluate_question(
    *,
    question: dict[str, Any],
    memory_map: dict[str, dict[str, Any]],
    record_index: dict[str, dict[str, dict[str, str]]],
    client: CodexModelClient,
    args: argparse.Namespace,
    attempt_round: int,
) -> dict[str, Any]:
    row = {
        key: question[key]
        for key in (
            "question_id",
            "sample_id",
            "question_index",
            "question",
            "gold_answer",
            "gold_evidence_ids",
            "category_id",
            "category",
            "current_date",
        )
    }
    row.update(
        {
            "attempt_round": attempt_round,
            "answer_model": args.answer_model,
            "judge_model": args.judge_model,
            "evidence_mode": args.evidence_mode,
            "retrieval_metrics": question.get("retrieval_metrics") or {},
            "query_variants": question.get("query_variants") or [],
            "started_at": now_iso(),
        }
    )
    try:
        if args.evidence_mode == "oracle":
            evidence = oracle_evidence(question, record_index)
        else:
            evidence = format_evidence(
                list(question.get("results") or []),
                memory_map=memory_map,
                max_chars=args.evidence_max_chars,
                chronological=question["category"] in {"multi-hop", "temporal"},
            )
        answer_prompt = build_answer_prompt(
            question=question["question"],
            category=question["category"],
            evidence=evidence,
            current_date=question["current_date"],
        )
        answer_started = time.perf_counter()
        raw_answer = client.complete(
            model=args.answer_model,
            system=(
                "You are a concise factual QA system. Follow the output format exactly "
                "and do not reveal chain-of-thought."
            ),
            user=answer_prompt,
        )
        answer_latency = time.perf_counter() - answer_started
        predicted = extract_answer(raw_answer)
        if predicted is None:
            repair_prompt = (
                answer_prompt
                + "\n\nYour previous response did not use the required format. "
                "Return exactly one line: ANSWER: <answer>."
            )
            raw_answer = client.complete(
                model=args.answer_model,
                system="Return exactly one ANSWER line and no other prose.",
                user=repair_prompt,
            )
            predicted = extract_answer(raw_answer)
        if predicted is None:
            row.update(
                {
                    "status": "invalid_answerer",
                    "error": "missing ANSWER line",
                    "answer_latency_seconds": round(answer_latency, 6),
                }
            )
            return row

        judge_prompt = build_judge_prompt(
            question=question["question"],
            gold_answer=question["gold_answer"],
            predicted_answer=predicted,
        )
        judge_started = time.perf_counter()
        raw_judge = client.complete(
            model=args.judge_model,
            system=(
                "You are a fair semantic answer judge. Return only the requested JSON label."
            ),
            user=judge_prompt,
        )
        judge_latency = time.perf_counter() - judge_started
        judgment = parse_judgment(raw_judge)
        if judgment is None:
            raw_judge = client.complete(
                model=args.judge_model,
                system="Return exactly {\"label\":\"CORRECT\"} or {\"label\":\"WRONG\"}.",
                user=judge_prompt,
            )
            judgment = parse_judgment(raw_judge)
        if judgment is None:
            row.update(
                {
                    "status": "invalid_judge",
                    "predicted_answer": predicted,
                    "raw_judge": raw_judge[:500],
                    "answer_latency_seconds": round(answer_latency, 6),
                    "judge_latency_seconds": round(judge_latency, 6),
                }
            )
            return row
        row.update(
            {
                "status": "scored",
                "correct": judgment,
                "predicted_answer": predicted,
                "judge_label": "CORRECT" if judgment else "WRONG",
                "answer_latency_seconds": round(answer_latency, 6),
                "judge_latency_seconds": round(judge_latency, 6),
                "completed_at": now_iso(),
            }
        )
        return row
    except ModelRouteDriftError:
        raise
    except Exception as exc:
        row.update(
            {
                "status": "invalid_transport",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "completed_at": now_iso(),
            }
        )
        return row


def run_evaluation(
    *,
    dataset: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    retrieval_rows = validated_retrieval_rows(
        questions,
        all_retrieval_rows(args.run_dir),
    )
    memory_maps = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))["memory_map"]
        for path in sorted((args.run_dir / "ingestion").glob("*.json"))
    }
    record_index = {
        str(sample["sample_id"]): {
            record["dia_id"]: record for record in conversation_records(sample)
        }
        for sample in dataset
    }
    results_path = args.run_dir / "results.jsonl"
    route_receipt = ensure_model_route_receipt(args)
    client = CodexModelClient(
        args.auth_path,
        timeout=args.model_timeout,
        max_attempts=MODEL_MAX_ATTEMPTS,
        expected_route=(route_receipt.get("route") or None),
    )
    expected_ids = {row["question_id"] for row in questions}

    def persisted_attempts() -> list[dict[str, Any]]:
        return validate_result_artifacts(
            questions,
            load_jsonl(results_path),
            answer_model=args.answer_model,
            judge_model=args.judge_model,
            evidence_mode=args.evidence_mode,
        )

    for attempt_round in range(1, max(1, args.model_rounds) + 1):
        attempts = persisted_attempts()
        done = completed_question_ids(attempts)
        pending = [
            retrieval_rows[row["question_id"]]
            for row in questions
            if row["question_id"] not in done
        ]
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    evaluate_question,
                    question=question,
                    memory_map=memory_maps[str(question["sample_id"])],
                    record_index=record_index,
                    client=client,
                    args=args,
                    attempt_round=attempt_round,
                ): question["question_id"]
                for question in pending
            }
            completed_this_round = 0
            for future in as_completed(futures):
                result = future.result()
                append_jsonl(results_path, result)
                completed_this_round += 1
                write_heartbeat(
                    args.run_dir,
                    {
                        "phase": "evaluate",
                        "attempt_round": attempt_round,
                        "completed_this_round": completed_this_round,
                        "pending_this_round": len(pending),
                        "scored_total": len(
                            completed_question_ids(persisted_attempts())
                        ),
                        "expected_total": len(expected_ids),
                        "at": now_iso(),
                    },
                )
        if expected_ids <= completed_question_ids(persisted_attempts()):
            break


def aggregate_retrieval(
    retrieval_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for cutoff in (5, 10, 20, 50):
        key = str(cutoff)
        rows = [
            row["retrieval_metrics"][key]
            for row in retrieval_rows
            if key in (row.get("retrieval_metrics") or {})
            and row["retrieval_metrics"][key].get("recall_fraction") is not None
        ]
        count = len(rows)
        output[key] = {
            "questions": count,
            "any_recall": (
                sum(1 for row in rows if row.get("any_recall") is True) / count
                if count
                else None
            ),
            "all_recall": (
                sum(1 for row in rows if row.get("all_recall") is True) / count
                if count
                else None
            ),
            "mean_recall_fraction": (
                sum(float(row["recall_fraction"]) for row in rows) / count
                if count
                else None
            ),
        }
    return output


def _is_sha256_hex(value: Any) -> bool:
    digest = str(value or "")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _is_git_object_id(value: Any) -> bool:
    digest = str(value or "")
    return len(digest) in {40, 64} and all(
        character in "0123456789abcdef" for character in digest
    )


def _source_epoch_is_valid(epoch: Any) -> bool:
    return (
        isinstance(epoch, dict)
        and not epoch.get("error")
        and _is_git_object_id(epoch.get("head"))
        and _is_git_object_id(epoch.get("head_tree"))
        and type(epoch.get("dirty")) is bool
        and type(epoch.get("index_count")) is int
        and epoch["index_count"] >= 0
        and _is_sha256_hex(epoch.get("index_sha256"))
        and type(epoch.get("tracked_count")) is int
        and epoch["tracked_count"] >= 0
        and _is_sha256_hex(epoch.get("tracked_worktree_sha256"))
        and type(epoch.get("untracked_count")) is int
        and epoch["untracked_count"] >= 0
        and _is_sha256_hex(epoch.get("untracked_sha256"))
        and _is_sha256_hex(epoch.get("candidate_sha256"))
    )


def _is_json_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def model_route_receipt_is_valid(
    receipt: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    """Validate exact, secret-free route receipt schema against the run manifest."""

    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "created_at",
        "source_candidate_sha256",
        "models",
        "timeout_seconds",
        "max_attempts",
        "workers",
        "model_rounds",
        "route",
    }:
        return False
    raw_route = receipt.get("route")
    route: dict[str, Any] = raw_route if isinstance(raw_route, dict) else {}
    if set(route) != {
        "provider",
        "protocol",
        "base_url_sha256",
        "credential_identity_fields",
        "credential_identity_sha256",
    }:
        return False
    raw_model_execution = (
        manifest.get("model_execution") if isinstance(manifest, dict) else None
    )
    model_execution: dict[str, Any] = (
        raw_model_execution if isinstance(raw_model_execution, dict) else {}
    )
    raw_source = manifest.get("source") if isinstance(manifest, dict) else None
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    identity_fields = route.get("credential_identity_fields")
    receipt_models = receipt.get("models")
    manifest_models = manifest.get("models") if isinstance(manifest, dict) else None
    return (
        receipt.get("schema_version") == MODEL_ROUTE_SCHEMA_VERSION
        and isinstance(receipt.get("created_at"), str)
        and bool(receipt["created_at"])
        and _is_sha256_hex(receipt.get("source_candidate_sha256"))
        and receipt.get("source_candidate_sha256") == source.get("candidate_sha256")
        and isinstance(receipt_models, dict)
        and isinstance(manifest_models, dict)
        and receipt_models == manifest_models
        and all(isinstance(value, str) for value in receipt_models.values())
        and _is_json_number(receipt.get("timeout_seconds"))
        and _is_json_number(model_execution.get("timeout_seconds"))
        and float(receipt["timeout_seconds"])
        == float(model_execution["timeout_seconds"])
        and type(receipt.get("max_attempts")) is int
        and type(model_execution.get("max_attempts")) is int
        and receipt["max_attempts"] == model_execution["max_attempts"]
        and type(receipt.get("workers")) is int
        and type(model_execution.get("workers")) is int
        and receipt["workers"] == model_execution["workers"]
        and type(receipt.get("model_rounds")) is int
        and type(model_execution.get("model_rounds")) is int
        and receipt["model_rounds"] == model_execution["model_rounds"]
        and route.get("provider") == "openai-codex"
        and route.get("protocol") == "codex-responses"
        and _is_sha256_hex(route.get("base_url_sha256"))
        and isinstance(identity_fields, list)
        and bool(identity_fields)
        and all(isinstance(field, str) and field for field in identity_fields)
        and identity_fields == sorted(set(identity_fields))
        and _is_sha256_hex(route.get("credential_identity_sha256"))
    )


def official_comparability_checks(
    *,
    questions: list[dict[str, Any]],
    manifest: dict[str, Any],
    score: dict[str, Any],
    retrieval_rows: list[dict[str, Any]],
    retrieval_summary: dict[str, Any],
    model_route_receipt_valid: bool,
    retrieval_artifacts_valid: bool,
    result_artifacts_valid: bool,
    query_plan_artifacts_valid: bool,
) -> dict[str, bool]:
    """Return explicit gates for an official category 1-4 LoCoMo claim."""

    expected_ids = [str(row.get("question_id") or "") for row in questions]
    expected_id_set = set(expected_ids)
    retrieval_ids = [str(row.get("question_id") or "") for row in retrieval_rows]
    category_counts = Counter(int(row.get("category_id") or 0) for row in questions)
    manifest_dataset = manifest.get("dataset") if isinstance(manifest, dict) else {}
    manifest_strategy = manifest.get("strategy") if isinstance(manifest, dict) else {}
    manifest_source = manifest.get("source") if isinstance(manifest, dict) else {}
    manifest_dependencies = (
        manifest.get("dependencies") if isinstance(manifest, dict) else {}
    )
    hermes_dependency = (
        manifest_dependencies.get("hermes_agent")
        if isinstance(manifest_dependencies, dict)
        else {}
    )
    model_execution = (
        manifest.get("model_execution") if isinstance(manifest, dict) else {}
    )
    top50 = retrieval_summary.get("50") if isinstance(retrieval_summary, dict) else {}
    actual_question_ids_sha256 = hashlib.sha256(
        "\n".join(expected_ids).encode("utf-8")
    ).hexdigest()
    return {
        "canonical_dataset": (
            isinstance(manifest_dataset, dict)
            and str(manifest_dataset.get("sha256") or "")
            == CANONICAL_LOCOMO_DATASET_SHA256
            and int(manifest_dataset.get("sample_count") or 0) == 10
        ),
        "canonical_questions": (
            len(expected_ids) == sum(CANONICAL_LOCOMO_CATEGORY_COUNTS.values())
            and actual_question_ids_sha256 == CANONICAL_LOCOMO_QUESTION_IDS_SHA256
            and str(manifest.get("question_ids_sha256") or "")
            == actual_question_ids_sha256
            and int(manifest.get("question_count") or 0) == len(expected_ids)
        ),
        "canonical_category_composition": dict(category_counts)
        == CANONICAL_LOCOMO_CATEGORY_COUNTS,
        "retrieved_evidence_only": (
            isinstance(manifest_strategy, dict)
            and str(manifest_strategy.get("evidence_mode") or "") == "retrieved"
        ),
        "source_epoch_bound": _source_epoch_is_valid(manifest_source),
        "hermes_dependency_bound": _source_epoch_is_valid(hermes_dependency),
        "execution_contract_bound": (
            isinstance(model_execution, dict)
            and _is_json_number(model_execution.get("timeout_seconds"))
            and float(model_execution["timeout_seconds"]) > 0.0
            and type(model_execution.get("max_attempts")) is int
            and model_execution["max_attempts"] == MODEL_MAX_ATTEMPTS
            and type(model_execution.get("workers")) is int
            and model_execution["workers"] > 0
            and type(model_execution.get("model_rounds")) is int
            and model_execution["model_rounds"] > 0
        ),
        "score_complete": (
            bool(score.get("complete"))
            and score.get("artifact_rows_validated") is True
            and int(score.get("expected_questions") or 0) == len(expected_ids)
            and int(score.get("scored_questions") or 0) == len(expected_ids)
            and int(score.get("invalid_questions") or 0) == 0
            and int(score.get("missing_questions") or 0) == 0
            and int(score.get("unexpected_questions") or 0) == 0
        ),
        "retrieval_rows_complete": (
            len(retrieval_ids) == len(expected_ids)
            and len(set(retrieval_ids)) == len(expected_ids)
            and set(retrieval_ids) == expected_id_set
        ),
        "retrieval_metrics_complete": (
            isinstance(top50, dict)
            and int(top50.get("questions") or 0)
            == CANONICAL_LOCOMO_RETRIEVAL_METRIC_QUESTIONS
        ),
        "retrieval_artifacts_valid": bool(retrieval_artifacts_valid),
        "result_artifacts_valid": bool(result_artifacts_valid),
        "query_plan_artifacts_valid": bool(query_plan_artifacts_valid),
        "model_route_receipt_valid": bool(model_route_receipt_valid),
    }


def build_report(
    *,
    questions: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    manifest_path = args.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    models = manifest.get("models") if isinstance(manifest.get("models"), dict) else {}
    strategy = (
        manifest.get("strategy") if isinstance(manifest.get("strategy"), dict) else {}
    )
    raw_attempts = load_jsonl(args.run_dir / "results.jsonl")
    raw_retrieval_rows = all_retrieval_rows(args.run_dir)
    raw_planner_rows = load_jsonl(args.run_dir / "query-plans.jsonl")
    artifact_errors: dict[str, str] = {}

    try:
        attempts = validate_result_artifacts(
            questions,
            raw_attempts,
            answer_model=str(models.get("answerer") or ""),
            judge_model=str(models.get("judge") or ""),
            evidence_mode=str(strategy.get("evidence_mode") or ""),
        )
        result_artifacts_valid = True
    except ValueError as exc:
        attempts = []
        result_artifacts_valid = False
        artifact_errors["results"] = f"{type(exc).__name__}: {str(exc)[:240]}"

    try:
        retrieval_map = validate_retrieval_artifacts(
            questions,
            raw_retrieval_rows,
            require_complete=True,
        )
        retrieval_rows = list(retrieval_map.values())
        retrieval_artifacts_valid = True
    except ValueError as exc:
        retrieval_rows = []
        retrieval_artifacts_valid = False
        artifact_errors["retrieval"] = f"{type(exc).__name__}: {str(exc)[:240]}"

    planner_model = str(models.get("query_planner") or "")
    planner_enabled = bool(planner_model) and bool(strategy.get("query_variants"))
    planner_categories = (
        {
            int(value.strip())
            for value in str(strategy.get("planner_categories") or "").split(",")
            if value.strip()
        }
        if planner_enabled
        else set()
    )
    try:
        planner_map = validate_query_plan_artifacts(
            questions,
            raw_planner_rows,
            planner_model=planner_model,
            planner_categories=planner_categories,
            require_complete=True,
        )
        planner_rows = list(planner_map.values())
        query_plan_artifacts_valid = True
    except ValueError as exc:
        planner_rows = []
        query_plan_artifacts_valid = False
        artifact_errors["query_plans"] = f"{type(exc).__name__}: {str(exc)[:240]}"

    expected_ids = {row["question_id"] for row in questions}
    score = score_results(
        attempts,
        expected_question_ids=expected_ids,
        artifacts_validated=result_artifacts_valid,
    )
    retrieval_summary = aggregate_retrieval(retrieval_rows)
    model_route_path = args.run_dir / "model-route.json"
    model_route_receipt = (
        json.loads(model_route_path.read_text(encoding="utf-8"))
        if model_route_path.is_file()
        else {}
    )
    model_route_valid = model_route_receipt_is_valid(model_route_receipt, manifest)
    comparability_checks = official_comparability_checks(
        questions=questions,
        manifest=manifest,
        score=score,
        retrieval_rows=raw_retrieval_rows,
        retrieval_summary=retrieval_summary,
        model_route_receipt_valid=model_route_valid,
        retrieval_artifacts_valid=retrieval_artifacts_valid,
        result_artifacts_valid=result_artifacts_valid,
        query_plan_artifacts_valid=query_plan_artifacts_valid,
    )
    artifacts_valid = (
        retrieval_artifacts_valid
        and result_artifacts_valid
        and query_plan_artifacts_valid
    )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "manifest_sha256": sha256_file(manifest_path),
        "model_route_sha256": (
            sha256_file(model_route_path) if model_route_path.is_file() else None
        ),
        "artifact_validation": {
            "retrieval": retrieval_artifacts_valid,
            "results": result_artifacts_valid,
            "query_plans": query_plan_artifacts_valid,
            "errors": artifact_errors,
        },
        "score": score,
        "accuracy_percent": (
            round(float(score["accuracy"]) * 100.0, 4)
            if score["accuracy"] is not None
            else None
        ),
        "coverage_percent": round(float(score["coverage"]) * 100.0, 4),
        "retrieval": retrieval_summary,
        "query_planner": {
            "model": planner_model,
            "rows": len(raw_planner_rows),
            "validated_rows": len(planner_rows),
            "model_valid": sum(
                1 for row in planner_rows if row.get("model_valid") is True
            ),
            "fallback_used": sum(
                1 for row in planner_rows if row.get("fallback_used") is True
            ),
        },
        "attempt_rows": len(raw_attempts),
        "validated_attempt_rows": len(attempts),
        "retrieval_rows": len(raw_retrieval_rows),
        "validated_retrieval_rows": len(retrieval_rows),
        "official_comparability_checks": comparability_checks,
        "official_comparable_categories_1_to_4": all(
            comparability_checks.values()
        ),
        "complete": bool(score["complete"]) and artifacts_valid,
    }
    write_json_atomic(args.run_dir / "report.json", report)
    return report


def main() -> int:
    args = parse_args()
    args.run_dir = external_run_directory(args.run_dir)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if not args.dataset.is_file():
        raise FileNotFoundError(args.dataset)
    needs_auth = args.phase in {"all", "evaluate"} or (
        args.phase == "retrieve" and bool(args.query_planner_model)
    )
    if needs_auth and (
        args.auth_path is None or not args.auth_path.is_file()
    ):
        raise FileNotFoundError(
            "--auth-path must name an existing Hermes auth store for model-backed phases"
        )
    if not args.hermes_agent_root.is_dir():
        raise NotADirectoryError(args.hermes_agent_root)
    load_source_package(args.hermes_agent_root.resolve())
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not isinstance(dataset, list):
        raise ValueError("LoCoMo dataset must be a JSON array")
    questions = selected_questions(dataset, args)
    config = benchmark_config(args.embedder, args.retrieval_limit)
    ensure_manifest(
        args=args,
        dataset=dataset,
        questions=questions,
        config=config,
    )

    if args.phase in {"all", "retrieve"}:
        query_plans = prepare_query_plans(questions=questions, args=args)
        run_retrieval(
            dataset=dataset,
            questions=questions,
            config=config,
            query_plans=query_plans,
            args=args,
        )
    if args.phase in {"all", "evaluate"}:
        run_evaluation(dataset=dataset, questions=questions, args=args)
    if args.phase in {"all", "evaluate", "report"}:
        report = build_report(questions=questions, args=args)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["complete"] else 2

    summary = {
        "phase": "retrieve",
        "questions": len(questions),
        "retrieval_rows": len(all_retrieval_rows(args.run_dir)),
        "run_dir": str(args.run_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
