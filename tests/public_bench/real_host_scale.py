"""Frozen M0 corpus/load with full hooks, session starts and all-channel backlog."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

from cache import RequestCache
from cases import Case
from lab import Lab
from performance import NOW, SCOPES, distribution, seed_truth, seed_vectors
from ports import Ports
from real_host_io import write_json
from real_host_runtime import utc_now
from scenario_store import HOSTS


class TimedEmbedding:
    """Observe every uncached query call without changing the port's geometry or output."""

    def __init__(self, target):
        self.target, self.samples = target, []

    def embed_query(self, query, remaining_seconds=5):
        """Measure the real request's complete wall clock, including transport admission."""
        began = time.perf_counter()
        try:
            return self.target.embed_query(query, remaining_seconds=remaining_seconds)
        finally:
            self.samples.append((time.perf_counter() - began) * 1000)

    def __getattr__(self, name):
        return getattr(self.target, name)

    def embed_queries(self, queries, remaining_seconds=5):
        """Measure the batch method preferred by the production vector port."""
        began = time.perf_counter()
        try:
            return self.target.embed_queries(queries, remaining_seconds=remaining_seconds)
        finally:
            self.samples.append((time.perf_counter() - began) * 1000)


def scale_ports(run, root):
    """Use deterministic ports for smoke, otherwise shipped adapters with no response cache."""
    config = None
    if run.routes:
        auxiliary = run.routes["auxiliary"]
        config = dict(
            chat=auxiliary["consolidation"],
            embedding=auxiliary["embedding"],
            pricing=auxiliary["budget"]["pricing"],
            weights=auxiliary["consolidation"]["model"],
            temperature=0,
            thinking="disabled",
        )
    ports = Ports(
        RequestCache(root / "requests.sqlite3"),
        config=config,
        approved=() if config is None else [config["chat"]["endpoint"], config["embedding"]["endpoint"]],
        ledger_root=root,
    )
    # No warm response may replace network time in a full measurement.
    ports.embedder = TimedEmbedding(ports.embedder.embedder)
    ports.model = ports.model.model
    return ports


def clients(lab, root):
    """Attach four real logical client adapters to the same seeded shared installation."""
    from scope_recall.adapters.clients import CodexHookHandler
    from scope_recall.adapters.clients.config import load_shared_client
    from scope_recall.adapters.hermes.shared_entries import (
        attach_shared_record,
        client_entry_record,
        new_shared_payload,
        write_shared_payload,
    )
    from scope_recall.core import CoreConfig, MemoryCore

    payload = new_shared_payload(lab.binding.data_directory, agent_id=lab.binding.agent_id)
    payload["installation_id"] = lab.binding.installation_id
    write_shared_payload(lab.binding.data_directory, payload)
    attach_shared_record(
        lab.binding.data_directory,
        client_entry_record(
            host="codex",
            home=root / "TEST-seed-home",
            entry_id="test-seed",
            display_name="TEST seed",
            attached_at=utc_now(),
            allowed_scope_ids=sorted(SCOPES),
            writable_scope_ids=sorted(SCOPES),
            capture_scope_id="public",
        ),
        now=utc_now(),
    )
    result = []
    for host in HOSTS:
        home = root / (host + "-home")
        attach_shared_record(
            lab.binding.data_directory,
            client_entry_record(
                host=host,
                home=home,
                entry_id=host,
                display_name=host,
                attached_at=utc_now(),
                allowed_scope_ids=["public"],
                writable_scope_ids=["public"],
                capture_scope_id="public",
            ),
            now=utc_now(),
        )
        config = load_shared_client(home, host)
        # Each adapter binds only its granted scope; the seeding core retains
        # all eight scopes for corpus construction and background maintenance.
        core = MemoryCore(
            CoreConfig(config.to_binding()),
            clock=lab.clock,
            vectors=lab.core.recall_pipeline.vector_port,
            retrieval_policy=lab.core.recall_pipeline.policy,
        )
        result.append(CodexHookHandler(config, core=core, clock=lab.clock))
    return result


