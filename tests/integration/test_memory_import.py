"""Physical import latency with concurrent hooks on synthetic evidence only."""

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue

import pytest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.memory_import import ImportRecord, import_record
from v11_support import context, recall_request, source_event


@pytest.mark.real_store_time
def test_imports_release_writer_for_capture_and_recall(tmp_path):
    count = int(os.environ.get("SCOPE_RECALL_IMPORT_BENCHMARK_RECORDS", "200"))
    assert count >= 200
    stride = count // 20
    ctx = context(tmp_path / "TEST-import-fairness")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    core.record_event(ctx, source_event(content="TEST saffron palette"), scope_id="TEST-scope")
    hook = MemoryCore(CoreConfig(ctx.binding))
    hook_ctx = replace(ctx, session_id="TEST-hook")
    milestones = Queue()
    probes = []

    def probe(index):
        started = time.perf_counter()
        receipt = hook.record_host_event(
            hook_ctx,
            source_event(source_event_key=f"TEST-hook/{index}", content=f"TEST capture number {index}"),
            scope_id="TEST-scope",
            host_scope=None,
            remaining_seconds=1.0,
        )
        captured = time.perf_counter()
        packet = hook.recall(hook_ctx, recall_request(query="saffron", mode="current"))
        ended = time.perf_counter()
        assert receipt.durability in {"persisted", "queued"}
        assert packet.items
        return {"capture": captured - started, "recall": ended - captured}

    baseline = [probe(i) for i in range(5)]

    def import_many():
        began = time.perf_counter()
        for index in range(count):
            item = ImportRecord(f"TEST-{index}", f"TEST synthetic imported material {index}", "assistant")
            receipt = import_record(core, ctx, item, source="mem0", source_id="TEST-large", scope_id="TEST-scope")
            assert receipt.durability == "persisted"
            if index % stride == stride // 10:
                milestones.put(index)
            time.sleep(0)
        return time.perf_counter() - began

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(import_many)
        for index in range(5, 25):
            milestones.get(timeout=120)
            probes.append(probe(index))
        elapsed = task.result(timeout=1800)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM source_events WHERE origin='imported'").fetchone()[0] == count
    metrics = {"records": count, "import_seconds": round(elapsed, 4), "probes_during_import": len(probes)}
    for label, samples in (("baseline", baseline), ("during", probes)):
        for operation in ("capture", "recall"):
            values = [sample[operation] for sample in samples]
            metrics[f"{label}_{operation}_median_ms"] = round(statistics.median(values) * 1000, 3)
            metrics[f"{label}_{operation}_max_ms"] = round(max(values) * 1000, 3)
    print("IMPORT_METRICS=" + json.dumps(metrics, sort_keys=True))
