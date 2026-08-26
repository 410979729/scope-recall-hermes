"""Installed lexical CJK benchmark script contract."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.lexical_generation import (
    LEXICAL_GENERATION_ID,
    LEXICAL_POSTINGS_TABLE,
    LEXICAL_SHADOW_TABLE,
    backfill_generation,
    create_shadow_generation,
    ensure_lexical_generation_schema,
    generation_integrity_report,
)
from scope_recall.sql_store import ensure_schema, store_row

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark.lexical_cjk.py"


def _load_benchmark_module():
    module_name = "scope_recall_lexical_benchmark_contract_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_lexical_latency_contract_treats_absolute_p95_as_a_cross_host_target():
    benchmark = _load_benchmark_module()

    contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=91.063128,
        shadow_p95_ms=128.146864,
        release_contract=True,
    )

    assert 1.40 < contract["latency_ratio"] < 1.41
    assert contract["latency_ratio_budget"] == 4.0
    assert contract["shadow_p95_target_ms"] == 100.0
    assert contract["target_misses"] == ["shadow_p95"]
    assert contract["failures"] == []


def test_lexical_latency_contract_still_rejects_relative_regressions():
    benchmark = _load_benchmark_module()

    contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=20.0,
        shadow_p95_ms=100.1,
        release_contract=True,
    )

    assert contract["latency_ratio"] > 4.0
    assert contract["target_misses"] == ["shadow_p95"]
    assert contract["failures"] == ["latency_ratio"]


def test_lexical_latency_contract_fails_closed_on_invalid_measurements():
    benchmark = _load_benchmark_module()

    invalid_measurements = (
        (float("nan"), 20.0),
        (20.0, float("inf")),
        (-1.0, 20.0),
    )
    for legacy_p95_ms, shadow_p95_ms in invalid_measurements:
        contract = benchmark.evaluate_latency_contract(
            legacy_p95_ms=legacy_p95_ms,
            shadow_p95_ms=shadow_p95_ms,
            release_contract=True,
        )

        assert contract["latency_ratio"] is None
        assert contract["failures"] == ["invalid_latency"]


def test_lexical_latency_contract_accepts_exact_release_and_smoke_boundaries():
    benchmark = _load_benchmark_module()

    release_contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=25.0,
        shadow_p95_ms=100.0,
        release_contract=True,
    )
    smoke_contract = benchmark.evaluate_latency_contract(
        legacy_p95_ms=10.0,
        shadow_p95_ms=100.0,
        release_contract=False,
    )

    assert release_contract["latency_ratio"] == 4.0
    assert release_contract["target_misses"] == []
    assert release_contract["failures"] == []
    assert smoke_contract["latency_ratio"] == 10.0
    assert smoke_contract["target_misses"] == []
    assert smoke_contract["failures"] == []


def test_lexical_latency_contract_uses_the_published_six_decimal_values():
    benchmark = _load_benchmark_module()

    rounded_target = benchmark.evaluate_latency_contract(
        legacy_p95_ms=100.0,
        shadow_p95_ms=100.0000004,
        release_contract=True,
    )
    rounded_ratio = benchmark.evaluate_latency_contract(
        legacy_p95_ms=0.5007888341411246,
        shadow_p95_ms=0.41068645620639826,
        release_contract=True,
    )

    assert rounded_target["latency_ratio"] == 1.0
    assert rounded_target["target_misses"] == []
    assert rounded_target["failures"] == []
    assert rounded_ratio["latency_ratio"] == 0.410686 / 25.0
    assert rounded_ratio["failures"] == []


def test_lexical_latency_contract_uses_target_derived_floor_on_fast_hosts():
    benchmark = _load_benchmark_module()

    current_windows = benchmark.evaluate_latency_contract(
        legacy_p95_ms=1.3848,
        shadow_p95_ms=81.7871,
        release_contract=True,
    )
    frozen_1103_windows = benchmark.evaluate_latency_contract(
        legacy_p95_ms=1.5853,
        shadow_p95_ms=77.5278,
        release_contract=True,
    )
    fast_host_boundary = benchmark.evaluate_latency_contract(
        legacy_p95_ms=1.3848,
        shadow_p95_ms=100.0,
        release_contract=True,
    )

    assert current_windows["latency_ratio"] == 81.7871 / 25.0
    assert current_windows["target_misses"] == []
    assert current_windows["failures"] == []
    assert frozen_1103_windows["latency_ratio"] == 77.5278 / 25.0
    assert frozen_1103_windows["failures"] == []
    assert fast_host_boundary["latency_ratio"] == 4.0
    assert fast_host_boundary["failures"] == []


def test_lexical_cjk_benchmark_reports_quality_latency_and_growth():
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--json",
            "--rows",
            "500",
            "--rounds",
            "3",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "scope-recall.lexical-cjk-benchmark.v2"
    assert payload["passed"] is True
    assert payload["rows"] == 500
    assert payload["rounds"] == 3
    assert payload["cjk_expected_found"] == payload["cjk_queries"] == 3
    assert payload["english_regressions"] == 0
    assert payload["max_result_count"] <= payload["limit"] == 10
    assert payload["shadow_p95_ms"] >= 0.0
    assert payload["legacy_p95_ms"] >= 0.0
    assert payload["shadow_to_legacy_p95_ratio"] >= 0.0
    assert payload["page_growth_ratio"] >= 1.0
    assert payload["failures"] == []


# Structural performance invariants. These tests pin query-plan shapes instead
# of wall-clock budgets so they stay stable across hosts.

_POSTINGS_DOCID_INDEX = "idx_lexical_cjk_postings_v1_docid"
# Unconstrained FTS access path (``INDEX 0:`` with no rowid constraint); a
# constrained lookup renders as ``INDEX 0:=`` / ``INDEX 0:><`` instead.
_BLIND_SHADOW_SCAN = re.compile(r"^SCAN \S+ VIRTUAL TABLE INDEX 0:$")


def _shadow_fixture(rows: int = 4) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    for index in range(rows):
        store_row(
            conn,
            memory_id=f"memory-{index}",
            scope_id="scope-a",
            platform="telegram",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="aria",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="user",
            target="memory",
            content=f"数据库迁移记录 {index}",
            metadata=json.dumps({"lifecycle": "promoted"}),
            commit=False,
            enqueue_vector_intent=False,
        )
    conn.commit()
    create_shadow_generation(conn)
    while not bool(
        backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)["complete"]
    ):
        conn.commit()
    conn.commit()
    return conn


def _eqp_details(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] | None = None
) -> list[str]:
    if params is None:
        # EXPLAIN does not execute the statement; placeholder values only need
        # to satisfy the binding count.
        params = tuple(None for _ in range(sql.count("?")))
    return [
        str(row[3])
        for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    ]


def _is_blind_shadow_scan(detail: str) -> bool:
    """Full virtual-table scan: an unconstrained ``INDEX 0:`` access path."""

    return bool(_BLIND_SHADOW_SCAN.fullmatch(detail.strip()))


def test_postings_docid_deletes_use_the_covering_docid_index():
    conn = _shadow_fixture()

    equality_plan = _eqp_details(
        conn, f"DELETE FROM {LEXICAL_POSTINGS_TABLE} WHERE docid = ?", (1,)
    )
    range_plan = _eqp_details(
        conn,
        f"DELETE FROM {LEXICAL_POSTINGS_TABLE} WHERE docid > ? AND docid <= ?",
        (1, 2),
    )

    for plan in (equality_plan, range_plan):
        assert any(
            f"SEARCH {LEXICAL_POSTINGS_TABLE} USING COVERING INDEX "
            f"{_POSTINGS_DOCID_INDEX}" in detail
            for detail in plan
        ), plan
        assert not any(
            detail.strip() == f"SCAN {LEXICAL_POSTINGS_TABLE}" for detail in plan
        ), plan


def test_backfill_page_rebuild_uses_bounded_delete_plans():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_lexical_generation_schema(conn)
    for index in range(4):
        store_row(
            conn,
            memory_id=f"memory-{index}",
            scope_id="scope-a",
            platform="telegram",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="",
            agent_identity="aria",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="user",
            target="memory",
            content=f"数据库迁移记录 {index}",
            metadata=json.dumps({"lifecycle": "promoted"}),
            commit=False,
            enqueue_vector_intent=False,
        )
    conn.commit()
    create_shadow_generation(conn)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    backfill_generation(conn, LEXICAL_GENERATION_ID, batch_size=2)
    conn.set_trace_callback(None)
    traced = [sql.strip() for sql in statements]

    shadow_delete = next(
        sql for sql in traced if sql.startswith(f"DELETE FROM {LEXICAL_SHADOW_TABLE}")
    )
    postings_delete = next(
        sql
        for sql in traced
        if sql.startswith(f"DELETE FROM {LEXICAL_POSTINGS_TABLE}")
    )
    shadow_plan = _eqp_details(conn, shadow_delete)
    postings_plan = _eqp_details(conn, postings_delete)

    # The FTS docid range delete must be a constrained range access, never a
    # blind full virtual-table scan.
    assert shadow_plan
    assert not any(_is_blind_shadow_scan(detail) for detail in shadow_plan), shadow_plan
    # The postings page delete must ride the docid-leading covering index.
    assert any(
        f"SEARCH {LEXICAL_POSTINGS_TABLE} USING COVERING INDEX "
        f"{_POSTINGS_DOCID_INDEX}" in detail
        for detail in postings_plan
    ), postings_plan


def test_integrity_report_has_no_correlated_blind_shadow_scan():
    conn = _shadow_fixture()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    report = generation_integrity_report(conn, LEXICAL_GENERATION_ID)
    conn.set_trace_callback(None)
    assert report["healthy"] is True

    missing_plans: list[list[str]] = []
    for sql in statements:
        text = sql.strip()
        if not text.startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
            # Transaction markers and FTS-internal ``--`` commentary are not
            # independently explainable statements.
            continue
        plan = _eqp_details(conn, text)
        correlated = any("CORRELATED SCALAR SUBQUERY" in detail for detail in plan)
        blind = any(_is_blind_shadow_scan(detail) for detail in plan)
        # The O(n^2) signature: a correlated subquery re-scanning the whole
        # shadow table once per outer row.
        assert not (correlated and blind), (sql, plan)
        if "LEFT JOIN" in sql and LEXICAL_SHADOW_TABLE in sql and "IS NULL" in sql:
            missing_plans.append(plan)

    # The missing-row check specifically must be a flat rowid anti-join.
    assert missing_plans
    for plan in missing_plans:
        assert not any(
            "CORRELATED SCALAR SUBQUERY" in detail for detail in plan
        ), plan