def backlog(lab):
    """Observe every channel's depth, errors and oldest real availability timestamp."""
    with sqlite3.connect(lab.core.storage.path) as conn:
        rows = conn.execute(
            "SELECT work_type,state,last_error_code,count(*),min(available_at) FROM work_items GROUP BY 1,2,3"
        ).fetchall()
    depth = sum(row[3] for row in rows if row[1] in {"pending", "leased", "failed"})
    oldest = min((row[4] for row in rows if row[1] in {"pending", "leased", "failed"}), default=None)
    return dict(
        depth=depth,
        channels=rows,
        oldest_age_seconds=max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds())
        if oldest
        else 0,
    )


def window(lab, handlers, *, samples, pace=4):
    """Offer four arrivals per second, one foreground in flight, retaining every measured call."""
    results = []
    began = time.monotonic()
    for index in range(samples):
        due = began + index / pace
        time.sleep(max(0, due - time.monotonic()))
        handler = handlers[index % len(handlers)]
        session = str(uuid4())
        lab.clock.now = utc_now()
        started = time.perf_counter()
        start = handler.handle_payload(
            dict(hook_event_name="SessionStart", session_id=session, cwd=str(lab.binding.data_directory))
        )
        start_ms = (time.perf_counter() - started) * 1000
        text = f"TEST-fresh-{uuid4().hex} color is silver."
        tick = time.perf_counter()
        reply = handler.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id=session,
                prompt=text,
                cwd=str(lab.binding.data_directory),
                turn_id=str(uuid4()),
                prompt_id=str(uuid4()),
                generation_id=str(uuid4()),
            )
        )
        auto_ms = (time.perf_counter() - tick) * 1000
        case = Case(f"TEST-visibility-{uuid4().hex}", "synthetic", text.split()[0], utc_now(), "", (), ())
        visible, retrieval_ms = lab.recall(case, mode="current")
        results.append(
            dict(
                entry=HOSTS[index % 4],
                start_ms=start_ms,
                auto_ms=auto_ms,
                capture_ms=handler.diagnostics.capture_elapsed_ms,
                retrieval_ms=retrieval_ms,
                visible="silver" in json.dumps(visible),
                auto_output=reply,
                start_output=start,
                gaps=visible.get("gaps", []),
            )
        )
    return dict(
        samples=results,
        seconds=time.monotonic() - began,
        actual_rate=samples / (time.monotonic() - began),
        offered_rate=pace,
        max_inflight=1,
        capture=distribution([row["capture_ms"] for row in results]),
        auto=distribution([row["auto_ms"] for row in results]),
        start=distribution([row["start_ms"] for row in results]),
        retrieval=distribution([row["retrieval_ms"] for row in results]),
    )


def work_pass(lab):
    """Execute bounded production work with all configured channels, preserving deadline receipts."""
    from scope_recall.contracts import ContractError

    try:
        receipt = lab.core.drain_worker(
            lab.context,
            owner_id="TEST-frozen-backfill",
            consolidation=lab.ports.model,
            embed=lab.embed,
            purge=lab.purge,
            max_items=32,
            remaining_seconds=30,
        )
        return str(receipt)
    except ContractError as exc:
        if exc.code != "DEADLINE_EXCEEDED":
            raise
        return "bounded_worker_deadline:DEADLINE_EXCEEDED"


def frozen_conditions(run, root):
    """Freeze the same M0 counts, distribution and offered load before generation."""
    from environment import machine

    result = dict(
        seed=42,
        scales=run.args.scales,
        dimensions=3072,
        scopes=8,
        claims="scale // 10",
        vectors="scale; normalized Gaussian float32; each 100th duplicates predecessor",
        sources="scale",
        entries=4,
        max_inflight=1,
        offered_rate=4,
        samples=run.args.scale_samples,
        backfill=200,
        worker_concurrency=1,
        backend="sqlite mapped arrays",
        corpus_time=NOW,
        query_embedding="uncached gemini-embedding-2" if run.routes else "deterministic local fake",
        cache="application first then warm; OS cache uncontrolled",
        machine=machine(root),
        limits_ms=dict(capture=43.9241, auto=1000, first=2000, retrieval=200, start=200),
    )
    write_json(root / "conditions.json", result)
    return result


