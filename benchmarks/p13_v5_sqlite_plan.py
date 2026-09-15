"""P13 TEST-only SQLite planner/profile evidence for one fixed query."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
V4_PATH = ROOT / "benchmarks" / "v11_runtime_benchmark_v4.py"
spec = importlib.util.spec_from_file_location("p13_v4_for_plan", V4_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("v4_import_unavailable")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)
DB = ROOT / ".execution" / "TEST-P13-PREP-v4" / "core-10k-clean" / "memory.sqlite3"
OUT = ROOT / ".execution" / "TEST-P13-PREP-v5"
ORDINAL = 998


def plan_rows(connection, sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]


def main() -> int:
    v4.alias()
    from scope_recall.core.events import lexical_terms
    from scope_recall.core.recall_policy import meaningful_query_terms

    dataset = v4.rows(v4.DATASET10)
    query = dataset[ORDINAL]["query"]
    terms = tuple(meaningful_query_terms(query))
    if not terms:
        raise RuntimeError("fixed_query_has_no_meaningful_terms")
    scopes = (v4.SCOPE,)
    marks = ",".join("?" for _ in terms)
    scope_marks = ",".join("?" for _ in scopes)
    current_sql = f"""SELECT e.event_id,e.source_revision,COUNT(DISTINCT p.term) AS hits
        FROM lexical_projection p JOIN source_events e
        ON e.event_id=p.event_id AND e.source_revision=p.source_revision
        WHERE p.term IN ({marks}) AND e.scope_id IN ({scope_marks})
          AND e.read_blocked=0 AND (e.project_id IS NULL OR e.project_id=?)
          AND (e.branch_id IS NULL OR e.branch_id=?)
          AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
              AND b.object_ref=e.event_id AND b.read_blocked=1)
          AND NOT EXISTS (SELECT 1 FROM source_events newer
              WHERE newer.source_group_key=e.source_group_key
                AND newer.source_revision>e.source_revision)
        GROUP BY e.event_id,e.source_revision
        ORDER BY hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC LIMIT ?"""
    params = (*terms, *scopes, "P13", "main", 20)
    postings_sql = f"""SELECT e.event_id,e.source_revision,COUNT(DISTINCT p.term) AS hits
        FROM (SELECT term FROM lexical_projection WHERE term IN ({marks}) GROUP BY term) q
        CROSS JOIN lexical_projection p INDEXED BY sqlite_autoindex_lexical_projection_1
        JOIN source_events e
          ON e.event_id=p.event_id AND e.source_revision=p.source_revision
        WHERE p.term=q.term AND e.scope_id IN ({scope_marks})
          AND e.read_blocked=0 AND (e.project_id IS NULL OR e.project_id=?)
          AND (e.branch_id IS NULL OR e.branch_id=?)
          AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
              AND b.object_ref=e.event_id AND b.read_blocked=1)
          AND NOT EXISTS (SELECT 1 FROM source_events newer
              WHERE newer.source_group_key=e.source_group_key
                AND newer.source_revision>e.source_revision)
        GROUP BY e.event_id,e.source_revision
        ORDER BY hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC LIMIT ?"""
    with sqlite3.connect(DB) as connection:
        connection.row_factory = sqlite3.Row
        current_plan = plan_rows(connection, current_sql, params)
        posting_params = (*terms, *scopes, "P13", "main", 20)
        posting_plan = plan_rows(connection, postings_sql, posting_params)
        timings: dict[str, float] = {}
        outputs: dict[str, list[tuple[object, ...]]] = {}
        for name, sql, query_params in (("current", current_sql, params), ("postings_first", postings_sql, posting_params)):
            started = time.perf_counter()
            rows = [tuple(row) for row in connection.execute(sql, query_params).fetchall()]
            timings[name] = time.perf_counter() - started
            outputs[name] = rows
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        source_count = int(connection.execute("SELECT count(*) FROM source_events").fetchone()[0])
    v4_space = ROOT / ".execution" / "TEST-P13-PREP-v4" / "core-10k-clean"
    # Profile the real Core transaction status path separately from EXPLAIN.
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    binding = InstanceBinding("p13-benchmark-v3", "p13-installation-v3", v4_space.resolve(), frozenset({v4.SCOPE}), True)
    context = TrustedContext(binding, "p13-v4-session", frozenset({v4.SCOPE}), "human_direct", project_id="P13", branch_id="main")
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    status_times = []
    status_values = []
    for _ in range(3):
        started = time.perf_counter()
        with core.storage.read(context, remaining_seconds=5) as tx:
            status = tx.status()
        status_times.append(time.perf_counter() - started)
        status_values.append({"memory_epoch": status.memory_epoch, "sources": status.sources, "pending_work": status.pending_work})
    result = {
        "schema": "p13.sqlite-plan.v5",
        "database": str(DB),
        "fixed_ordinal": ORDINAL,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "meaningful_terms": list(terms),
        "source_count": source_count,
        "page_count": page_count,
        "current_plan": current_plan,
        "postings_first_plan": posting_plan,
        "timings_seconds": timings,
        "candidate_results_equal": outputs["current"] == outputs["postings_first"],
        "candidate_count": {name: len(rows) for name, rows in outputs.items()},
        "tx_status": {"samples_seconds": status_times, "status_values": status_values},
        "interpretation": "planner evidence only; no product SQL changed",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sqlite-plan.stdout.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
