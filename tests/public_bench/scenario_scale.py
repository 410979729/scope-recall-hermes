"""Capture freshness at scale; recall latency is imported, never remeasured here."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from scenario_model import FixtureModel
from scenario_store import HOSTS, contains
from scope_recall._version import __version__
from scope_recall.contracts import ContractError


def percentile(values):
    """Return a nearest-rank p95 from actual elapsed milliseconds."""
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def seed(s, count):
    """Clone a captured source template only inside this newly created fixture.

    This capture-only fixture has no claim/vector population and is explicitly
    excluded from frozen full-workload performance acceptance. Lexical postings
    are populated so new-message searches see a real large source index.
    """
    s.hook("codex", "TEST-scale-seed synthetic background text")
    with sqlite3.connect(s.core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        template = dict(conn.execute("SELECT * FROM source_events LIMIT 1").fetchone())
        terms = conn.execute(
            "SELECT term_id FROM lexical_postings WHERE source_id=?", (template["source_id"],)
        ).fetchall()
        columns = list(template)
        statement = f"INSERT INTO source_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        for start in range(1, count, 2000):
            rows, postings = [], []
            for index in range(start, min(count, start + 2000)):
                row = dict(
                    template,
                    event_id="event-" + hashlib.sha256(f"TEST-scale/{index}".encode()).hexdigest(),
                    source_event_key=f"TEST-scale/{index}",
                    source_group_key=f"TEST-scale/{index}",
                    source_id=template["source_id"] + index,
                    session_id=f"TEST-scale-{index // 10}",
                )
                rows.append(tuple(row[name] for name in columns))
                postings.extend((term[0], row["source_id"]) for term in terms)
            conn.executemany(statement, rows)
            conn.executemany("INSERT INTO lexical_postings VALUES (?,?)", postings)
            conn.commit()
    return dict(sources=count, claims=0, vectors=0, purpose="capture_only_not_full_frozen_workload")


def backlog(s):
    """Observe real consolidation queue depth and age using current UTC."""
    rows = s.rows("SELECT state,available_at,last_error_code FROM work_items WHERE work_type='consolidate'")
    pending = [row for row in rows if row["state"] in {"pending", "leased", "failed"}]
    oldest = min((row["available_at"] for row in pending), default=None)
    age = (
        max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds())
        if oldest
        else 0
    )
    return dict(depth=len(pending), oldest_age_seconds=age, states=rows)


def foreground(s, *, samples=20):
    """Alternate four actual hooks and immediately read each newly captured message."""
    rows = []
    for index in range(samples):
        host = HOSTS[index % len(HOSTS)]
        text = f"TEST-fresh-{index}-{time.time_ns()} 新消息颜色是银色。"
        began = time.perf_counter()
        hook = s.hook(host, text)
        read = s.recall(HOSTS[(index + 1) % len(HOSTS)], text.split()[0])
        rows.append(
            dict(
                host=host,
                input=text,
                hook=hook,
                read=read,
                visible=contains(read, "新消息颜色是银色"),
                visibility_ms=(time.perf_counter() - began) * 1000,
            )
        )
        # Four offered arrivals per second, one foreground operation in flight.
        time.sleep(max(0, 0.25 - (time.perf_counter() - began)))
    return rows


def worker(s, ready, stop):
    """Drain a genuine background queue while the foreground captures new messages."""
    model, receipts = FixtureModel(), []
    ready.set()
    for _ in range(24):
        if stop.is_set():
            break
        s.clock.now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        receipts.append(bounded_pass(s, model, "TEST-scale-worker", max_items=32))
    return dict(receipts=receipts, model_calls=model.calls)


def bounded_pass(s, model, identifier, *, max_items):
    """Treat a spent worker deadline as a bounded pass, retaining its diagnostic.

    Production supervisors schedule another pass after a deadline. Letting that
    exception escape the test thread would discard successful foreground reads.
    """
    try:
        result = s.core.drain_worker(
            s.context(), consolidation=model, owner_id=identifier, max_items=max_items, remaining_seconds=10
        )
        return str(result)
    except ContractError as exc:
        if exc.code != "DEADLINE_EXCEEDED":
            raise
        return "bounded_worker_deadline:DEADLINE_EXCEEDED"


def import_latency(args):
    """Read the named report verbatim and parse only its explicit threshold rows."""
    path = args.resources / "latency2-report.md"
    text = path.read_text(encoding="utf-8")
    pattern = (
        r"\| (350,000|1,000,000) / (正常向量|缺失向量) \| "
        r"(完整自动召回p95|检索p95|首个及每会话首次最大) \| ([\d,\.]+) \| (\d+) \|"
    )
    rows = [
        dict(
            scale=int(scale.replace(",", "")),
            state=state,
            metric=metric,
            ms=float(value.replace(",", "")),
            limit_ms=int(limit),
        )
        for scale, state, metric, value, limit in re.findall(pattern, text)
    ]
    return dict(
        path=str(path),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        rows=rows,
        source="latency package report; not this checkout's measurements",
    )


def scale(s, r, args):
    """Combine imported latency gates with bounded local capture/freshness evidence."""
    imported = import_latency(args)
    if __version__ == "3.16.1":
        baseline = json.loads((args.resources / "scenarios" / "results.json").read_text(encoding="utf-8"))["scenarios"][
            "S9"
        ]
        r.record("S9.latency", False, dict(source="M0 retained baseline", result=baseline))
    else:
        r.record(
            "S9.latency",
            len(imported["rows"]) == 12 and all(row["ms"] <= row["limit_ms"] for row in imported["rows"]),
            imported,
        )
    fixture = seed(s, args.scale)
    warm = foreground(s)
    starts = [s.hook(HOSTS[index % 4], "", event="SessionStart") for index in range(20)]
    p95 = percentile([row["hook"]["diagnostics"].get("capture_elapsed_ms") or row["hook"]["ms"] for row in warm])
    r.record(
        "S9.capture",
        p95 <= 43.9241 and percentile([row["ms"] for row in starts]) <= 200,
        dict(
            fixture=fixture,
            capture_p95_ms=p95,
            start_p95_ms=percentile([row["ms"] for row in starts]),
            samples=warm,
            starts=starts,
        ),
    )
    for index in range(200):
        s.hook("codex", "", event="Stop", last_assistant_message=f"TEST-backfill-{index} synthetic background text")
    before, began, ready, stop = backlog(s), time.perf_counter(), Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker, s, ready, stop)
        if not ready.wait(5):
            raise AssertionError("background worker did not enter its bounded pass")
        live = foreground(s)
        stop.set()
        work = running.result(timeout=60)
    elapsed, after = time.perf_counter() - began, backlog(s)
    concurrent_p95 = percentile(
        [row["hook"]["diagnostics"].get("capture_elapsed_ms") or row["hook"]["ms"] for row in live]
    )
    r.record(
        "S9.fresh",
        all(row["visible"] for row in live) and concurrent_p95 <= 43.9241,
        dict(
            fixture=fixture,
            samples=live,
            capture_p95_ms=concurrent_p95,
            offered_rate=4,
            actual_rate=len(live) / elapsed,
            max_inflight=1,
            worker=work,
        ),
    )
    recovered = after
    for _ in range(16):
        if recovered["depth"] == 0 or time.perf_counter() - began > 60:
            break
        s.clock.now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        bounded_pass(s, FixtureModel(), "TEST-scale-recovery", max_items=64)
        recovered = backlog(s)
    recovery_seconds = time.perf_counter() - began if recovered["depth"] == 0 else None
    r.record(
        "S9.backlog",
        recovered["depth"] == 0,
        dict(
            before=before,
            after=after,
            elapsed_seconds=elapsed,
            net_growth_per_second=(after["depth"] - before["depth"]) / elapsed,
            recovery_seconds=recovery_seconds,
            recovered=recovered,
            scope="consolidation_only_embedding_unconfigured",
        ),
    )
    r.machine("S9.frozen", "延迟报告事实/范围组成与 M0 不同；缺同条件两档捕获、会话开始、全通道积压和真实网络嵌入")
    r.machine("S9.linux", "Linux 同冻结负载与同门槛结果")