def measure_scale(run, root, count):
    """Seed the existing frozen fixture and measure foreground during and after backfill."""
    root.mkdir()
    ports = scale_ports(run, root)
    lab = Lab(root / "store", ports, now=NOW, scopes=SCOPES, installation_kind="shared")
    handlers = []
    try:
        handlers = clients(lab, root)
        lab.context = replace(lab.context, entry_id="test-seed")
        refs = seed_truth(lab, count)
        vectors = seed_vectors(lab, refs)
        lab.clock.utc_now = utc_now
        lab.context = replace(lab.context, allowed_scope_ids=frozenset({"public"}))
        first = window(lab, handlers, samples=1)
        warm = window(lab, handlers, samples=run.args.scale_samples)
        with sqlite3.connect(lab.core.storage.path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO work_items(work_type,subject_ref,subject_revision,scope_id,state,available_at) "
                "VALUES ('embed',?,1,'public','pending',?)",
                [(ref, utc_now()) for ref in refs[::8][:200]],
            )
        before, stop, ready = backlog(lab), Event(), Event()
        receipts = []

        def background():
            ready.set()
            while not stop.is_set():
                receipts.append(work_pass(lab))
                stop.wait(1)

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(background)
            ready.wait(5)
            try:
                concurrent = window(lab, handlers, samples=run.args.scale_samples)
            finally:
                stop.set()
            worker.result(timeout=40)
        after = backlog(lab)
        recovery_began = time.monotonic()
        while backlog(lab)["depth"] and time.monotonic() - recovery_began < run.args.scale_recovery:
            receipts.append(work_pass(lab))
            time.sleep(1)
        recovered = backlog(lab)
        metrics = scale_metrics(first, warm, concurrent, before, after, recovered)
        metrics["embedding_measured"] = bool(ports.embedder.samples)
        return dict(
            scale=count,
            fixture=dict(sources=count, claims=count // 10, vectors=vectors),
            first=first,
            warm=warm,
            concurrent=concurrent,
            backlog_before=before,
            backlog_after=after,
            backlog_recovered=recovered,
            recovery_seconds=time.monotonic() - recovery_began,
            net_growth_per_second=(after["depth"] - before["depth"]) / concurrent["seconds"],
            worker_receipts=receipts,
            query_embedding=distribution(ports.embedder.samples) if ports.embedder.samples else None,
            metrics=metrics,
            passed=all(metrics.values()),
        )
    finally:
        for handler in handlers:
            handler.close()
        lab.close()


def scale_metrics(first, warm, concurrent, before, after, recovered):
    """Apply original thresholds to both windows; incomplete channels remain failures."""
    windows = (warm, concurrent)
    return dict(
        capture=all(w["capture"]["p95_ms"] <= 43.9241 for w in windows),
        auto=all(w["auto"]["p95_ms"] <= 1000 for w in windows),
        first=max(value for w in (first, *windows) for value in w["auto"]["samples_ms"]) <= 2000,
        retrieval=all(w["retrieval"]["p95_ms"] <= 200 for w in windows),
        start=all(w["start"]["p95_ms"] <= 200 for w in windows),
        fresh=all(row["visible"] for w in windows for row in w["samples"]),
        channels=all(not row["gaps"] for w in windows for row in w["samples"]),
        backlog=after["depth"] <= before["depth"] and recovered["depth"] == 0,
    )


def frozen_check(run):
    """Run selected scales; a small or deterministic run remains nonacceptance evidence."""
    root = run.root / "frozen"
    root.mkdir()
    conditions = frozen_conditions(run, root)
    results = []
    for scale in run.args.scales:
        result = measure_scale(run, root / str(scale), scale)
        write_json(root / f"scale-{scale}.json", result)
        results.append(result)
    complete = set(run.args.scales) == {350000, 1000000} and run.args.scale_samples == 60 and bool(run.routes)
    return complete and all(row["passed"] for row in results), dict(
        complete_frozen=complete,
        conditions=conditions,
        scales=results,
        scope="full" if complete else "small_or_deterministic_flow_only",
    )
