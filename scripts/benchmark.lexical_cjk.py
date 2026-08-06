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
    parser.add_argument("--rows", type=int, default=2_000)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


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
        latency_ratio = shadow_p95 / max(legacy_p95, 0.25)
        page_growth = shadow_pages / max(1, baseline_pages)
        max_count = max(legacy_max, shadow_max)
        failures: list[str] = []
        if cjk_found != len(_CJK_QUERIES):
            failures.append("cjk_quality")
        if english_regressions:
            failures.append("english_regression")
        if max_count > limit:
            failures.append("result_limit")
        if shadow_p95 > 100.0:
            failures.append("shadow_p95")
        if latency_ratio > 4.0:
            failures.append("latency_ratio")
        if page_growth > 2.5:
            failures.append("page_growth")
        return {
            "schema_version": "scope-recall.lexical-cjk-benchmark.v1",
            "passed": not failures,
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
            "shadow_to_legacy_p95_ratio": round(latency_ratio, 6),
            "baseline_pages": baseline_pages,
            "shadow_pages": shadow_pages,
            "page_growth_ratio": round(page_growth, 6),
            "budgets": {
                "shadow_p95_ms_max": 100.0,
                "shadow_to_legacy_p95_ratio_max": 4.0,
                "page_growth_ratio_max": 2.5,
            },
            "failures": failures,
        }
    finally:
        conn.close()


def main() -> int:
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
