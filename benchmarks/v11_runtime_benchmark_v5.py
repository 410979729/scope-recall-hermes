"""P13 v5 bounded latency/resource diagnosis.

This is a TEST-only diagnostic over the existing 10k Core database.  It uses a
high-entropy deterministic vector family to avoid the periodic collisions in
the v4 fixture.  It does not measure semantic quality and does not rebuild the
100k fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
V4_PATH = ROOT / "benchmarks" / "v11_runtime_benchmark_v4.py"
V4_SPEC = importlib.util.spec_from_file_location("p13_v4_runtime", V4_PATH)
if V4_SPEC is None or V4_SPEC.loader is None:
    raise RuntimeError("v4_benchmark_import_unavailable")
V4 = importlib.util.module_from_spec(V4_SPEC)
V4_SPEC.loader.exec_module(V4)
OUT = ROOT / ".execution" / "TEST-P13-PREP-v5"
DATA_DIR = ROOT / ".execution" / "TEST-P13-PREP-v4" / "core-10k-clean"
INDEX_DIR = OUT / "lance-10k-high-entropy"
NATIVE_PY = Path(os.environ.get("P13_NATIVE_PY", str(ROOT / ".execution" / "TEST-FINAL-RUNTIME-ENV" / "Scripts" / "python.exe")))
ORDINALS = tuple((i * 499) % 10_000 for i in range(25))
COLD_ORDINALS = (998, 4491, 9481)


def high_entropy_vector(seed: int, dimensions: int) -> tuple[float, ...]:
    """Stable TEST vector with a large period and no intentional repeats."""
    state = (0x9E3779B9 ^ ((seed + 1) * 0x85EBCA6B)) & 0xFFFFFFFF
    values: list[float] = []
    for index in range(dimensions):
        state = (1664525 * state + 1013904223 + index * 0x27D4EB2D) & 0xFFFFFFFF
        values.append(state / 4294967296.0)
    return tuple(values)


V4.deterministic_vector = high_entropy_vector


class ResourceSampler:
    def __init__(self) -> None:
        self.process = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.peak_rss = 0
        self.peak_threads = 0
        self.peak_children = 0

    def start(self) -> None:
        import psutil

        self.process = psutil.Process()

        def sample() -> None:
            while not self.stop.is_set():
                try:
                    self.peak_rss = max(self.peak_rss, int(self.process.memory_info().rss))
                    self.peak_threads = max(self.peak_threads, int(self.process.num_threads()))
                    children = self.process.children(recursive=True)
                    self.peak_children = max(self.peak_children, len(children))
                except (psutil.Error, OSError):
                    pass
                self.stop.wait(0.01)

        self.thread = threading.Thread(target=sample, name="p13-v5-resource-sampler", daemon=True)
        self.thread.start()

    def finish(self) -> dict[str, int]:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        return {"peak_rss_bytes": self.peak_rss, "peak_threads": self.peak_threads, "peak_owned_children": self.peak_children}


def _timed(name: str, timings: dict[str, float], original: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    started = time.perf_counter()
    try:
        return original(*args, **kwargs)
    finally:
        timings[name] = timings.get(name, 0.0) + time.perf_counter() - started


def _item_payload(item: Any) -> dict[str, Any]:
    return {
        "kind": getattr(item, "kind", None),
        "ref": getattr(item, "ref", None),
        "revision": getattr(item, "revision", None),
        "evidence_refs": list(getattr(item, "evidence_refs", ())),
        "temporal_status": getattr(item, "temporal_status", None),
    }


def _install_instrumentation(core, query_embedding, holder: dict[str, dict[str, float] | None]) -> None:
    reader = core.recall_pipeline.storage_reader
    for name in ("exact", "lexical", "recent", "hydrate", "related"):
        original = getattr(reader, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            current = holder["current"]
            if current is None:
                return _original(*args, **kwargs)
            return _timed(_name, current, _original, *args, **kwargs)

        setattr(reader, name, wrapped)
    vector_port = core.recall_pipeline.vector_port
    original_vector = vector_port.search

    def wrapped_vector(*args, **kwargs):
        current = holder["current"]
        if current is None:
            return original_vector(*args, **kwargs)
        return _timed("native_vector_search", current, original_vector, *args, **kwargs)

    vector_port.search = wrapped_vector
    original_embed = query_embedding.embed_query

    def wrapped_embed(*args, **kwargs):
        current = holder["current"]
        if current is None:
            return original_embed(*args, **kwargs)
        return _timed("query_embedding", current, original_embed, *args, **kwargs)

    query_embedding.embed_query = wrapped_embed


def _one_hot(core, context, query_embedding, dataset, refs, ordinal: int, holder=None) -> dict[str, Any]:
    if holder is None:
        holder = {"current": None}
        _install_instrumentation(core, query_embedding, holder)
    timings: dict[str, float] = {}
    holder["current"] = timings
    request = {
        "protocol_version": "1.1", "request_id": f"p13-v5-hot-{ordinal}",
        "query": dataset[ordinal]["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200,
    }
    expected = refs[ordinal]
    started = time.perf_counter()
    try:
        result = core.recall(context, request, deadline_seconds=1.5)
        items = [_item_payload(item) for item in result.items]
        actual_refs = [f"{item['ref']}@{item['revision']}" for item in items if item["ref"]]
        evidence_refs = sorted({evidence for item in items for evidence in item["evidence_refs"]})
        payload = {
            "ordinal": ordinal, "elapsed_seconds": time.perf_counter() - started,
            "status": result.answerability_hint, "coverage": result.coverage,
            "gaps": list(result.gaps), "item_count": len(items), "items": items,
            "actual_refs": actual_refs, "expected_source_ref": expected,
            "expected_source_hit": any(item["ref"] == expected for item in items),
            "expected_evidence_hit": any(ref == f"{expected}@1" for ref in evidence_refs),
            "timings_seconds": timings,
        }
        holder["current"] = None
        return payload
    except Exception as exc:
        holder["current"] = None
        return {
            "ordinal": ordinal, "elapsed_seconds": time.perf_counter() - started,
            "status": "error", "coverage": "unknown", "gaps": [type(exc).__name__],
            "item_count": 0, "items": [], "actual_refs": [], "expected_source_ref": expected,
            "expected_source_hit": False, "expected_evidence_hit": False, "timings_seconds": timings,
        }


def _prepare_runtime(data_dir: Path, index_dir: Path):
    V4.alias()
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import SPACE_ID
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    store_started = time.perf_counter()
    store = ProcessLanceVectorStore(index_dir, table_name="P13_v5_3072", dimensions=V4.DIM10)
    store.open()
    store_open_seconds = time.perf_counter() - store_started
    embedding = V4.QueryEmbedding(V4.DIM10)
    port = LanceVectorPort(store, embedding, expected_embedding_space=SPACE_ID)
    core, context = V4.core_for(data_dir, vectors=port)
    return store, embedding, core, context, store_open_seconds


def cold_child(index_dir: Path, data_dir: Path, ordinal: int) -> dict[str, Any]:
    started = time.perf_counter()
    store, embedding, core, context, store_open_seconds = _prepare_runtime(data_dir, index_dir)
    try:
        holder = {"current": None}
        _install_instrumentation(core, embedding, holder)
        sample = _one_hot(core, context, embedding, V4.rows(V4.DATASET10), V4.source_refs(), ordinal, holder)
        sample.update({"kind": "cold", "child_elapsed_seconds": time.perf_counter() - started, "native_store_open_seconds": store_open_seconds})
        return sample
    finally:
        store.close()


def diagnose(index_dir: Path, data_dir: Path) -> dict[str, Any]:
    refs = V4.source_refs()
    dataset = V4.rows(V4.DATASET10)
    sampler = ResourceSampler()
    sampler.start()
    started = time.perf_counter()
    store, embedding, core, context, store_open_seconds = _prepare_runtime(data_dir, index_dir)
    try:
        holder = {"current": None}
        _install_instrumentation(core, embedding, holder)
        hot = [_one_hot(core, context, embedding, dataset, refs, ordinal, holder) for ordinal in ORDINALS]
    finally:
        store.close()
    resources = sampler.finish()
    cold: list[dict[str, Any]] = []
    for ordinal in COLD_ORDINALS:
        child_started = time.perf_counter()
        command = [sys.executable, str(SCRIPT), "--phase", "cold-child", "--index-dir", str(index_dir), "--data-dir", str(data_dir), "--ordinal", str(ordinal)]
        completed = subprocess.run(command, cwd=os.environ.get("TEMP") or str(ROOT), capture_output=True, text=True, timeout=30, check=False)
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            payload = {"status": "child_output_invalid", "gaps": [completed.stderr[-500:]]}
        payload.update({"parent_elapsed_seconds": time.perf_counter() - child_started, "exit_code": completed.returncode})
        cold.append(payload)
    elapsed = [sample["elapsed_seconds"] for sample in hot]
    success = [sample["elapsed_seconds"] for sample in hot if sample["expected_source_hit"] or sample["expected_evidence_hit"]]
    failed = [sample for sample in hot if not (sample["expected_source_hit"] or sample["expected_evidence_hit"])]
    return {
        "schema": "p13.diagnostic.v5",
        "status": "diagnostic_partial",
        "sample_count": len(hot),
        "ordinals": list(ORDINALS),
        "hot": hot,
        "cold": cold,
        "hot_elapsed_all_samples": _quantiles(elapsed),
        "hot_elapsed_expected_ref_or_evidence_samples": _quantiles(success),
        "hot_failure_or_empty_count": len(failed),
        "native_store_open_seconds": store_open_seconds,
        "resources": resources,
        "total_elapsed_seconds": time.perf_counter() - started,
        "vector_family": "high_entropy_deterministic_lcg_TEST_only",
        "semantic_quality": "not measured",
        "notes": [
            "expected_source_hit/evidence_hit separates fixture identity from latency; a missing expected ref is not by itself a semantic failure",
            "hot uses one open native store and one Core object for connection-reuse timing",
            "cold parent_elapsed includes fresh Python startup; child_elapsed and native_store_open_seconds split Core/native setup",
        ],
    }


def _quantiles(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_seconds": None, "p95_seconds": None, "min_seconds": None, "max_seconds": None}
    ordered = sorted(values)
    return {"count": len(ordered), "p50_seconds": ordered[(len(ordered) - 1) // 2], "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], "min_seconds": ordered[0], "max_seconds": ordered[-1]}


def build_index(index_dir: Path) -> dict[str, Any]:
    V4.alias()
    from scope_recall.core.recall_policy import SPACE_ID
    V4.SPACE_ID = SPACE_ID
    return V4.build_index(index_dir, V4.source_refs(), dimensions=V4.DIM10, count=10_000, batch=250)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("build-index", "diagnose", "cold-child"), required=True)
    parser.add_argument("--index-dir", type=Path, default=INDEX_DIR)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--ordinal", type=int, default=998)
    args = parser.parse_args()
    if args.phase == "build-index":
        result = build_index(args.index_dir)
    elif args.phase == "diagnose":
        result = diagnose(args.index_dir, args.data_dir)
    else:
        result = cold_child(args.index_dir, args.data_dir, args.ordinal)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
