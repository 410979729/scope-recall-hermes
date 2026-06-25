#!/usr/bin/env python3
"""Run a synthetic Scope Recall retrieval-regression benchmark.

This runner is intentionally self-contained and safe by default: it creates an
isolated temporary Hermes home, copies the current checkout into
``plugins/scope-recall`` for provider discovery, writes benchmark-only config,
stores a small labeled corpus plus configurable distractor rows, and executes
``scope_recall_benchmark`` with Recall Funnel traces enabled.

The goal is not to benchmark vector model quality.  The default config disables
vectors so every contributor/CI runner can exercise candidate_pool/top_k,
lexical/BM25/RRF plumbing, lifecycle filtering, and prompt-budget metrics
without API keys or native vector dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from plugins.memory import load_memory_provider

ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE_PATTERNS = (
    ".git",
    ".hermes",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "*.pyc",
    "build",
    "dist",
    "*.egg-info",
)

BASE_MEMORIES: list[dict[str, Any]] = [
    {
        "label": "atlas_deploy",
        "content": "Project Atlas production deploy command is uv run atlas-server --env prod after pytest passes.",
        "target": "project",
        "memory_type": "procedure",
        "importance": 0.95,
        "entities": ["Project Atlas"],
    },
    {
        "label": "atlas_low_value_general",
        "content": "Temporary Project Atlas dinner note: Joy likes warm soup tonight after deployment discussion.",
        "target": "general",
        "memory_type": "episodic",
        "importance": 0.1,
        "entities": ["Project Atlas"],
    },
    {
        "label": "northstar_old_archived",
        "content": "Northstar API base URL is https://old-api.invalid/v1. This stale value was replaced by a newer live check.",
        "target": "memory",
        "memory_type": "factual",
        "importance": 0.3,
        "entities": ["Northstar"],
        "lifecycle": "archived",
    },
    {
        "label": "northstar_current",
        "content": "Northstar API base URL is https://api.northstar.example/v2 according to the latest live configuration check.",
        "target": "project",
        "memory_type": "factual",
        "importance": 0.95,
        "entities": ["Northstar"],
    },
    {
        "label": "zephyr_ops",
        "content": "Project Zephyr rollback runbook uses systemctl restart zephyr-worker after verifying queue drain metrics.",
        "target": "ops",
        "memory_type": "procedure",
        "importance": 0.9,
        "entities": ["Project Zephyr"],
    },
]

BASE_CASES: list[dict[str, Any]] = [
    {
        "name": "atlas procedure beats local scratch",
        "query": "Project Atlas production deploy command",
        "expected_labels": ["atlas_deploy"],
        "forbidden_labels": ["atlas_low_value_general"],
        "min_rank": 1,
        "min_top_score": 0.1,
    },
    {
        "name": "archived old fact stays out of current answer",
        "query": "Northstar API base URL current latest configuration",
        "expected_labels": ["northstar_current"],
        "forbidden_labels": ["northstar_old_archived"],
        "min_rank": 1,
        "min_top_score": 0.1,
    },
    {
        "name": "topic isolation under overlapping ops terms",
        "query": "Project Zephyr rollback worker queue drain",
        "expected_labels": ["zephyr_ops"],
        "forbidden_labels": ["atlas_deploy", "atlas_low_value_general"],
        "min_rank": 1,
        "min_top_score": 0.1,
    },
    {
        "name": "atlas paraphrase remains in top-k",
        "query": "How do we deploy Atlas to production?",
        "expected_labels": ["atlas_deploy"],
        "forbidden_labels": ["atlas_low_value_general"],
        "min_rank": 2,
        "min_top_score": 0.1,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run synthetic scope-recall retrieval regression benchmark")
    parser.add_argument("--distractors", type=int, default=60, help="Number of synthetic distractor memories to insert")
    parser.add_argument("--limit", type=int, default=5, help="Maximum returned results per benchmark query")
    parser.add_argument("--candidate-pool", type=int, default=24, help="retrieval.candidate_pool for fixture config")
    parser.add_argument("--top-k", type=int, default=5, help="retrieval.top_k/default tool limit for fixture config")
    parser.add_argument("--prompt-budget-chars", type=int, default=1600, help="Returned character budget metric threshold")
    parser.add_argument("--min-known-answer-recall", type=float, default=1.0)
    parser.add_argument("--min-top-k-accuracy", type=float, default=1.0)
    parser.add_argument("--max-p95-ms", type=float, default=0.0, help="Optional latency gate; <=0 disables")
    parser.add_argument("--auto-explain-on-fail", action="store_true")
    parser.add_argument("--include-trace", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--keep-home", action="store_true", help="Do not delete the temporary Hermes home")
    return parser.parse_args()


def _write_config(hermes_home: Path, config: dict[str, Any]) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_source_plugin(source_root: Path, hermes_home: Path) -> Path:
    plugin_dir = hermes_home / "plugins" / "scope-recall"
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, plugin_dir, ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS))
    return plugin_dir


@contextmanager
def _provider_environment(hermes_home: Path) -> Iterator[None]:
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        yield
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


def _load_provider_for_home(hermes_home: Path) -> Any:
    with _provider_environment(hermes_home):
        plugin = load_memory_provider("scope-recall")
    if plugin is None:
        raise RuntimeError(f"scope-recall provider is not available in {hermes_home / 'plugins' / 'scope-recall'}")
    return plugin


def _mark_archived(plugin: Any, memory_id: str) -> None:
    conn = plugin._require_conn()
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"stored memory not found for archive marker: {memory_id}")
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except Exception:
        metadata = {}
    metadata["lifecycle"] = "archived"
    metadata["archived_by"] = "synthetic-retrieval-benchmark"
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


def _distractor(index: int) -> dict[str, Any]:
    project = f"Noise{index:04d}"
    # These rows intentionally share broad retrieval words (project, deploy,
    # rollback, command, API, queue) without sharing the discriminating entity.
    # They make candidate_pool/top_k regressions visible while keeping the
    # benchmark deterministic and API-key-free.
    return {
        "content": (
            f"Synthetic Project {project} maintenance note {index}: deploy command noise-{index} "
            f"and rollback checklist for unrelated queue/API validation."
        ),
        "target": "memory",
        "memory_type": "tool_trace" if index % 5 == 0 else "factual",
        "importance": 0.2,
        "entities": [f"Project {project}"],
    }


def _resolve_case_labels(case: dict[str, Any], label_to_id: dict[str, str]) -> dict[str, Any]:
    resolved = dict(case)
    resolved.pop("name", None)
    expected: list[str] = []
    for label in case.get("expected_labels") or []:
        expected.append(label_to_id[str(label)])
    forbidden: list[str] = []
    for label in case.get("forbidden_labels") or []:
        forbidden.append(label_to_id[str(label)])
    resolved.pop("expected_labels", None)
    resolved.pop("forbidden_labels", None)
    if expected:
        resolved["expected_ids"] = expected
    if forbidden:
        resolved["forbidden_ids"] = forbidden
    return resolved


def _fixture_config(*, candidate_pool: int, top_k: int) -> dict[str, Any]:
    return {
        "vector": {"enabled": False},
        "retrieval": {
            "mode": "lexical",
            "min_score": 0.0,
            "include_general": "same-scope",
            "candidate_pool": max(1, int(candidate_pool)),
            "top_k": max(1, int(top_k)),
        },
    }


def _apply_thresholds(payload: dict[str, Any], args: argparse.Namespace) -> None:
    raw_metrics = payload.get("metrics")
    metrics: dict[str, Any] = raw_metrics if isinstance(raw_metrics, dict) else {}
    failures = list(payload.get("failures") or [])
    recall = metrics.get("known_answer_recall")
    top_k = metrics.get("top_k_accuracy")
    p95 = float(metrics.get("latency_ms_p95") or 0.0)
    if recall is not None and float(recall) < float(args.min_known_answer_recall):
        failures.append(f"known_answer_recall_below_min:{recall}:min={args.min_known_answer_recall}")
    if top_k is not None and float(top_k) < float(args.min_top_k_accuracy):
        failures.append(f"top_k_accuracy_below_min:{top_k}:min={args.min_top_k_accuracy}")
    if float(args.max_p95_ms) > 0.0 and p95 > float(args.max_p95_ms):
        failures.append(f"latency_p95_above_max:{p95}:max={args.max_p95_ms}")
    payload["failures"] = failures
    payload["passed"] = bool(payload.get("passed")) and not failures


def run_synthetic(args: argparse.Namespace, hermes_home: Path) -> dict[str, Any]:
    _write_config(hermes_home, _fixture_config(candidate_pool=args.candidate_pool, top_k=args.top_k))
    plugin = _load_provider_for_home(hermes_home)
    label_to_id: dict[str, str] = {}
    try:
        with _provider_environment(hermes_home):
            plugin.initialize(
                f"session-synthetic-retrieval-{uuid.uuid4().hex}",
                hermes_home=str(hermes_home),
                platform="cli",
                agent_context="primary",
                agent_identity="yuheng",
                agent_workspace="hermes",
                user_id="joy",
            )
            for item in BASE_MEMORIES:
                label = str(item["label"])
                payload = {key: value for key, value in item.items() if key not in {"label", "lifecycle"}}
                stored = json.loads(plugin.handle_tool_call("scope_recall_store", payload))
                memory_id = str(stored.get("id") or "")
                if not memory_id:
                    raise RuntimeError(f"failed to store fixture {label}: {stored}")
                label_to_id[label] = memory_id
                if str(item.get("lifecycle") or "").lower() == "archived":
                    _mark_archived(plugin, memory_id)
            for index in range(max(0, int(args.distractors))):
                stored = json.loads(plugin.handle_tool_call("scope_recall_store", _distractor(index)))
                if not str(stored.get("id") or ""):
                    raise RuntimeError(f"failed to store distractor {index}: {stored}")
            resolved_cases = [_resolve_case_labels(case, label_to_id) for case in BASE_CASES]
            result = json.loads(
                plugin.handle_tool_call(
                    "scope_recall_benchmark",
                    {
                        "cases": resolved_cases,
                        "limit": max(1, int(args.limit)),
                        "auto_explain_on_fail": bool(args.auto_explain_on_fail),
                        "include_trace": bool(args.include_trace),
                        "prompt_budget_chars": max(0, int(args.prompt_budget_chars)),
                    },
                )
            )
    finally:
        plugin.shutdown()
    result["benchmark_name"] = "synthetic_retrieval_regression_v1"
    result["hermes_home"] = str(hermes_home)
    result["label_to_id"] = label_to_id
    result["synthetic"] = {
        "base_memories": len(BASE_MEMORIES),
        "cases": len(BASE_CASES),
        "distractors": max(0, int(args.distractors)),
        "candidate_pool": max(1, int(args.candidate_pool)),
        "top_k": max(1, int(args.top_k)),
        "limit": max(1, int(args.limit)),
        "prompt_budget_chars": max(0, int(args.prompt_budget_chars)),
    }
    _apply_thresholds(result, args)
    return result


def main() -> int:
    args = parse_args()
    temp_dir = tempfile.mkdtemp(prefix="scope-recall-retrieval-regression-")
    hermes_home = Path(temp_dir)
    _copy_source_plugin(ROOT, hermes_home)
    try:
        payload = run_synthetic(args, hermes_home)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("passed") else 1
    finally:
        if not args.keep_home:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
