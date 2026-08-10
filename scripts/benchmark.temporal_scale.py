#!/usr/bin/env python3
"""Deterministic 100k/1M temporal-ledger scale and natural-language recall benchmark."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIZES = (100_000, 1_000_000)
DEFAULT_ROUNDS = 30
MAX_ROWS = 1_000_000
PERFORMANCE_THRESHOLDS_MS = {
    "cold_lookup_p99": 50.0,
    "memory_filtered_current_p99": 100.0,
    "natural_language_current_p99": 500.0,
    "hot_overflow_p99": 1_000.0,
}
MAX_BUILD_SECONDS = 120.0
MAX_SECOND_START_SECONDS_BY_SIZE = {
    100_000: 2.0,
    1_000_000: 5.0,
}


def _bootstrap_source_package() -> None:
    """Prefer this checkout even when an older scope_recall is installed."""

    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    try:
        importlib.import_module("scope_recall.fact_repository")
        return
    except ImportError:
        for name in list(sys.modules):
            if name == "scope_recall" or name.startswith("scope_recall."):
                sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "scope_recall",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to bootstrap scope_recall source package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)


_bootstrap_source_package()

from scope_recall.fact_identity import canonical_fact_key  # noqa: E402
from scope_recall.fact_repository import (  # noqa: E402
    TemporalValidationError,
    claim_history,
    claims_as_of,
    current_claims_for_scopes,
)
from scope_recall.sql_store import ensure_schema  # noqa: E402
from scope_recall.temporal_query import (  # noqa: E402
    MAX_CURRENT_FACT_CANDIDATES,
    query_current_fact_views,
)

SCOPE = "scope-scale"
PREDICATE = "status"
HOT_SUBJECT = "Hot Slot"
COLD_SUBJECT = "Cold Slot"
BULK_SUBJECT = "Bulk Slot"
HOT_KEY = canonical_fact_key(HOT_SUBJECT, PREDICATE)
COLD_KEY = canonical_fact_key(COLD_SUBJECT, PREDICATE)
BULK_KEY = canonical_fact_key(BULK_SUBJECT, PREDICATE)
AT = "2026-07-15T00:00:00+00:00"
NATURAL_QUERY = "What is the emergency phone needle-route-2026?"
OVERFLOW_QUERY = "slot status"


def _parse_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated integers") from exc
    if not sizes or any(item < 2_000 or item > MAX_ROWS for item in sizes):
        raise argparse.ArgumentTypeError(
            f"sizes must contain integers between 2000 and {MAX_ROWS}"
        )
    if len(set(sizes)) != len(sizes):
        raise argparse.ArgumentTypeError("sizes must not contain duplicates")
    return sizes


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)

    def percentile(value: float) -> float:
        index = max(0, min(len(ordered) - 1, math.ceil(value * len(ordered)) - 1))
        return round(ordered[index] * 1000.0, 4)

    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "min_ms": round(ordered[0] * 1000.0, 4),
        "max_ms": round(ordered[-1] * 1000.0, 4),
    }


def _sample(call: Callable[[], Any], *, rounds: int) -> dict[str, float]:
    call()
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        call()
        samples.append(time.perf_counter() - started)
    return _percentiles(samples)


def _expect_overflow(call: Callable[[], Any]) -> None:
    try:
        call()
    except TemporalValidationError as exc:
        if "limit" not in str(exc):
            raise AssertionError(f"unexpected overflow error: {exc}") from exc
        return
    raise AssertionError("expected bounded scan overflow")


def _build_fixture(path: Path, rows: int) -> tuple[sqlite3.Connection, dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")
    ensure_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    started = time.perf_counter()
    conn.executemany(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, dedup_key, metadata
        ) VALUES (?, ?, 'scale-benchmark', 'memory', ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                "memory-hot",
                SCOPE,
                "Hot Slot common status overflow probe.",
                "Hot Slot common status overflow probe.",
                AT,
                AT,
                "scale:memory-hot",
                '{"lifecycle":"promoted","memory_type":"factual"}',
            ),
            (
                "memory-cold",
                SCOPE,
                "Cold Slot emergency phone needle-route-2026 is +1-555-0199.",
                "Cold Slot emergency phone needle-route-2026.",
                AT,
                AT,
                "scale:memory-cold",
                '{"lifecycle":"promoted","memory_type":"factual"}',
            ),
            (
                "memory-bulk",
                SCOPE,
                "Bulk Slot device status is green common telemetry.",
                "Bulk Slot common telemetry.",
                AT,
                AT,
                "scale:memory-bulk",
                '{"lifecycle":"promoted","memory_type":"factual"}',
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO memories_fts(memory_id, content, summary)
        SELECT id, content, summary
        FROM memories
        """
    )
    conn.executemany(
        """
        INSERT INTO memory_entities(memory_id, entity, weight, source)
        VALUES (?, ?, 1.0, 'scale-benchmark')
        """,
        (
            ("memory-hot", "benchmark:hot"),
            ("memory-cold", "benchmark:cold"),
            ("memory-bulk", "benchmark:bulk"),
        ),
    )
    conn.execute(
        """
        WITH RECURSIVE sequence(value) AS (
            SELECT 1
            UNION ALL
            SELECT value + 1 FROM sequence WHERE value < ?
        )
        INSERT INTO fact_claims(
            claim_id, memory_id, scope_id, subject_key, predicate_key,
            fact_key, value, normalized_value, value_fingerprint,
            cardinality, assertion_kind, valid_from, valid_to, recorded_at,
            retired_at, status, confidence, superseded_by_claim_id,
            source_type, source_ref, evidence_hash, metadata
        )
        SELECT
            printf('claim-%09d', value),
            CASE WHEN value <= 1001 THEN 'memory-hot'
                 WHEN value = ? THEN 'memory-cold'
                 ELSE 'memory-bulk' END,
            ?,
            CASE WHEN value <= 1001 THEN 'hot slot'
                 WHEN value = ? THEN 'cold slot'
                 ELSE 'bulk slot' END,
            'status',
            CASE WHEN value <= 1001 THEN ?
                 WHEN value = ? THEN ?
                 ELSE ? END,
            printf('value-%09d', value),
            printf('value-%09d', value),
            printf('fingerprint-%09d', value),
            'multi', 'direct', '2026-01-01T00:00:00+00:00', NULL,
            printf('2026-07-14T00:00:%02d+00:00', value % 60),
            NULL, 'current', 0.95, NULL,
            'scale-benchmark', printf('row:%d', value), '', '{}'
        FROM sequence
        """,
        (
            rows,
            rows,
            SCOPE,
            rows,
            HOT_KEY,
            rows,
            COLD_KEY,
            BULK_KEY,
        ),
    )
    conn.commit()
    build_seconds = time.perf_counter() - started
    second_start_changes_before = conn.total_changes
    second_start_started = time.perf_counter()
    ensure_schema(conn)
    second_start_seconds = time.perf_counter() - second_start_started
    second_start_write_delta = conn.total_changes - second_start_changes_before
    second_start_budget = MAX_SECOND_START_SECONDS_BY_SIZE.get(rows, 5.0)
    if second_start_write_delta != 0:
        raise AssertionError(
            "complete temporal DB second-start unexpectedly wrote "
            f"{second_start_write_delta} rows"
        )
    if second_start_seconds > second_start_budget:
        raise AssertionError(
            "complete temporal DB second-start exceeded budget: "
            f"{second_start_seconds:.4f}s > {second_start_budget:.4f}s"
        )
    count = int(conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0])
    fts_count = int(conn.execute("SELECT COUNT(*) FROM fact_claims_fts").fetchone()[0])
    plan_rows = conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT claim_id FROM fact_claims
        WHERE scope_id = ? AND fact_key = ?
        ORDER BY COALESCE(valid_from, ''), recorded_at, claim_id
        LIMIT 1001
        """,
        (SCOPE, COLD_KEY),
    ).fetchall()
    plan = [str(row[3]) for row in plan_rows]
    return conn, {
        "row_count": count,
        "fts_row_count": fts_count,
        "build_seconds": round(build_seconds, 4),
        "second_start_seconds": round(second_start_seconds, 4),
        "second_start_budget_seconds": second_start_budget,
        "second_start_write_delta": second_start_write_delta,
        "db_size_bytes": path.stat().st_size,
        "query_plan": plan,
        "slot_index_used": any(
            "idx_fact_claims_scope_fact_recorded" in item for item in plan
        ),
    }


def _scenario(root: Path, rows: int, *, rounds: int) -> dict[str, Any]:
    db_path = root / f"temporal-{rows}.sqlite3"
    conn, build = _build_fixture(db_path, rows)
    changes_before = conn.total_changes

    def cold_as_of() -> None:
        result = claims_as_of(
            conn,
            scope_id=SCOPE,
            subject=COLD_SUBJECT,
            predicate=PREDICATE,
            valid_at=AT,
            limit=10,
            scan_limit=1000,
        )
        if len(result) != 1 or result[0].fact_key != COLD_KEY:
            raise AssertionError("cold as-of lookup returned the wrong claim")

    def cold_history() -> None:
        result = claim_history(
            conn,
            scope_id=SCOPE,
            subject=COLD_SUBJECT,
            predicate=PREDICATE,
            limit=1000,
        )
        if len(result) != 1:
            raise AssertionError("cold history lookup returned the wrong size")

    def cold_current_by_memory() -> None:
        result = current_claims_for_scopes(
            conn,
            scope_ids=[SCOPE],
            valid_at=AT,
            memory_ids=["memory-cold"],
            limit=10,
        )
        if len(result) != 1 or result[0].fact_key != COLD_KEY:
            raise AssertionError("memory-filtered current lookup returned the wrong claim")

    natural_diagnostics: dict[str, Any] = {}
    overflow_diagnostics: dict[str, Any] = {}

    def natural_language_current() -> None:
        diagnostics: dict[str, Any] = {}
        result = query_current_fact_views(
            conn,
            scope_ids=[SCOPE],
            query=NATURAL_QUERY,
            valid_at=AT,
            limit=10,
            diagnostics=diagnostics,
        )
        if not result or result[0].memory_id != "memory-cold":
            raise AssertionError("natural-language current recall returned the wrong top-1")
        if diagnostics.get("truncated"):
            raise AssertionError("natural-language current recall silently truncated candidates")
        natural_diagnostics.clear()
        natural_diagnostics.update(diagnostics)

    def indexed_candidate_overflow_probe() -> None:
        diagnostics: dict[str, Any] = {}
        query_current_fact_views(
            conn,
            scope_ids=[SCOPE],
            query=OVERFLOW_QUERY,
            valid_at=AT,
            limit=10,
            diagnostics=diagnostics,
        )
        if not diagnostics.get("truncated") or diagnostics.get("complete"):
            raise AssertionError("indexed candidate overflow was not surfaced explicitly")
        overflow_diagnostics.clear()
        overflow_diagnostics.update(diagnostics)

    def hot_as_of_overflow() -> None:
        _expect_overflow(
            lambda: claims_as_of(
                conn,
                scope_id=SCOPE,
                subject=HOT_SUBJECT,
                predicate=PREDICATE,
                valid_at=AT,
                limit=100,
                scan_limit=1000,
            )
        )

    def hot_history_overflow() -> None:
        _expect_overflow(
            lambda: claim_history(
                conn,
                scope_id=SCOPE,
                subject=HOT_SUBJECT,
                predicate=PREDICATE,
                limit=1000,
            )
        )

    def scope_current_overflow() -> None:
        _expect_overflow(
            lambda: current_claims_for_scopes(
                conn,
                scope_ids=[SCOPE],
                valid_at=AT,
                limit=1000,
            )
        )

    indexed_candidate_overflow_probe()
    latency = {
        "cold_as_of": _sample(cold_as_of, rounds=rounds),
        "cold_history": _sample(cold_history, rounds=rounds),
        "cold_current_by_memory": _sample(cold_current_by_memory, rounds=rounds),
        "natural_language_current": _sample(natural_language_current, rounds=rounds),
        "hot_as_of_overflow": _sample(hot_as_of_overflow, rounds=rounds),
        "hot_history_overflow": _sample(hot_history_overflow, rounds=rounds),
        "scope_current_overflow": _sample(scope_current_overflow, rounds=rounds),
    }
    quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    cold_p99 = max(
        latency["cold_as_of"]["p99_ms"],
        latency["cold_history"]["p99_ms"],
    )
    hot_p99 = max(
        latency["hot_as_of_overflow"]["p99_ms"],
        latency["hot_history_overflow"]["p99_ms"],
        latency["scope_current_overflow"]["p99_ms"],
    )
    checks = {
        "row_count_exact": build["row_count"] == rows,
        "fts_row_count_exact": build["fts_row_count"] == rows,
        "slot_index_used": bool(build["slot_index_used"]),
        "natural_language_top1_correct": natural_diagnostics.get("strategy") == "fts5_bm25",
        "natural_language_candidates_complete": bool(natural_diagnostics.get("complete")),
        "indexed_candidate_overflow_explicit": (
            bool(overflow_diagnostics.get("truncated"))
            and not bool(overflow_diagnostics.get("complete"))
        ),
        "read_queries_zero_write": conn.total_changes == changes_before,
        "quick_check_ok": quick_check == "ok",
        "p99_finite": all(
            math.isfinite(metrics["p99_ms"]) for metrics in latency.values()
        ),
        "build_time_within_threshold": build["build_seconds"] <= MAX_BUILD_SECONDS,
        "cold_lookup_p99_within_threshold": (
            cold_p99 <= PERFORMANCE_THRESHOLDS_MS["cold_lookup_p99"]
        ),
        "memory_filtered_current_p99_within_threshold": (
            latency["cold_current_by_memory"]["p99_ms"]
            <= PERFORMANCE_THRESHOLDS_MS["memory_filtered_current_p99"]
        ),
        "natural_language_current_p99_within_threshold": (
            latency["natural_language_current"]["p99_ms"]
            <= PERFORMANCE_THRESHOLDS_MS["natural_language_current_p99"]
        ),
        "hot_overflow_p99_within_threshold": (
            hot_p99 <= PERFORMANCE_THRESHOLDS_MS["hot_overflow_p99"]
        ),
    }
    conn.close()
    return {
        "rows": rows,
        "build": build,
        "latency": latency,
        "natural_language_diagnostics": natural_diagnostics,
        "overflow_diagnostics": overflow_diagnostics,
        "checks": checks,
        "passed": all(checks.values()),
    }


def run_benchmark(*, sizes: tuple[int, ...], rounds: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="scope-recall-temporal-scale-") as raw:
        root = Path(raw)
        scenarios = [_scenario(root, rows, rounds=rounds) for rows in sizes]
        return {
            "schema_version": "scope-recall.temporal-scale.v2",
            "candidate_version": "1.9.2",
            "live_database_used": False,
            "sizes": list(sizes),
            "rounds_per_query": rounds,
            "scan_caps": {
                "current_indexed_candidates": MAX_CURRENT_FACT_CANDIDATES,
                "slot": 1000,
                "history": 1000,
            },
            "thresholds": {
                **PERFORMANCE_THRESHOLDS_MS,
                "max_build_seconds": MAX_BUILD_SECONDS,
            },
            "scenarios": scenarios,
            "passed": all(item["passed"] for item in scenarios),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON (default output is JSON too)")
    parser.add_argument(
        "--sizes",
        type=_parse_sizes,
        default=DEFAULT_SIZES,
        help="comma-separated row counts; release default is 100000,1000000",
    )
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    args = parser.parse_args()
    if args.rounds < 1 or args.rounds > 100:
        parser.error("rounds must be between 1 and 100")
    payload = run_benchmark(sizes=tuple(args.sizes), rounds=int(args.rounds))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
