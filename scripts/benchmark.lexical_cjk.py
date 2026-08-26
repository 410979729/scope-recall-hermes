#!/usr/bin/env python3
"""Bounded CJK lexical shadow quality and latency benchmark."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT_PATH.parents[1]
PACKAGE_NAME = "scope_recall_lexical_benchmark_runtime"

spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
if spec is None or spec.loader is None:
    raise RuntimeError("unable to load scope-recall package")
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = package
spec.loader.exec_module(package)

from scope_recall_lexical_benchmark_runtime.lexical_generation import (  # noqa: E402
    LEXICAL_GENERATION_ID,
    backfill_generation,
    create_shadow_generation,
)
from scope_recall_lexical_benchmark_runtime.sql_store import ensure_schema  # noqa: E402
from scope_recall_lexical_benchmark_runtime.storage_views import (  # noqa: E402
    search_db_memories,
)

_CJK_QUERIES = (
    "数据库迁移方案",
    "生产库切换前需要做什么",
    "上线前怎么做回滚演练",
)
_ENGLISH_QUERIES = (
    "OpenAI endpoint redirect safety",
    "exact credential stripping",
)

# Five queries across twenty rounds yield one hundred timed observations.  With
# nearest-rank p95 that keeps an isolated scheduler pause from becoming the
# percentile itself, while a consistently slow query still occupies enough of
# the sample to fail the unchanged latency contract.
DEFAULT_RELEASE_ROUNDS = 20


class _Provider:
    """Small provider surface required by the production storage view."""

    def __init__(self, conn: sqlite3.Connection, *, candidate_pool: int = 20):
        self._conn = conn
        self._lock = threading.RLock()
        self._accessible_scope_ids = ["scope-a"]
        self._retrieval_config = {
            "candidate_pool": candidate_pool,
            "min_score": 0.18,
        }

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _config_value(self, _key: str, default: Any) -> Any:
        return default


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark CJK lexical shadow quality and bounded latency"
    )
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    # Release-contract defaults: the strict gate must prove the shadow channel
    # at real scale (50k rows), not only a 2k smoke corpus.
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--rounds", type=int, default=DEFAULT_RELEASE_ROUNDS)
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def evaluate_latency_contract(
    *,
    legacy_p95_ms: float,
    shadow_p95_ms: float,
    release_contract: bool,
) -> dict[str, Any]:
    """Evaluate portable latency evidence against a paired host baseline.

    The 100 ms shadow target remains visible for operators, but shared CI hosts
    cannot enforce it as a universal absolute release bound: the same source
    can cross that target solely because the runner is slower. The paired
    legacy query is measured in the same process, so its ratio is the
    fail-closed regression guard. On a fast host, the ratio denominator is
    floored at ``target / budget`` so an optimized near-zero legacy path cannot
    turn a target-compliant shadow path into a false regression. Equivalently,
    the hard bound is ``shadow <= max(target, budget * legacy)``.
    """

    shadow_p95_target_ms = 100.0
    latency_ratio_budget = 4.0 if release_contract else 10.0
    if (
        not math.isfinite(legacy_p95_ms)
        or not math.isfinite(shadow_p95_ms)
        or legacy_p95_ms < 0.0
        or shadow_p95_ms < 0.0
    ):
        return {
            "latency_ratio": None,
            "latency_ratio_budget": latency_ratio_budget,
            "shadow_p95_target_ms": shadow_p95_target_ms,
            "target_misses": [],
            "failures": ["invalid_latency"],
        }
    # The JSON contract publishes six decimal places. Derive target evidence
    # and the ratio from those same public values so the outer validator can
    # reproduce the decision exactly at rounding boundaries.
    legacy_p95_ms = round(legacy_p95_ms, 6)
    shadow_p95_ms = round(shadow_p95_ms, 6)
    denominator_floor_ms = shadow_p95_target_ms / latency_ratio_budget
    latency_ratio = shadow_p95_ms / max(legacy_p95_ms, denominator_floor_ms)
    target_misses = (
        ["shadow_p95"] if shadow_p95_ms > shadow_p95_target_ms else []
    )
    failures = ["latency_ratio"] if latency_ratio > latency_ratio_budget else []
    return {
        "latency_ratio": latency_ratio,
        "latency_ratio_budget": latency_ratio_budget,
        "shadow_p95_target_ms": shadow_p95_target_ms,
        "target_misses": target_misses,
        "failures": failures,
    }


def _seed(conn: sqlite3.Connection, rows: int) -> None:
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records: list[tuple[str, str, str, str, str, str, str, str, str]] = [
        (
            "cjk-target",
            "scope-a",
            "benchmark",
            "ops",
            "生产数据库迁移方案：先做全量备份，校验副本，安排切换窗口，并在变更前完成回滚演练。",
            "生产数据库迁移、切换和回滚演练",
            created.isoformat(),
            created.isoformat(),
            "{}",
        ),
        (
            "english-target",
            "scope-a",
            "benchmark",
            "ops",
            "OpenAI endpoint redirect safety requires exact credential stripping and no HTTPS downgrade.",
            "Exact endpoint credential and redirect policy",
            created.isoformat(),
            (created + timedelta(seconds=1)).isoformat(),
            "{}",
        ),
    ]
    for index in range(max(0, rows - len(records))):
        timestamp = (created + timedelta(seconds=index + 2)).isoformat()
        records.append(
            (
                f"noise-{index:06d}",
                "scope-a",
                "benchmark",
                "ops",
                f"数据库监控日报 {index}：数据库容量、连接池、慢查询和告警指标巡检。",
                "数据库监控噪声",
                timestamp,
                timestamp,
                "{}",
            )
        )
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records,
    )
    conn.executemany(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        ((row[0], row[4], row[5]) for row in records),
    )
    conn.commit()


def _measure(
    provider: _Provider,
    *,
    generation_override: str,
    allow_unreviewed: bool,
    queries: tuple[str, ...],
    rounds: int,
    limit: int,
) -> tuple[list[float], dict[str, set[str]], int]:
    timings: list[float] = []
    latest: dict[str, set[str]] = {}
    max_result_count = 0
    for query in queries:
        search_db_memories(
            provider,
            query,
            limit=limit,
            generation_override=generation_override,
            allow_unreviewed_generation=allow_unreviewed,
        )
    for _round in range(rounds):
        for query in queries:
            started = time.perf_counter()
            results = search_db_memories(
                provider,
                query,
                limit=limit,
                generation_override=generation_override,
                allow_unreviewed_generation=allow_unreviewed,
            )
            timings.append((time.perf_counter() - started) * 1_000.0)
            latest[query] = {item.id for item in results}
            max_result_count = max(max_result_count, len(results))
    return timings, latest, max_result_count


def run_benchmark(*, rows: int, rounds: int, limit: int) -> dict[str, Any]:
    """Run the bounded synthetic lexical contract and return its evidence payload."""

    if rows < 100 or rows > 50_000:
        raise ValueError("rows must be between 100 and 50000")
    if rounds < 1 or rounds > 100:
        raise ValueError("rounds must be between 1 and 100")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _seed(conn, rows)
        baseline_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        create_shadow_generation(conn)
        while True:
            batch = backfill_generation(
                conn,
                LEXICAL_GENERATION_ID,
                batch_size=500,
            )
            conn.commit()
            if bool(batch["complete"]):
                break
        shadow_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])

        provider = _Provider(conn)
        all_queries = _CJK_QUERIES + _ENGLISH_QUERIES
        legacy_times, legacy_results, legacy_max = _measure(
            provider,
            generation_override="",
            allow_unreviewed=False,
            queries=all_queries,
            rounds=rounds,
            limit=limit,
        )
        shadow_times, shadow_results, shadow_max = _measure(
            provider,
            generation_override=LEXICAL_GENERATION_ID,
            allow_unreviewed=True,
            queries=all_queries,
            rounds=rounds,
            limit=limit,
        )

        cjk_found = sum(
            int("cjk-target" in shadow_results.get(query, set()))
            for query in _CJK_QUERIES
        )
        english_regressions = sum(
            len(legacy_results.get(query, set()) - shadow_results.get(query, set()))
            for query in _ENGLISH_QUERIES
        )
        legacy_p50 = _percentile(legacy_times, 0.50)
        legacy_p95 = _percentile(legacy_times, 0.95)
        shadow_p50 = _percentile(shadow_times, 0.50)
        shadow_p95 = _percentile(shadow_times, 0.95)
        page_growth = shadow_pages / max(baseline_pages, 1)
        release_contract = rows >= 50_000 and rounds >= DEFAULT_RELEASE_ROUNDS
        latency_contract = evaluate_latency_contract(
            legacy_p95_ms=legacy_p95,
            shadow_p95_ms=shadow_p95,
            release_contract=release_contract,
        )
        latency_ratio_raw = latency_contract["latency_ratio"]
        latency_ratio = (
            float(latency_ratio_raw)
            if isinstance(latency_ratio_raw, (int, float))
            and not isinstance(latency_ratio_raw, bool)
            else None
        )
        latency_ratio_budget = float(latency_contract["latency_ratio_budget"])
        max_count = max(legacy_max, shadow_max)
        failures: list[str] = []
        if cjk_found != len(_CJK_QUERIES):
            failures.append("cjk_quality")
        if english_regressions:
            failures.append("english_regression")
        if max_count > limit:
            failures.append("result_limit")
        failures.extend(str(item) for item in latency_contract["failures"])
        if page_growth > 2.5:
            failures.append("page_growth")
        return {
            "schema_version": "scope-recall.lexical-cjk-benchmark.v2",
            "passed": not failures,
            "contract_mode": "release" if release_contract else "smoke",
            "rows": rows,
            "rounds": rounds,
            "limit": limit,
            "cjk_queries": len(_CJK_QUERIES),
            "cjk_expected_found": cjk_found,
            "english_queries": len(_ENGLISH_QUERIES),
            "english_regressions": english_regressions,
            "max_result_count": max_count,
            "legacy_p50_ms": round(legacy_p50, 6),
            "legacy_p95_ms": round(legacy_p95, 6),
            "shadow_p50_ms": round(shadow_p50, 6),
            "shadow_p95_ms": round(shadow_p95, 6),
            "shadow_to_legacy_p95_ratio": (
                round(latency_ratio, 6) if latency_ratio is not None else None
            ),
            "baseline_pages": baseline_pages,
            "shadow_pages": shadow_pages,
            "page_growth_ratio": round(page_growth, 6),
            "targets": {
                "shadow_p95_ms": latency_contract["shadow_p95_target_ms"],
            },
            "target_misses": latency_contract["target_misses"],
            "budgets": {
                "shadow_to_legacy_p95_ratio_max": latency_ratio_budget,
                "page_growth_ratio_max": 2.5,
            },
            "failures": failures,
        }
    finally:
        conn.close()


def main() -> int:
    """Parse CLI arguments, emit benchmark evidence, and return its gate status."""

    args = _parse_args()
    payload = run_benchmark(rows=args.rows, rounds=args.rounds, limit=args.limit)
    text = json.dumps(
        payload,
        ensure_ascii=not bool(args.json),
        sort_keys=True,
        separators=(",", ":") if args.json else None,
        indent=None if args.json else 2,
    )
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8")
    print(text)
    return 0 if bool(payload.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
