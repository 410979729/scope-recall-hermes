"""Bounded P13 v3 performance and native-boundary benchmark.

This script is intentionally separate from the preserved v2 benchmark.  It
defines logical objects correctly (one source plus one claim per object),
keeps preparation resumable, and labels deterministic vectors as TEST-only.
It never calls a model or network endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import types
from typing import Any

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
PREP = ROOT / ".execution" / "TEST-P13-PREP-v3"
DATASET = PREP / "dataset-10000" / "objects.jsonl"
MANIFEST = PREP / "dataset-10000" / "manifest.json"
SCALE_DATASET = PREP / "dataset-100000" / "objects.jsonl"
SCALE_MANIFEST = PREP / "dataset-100000" / "manifest.json"
LOGICAL_OBJECTS = 10_000
SCALE_OBJECTS = 100_000
RECORDS_PER_OBJECT = 2
SCHEMA = "p13.synthetic.core-record.v3"
SCOPE_ID = "p13-scope-v3"
AUTO_DEADLINE_SECONDS = 1.5
SAMPLE_HOT = 20
SAMPLE_COLD = 5
SAMPLE_CAPTURE = 20
NATIVE_DIMENSIONS = 3072
# The native test runtime is deliberately owned by the verified W worktree;
# allow an explicit path for a copied/isolated runner but never fall back to a
# formal or system interpreter.
NATIVE_PY = Path(os.environ.get(
    "P13_NATIVE_PY",
    str(ROOT.parent / "scope-recall-v1.1" / ".execution" / "TEST-P08-native" / "Scripts" / "python.exe"),
))


def install_source_alias() -> None:
    if "scope_recall" not in sys.modules:
        package = types.ModuleType("scope_recall")
        package.__path__ = [str(ROOT)]
        sys.modules["scope_recall"] = package


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def environment_fingerprint() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version,
        "python_executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "os": os.name,
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "sqlite": sqlite3.sqlite_version,
        "model": "none",
        "network": "disabled_by_contract",
        "embedding": "deterministic_test_only",
    }
    try:
        import psutil  # type: ignore

        process = psutil.Process()
        snapshot.update({
            "rss_bytes": process.memory_info().rss,
            "threads": process.num_threads(),
            "child_processes": len(process.children(recursive=True)),
            "psutil": getattr(psutil, "__version__", "unknown"),
        })
    except Exception as exc:
        snapshot["resource_snapshot_gap"] = type(exc).__name__
    return snapshot


def object_row(index: int) -> dict[str, Any]:
    project = f"P13 v3 synthetic project {index % 200:03d}"
    topic = f"topic {index % 509:03d}"
    value = f"checkpoint {index:06d}"
    source = (
        f"The source for {project} records {topic}; the current checkpoint is {value}. "
        "This bounded synthetic record contains no user data."
    )
    return {
        "schema": SCHEMA,
        "ordinal": index,
        "object_id": f"p13-v3-object-{index:06d}",
        "source_event_key": f"p13-v3://source/{index:06d}",
        "source": source,
        "claim_value": value,
        "query": f"Which checkpoint is recorded for {project} {topic}?",
        "recorded_at": "2026-09-06T12:00:00Z",
    }


def prepare_dataset(dataset: Path, manifest: Path, logical_objects: int, *, label: str) -> dict[str, Any]:
    dataset.parent.mkdir(parents=True, exist_ok=True)
    if dataset.exists():
        old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        actual = sha256(dataset)
        if old.get("dataset_sha256") != actual or old.get("logical_objects") != logical_objects:
            raise RuntimeError(f"{label}_manifest_mismatch_refusing_overwrite")
    else:
        with dataset.open("x", encoding="utf-8", newline="\n") as stream:
            for index in range(logical_objects):
                stream.write(json.dumps(object_row(index), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    actual = sha256(dataset)
    payload = {
        "schema": "p13.dataset-manifest.v3",
        "dataset": str(dataset.resolve()),
        "dataset_sha256": actual,
        "logical_objects": logical_objects,
        "source_events": logical_objects,
        "claim_proposals": logical_objects,
        "total_records": logical_objects * RECORDS_PER_OBJECT,
        "object_schema": SCHEMA,
        "seed": 1301,
        "label": label,
        "generation": "deterministic synthetic source+claim rows",
        "write_path": "fixture only; real Core build is a separate phase",
        "environment": environment_fingerprint(),
    }
    if manifest.exists():
        old = json.loads(manifest.read_text(encoding="utf-8"))
        if old.get("dataset_sha256") != actual or old.get("total_records") != payload["total_records"]:
            raise RuntimeError(f"{label}_manifest_exists_with_different_content")
        return old
    write_once(manifest, json_bytes(payload))
    return payload


def prepare_v3() -> dict[str, Any]:
    primary = prepare_dataset(DATASET, MANIFEST, LOGICAL_OBJECTS, label="primary-10000")
    scale = prepare_dataset(SCALE_DATASET, SCALE_MANIFEST, SCALE_OBJECTS, label="scale-100000")
    return {
        "schema": "p13.prep-receipt.v3",
        "status": "prepared_only",
        "primary": primary,
        "scale": scale,
        "logical_object_definition": "one source event plus one claim proposal per logical object",
        "scale_note": "100000 is one bounded fixture-size/similarity pass, not a full long soak",
        "embedding_note": "no vectors generated in preparation; native phase uses deterministic TEST vectors only",
    }


def load_rows(dataset: Path, manifest: Path, expected: int) -> list[dict[str, Any]]:
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if meta.get("dataset_sha256") != sha256(dataset) or meta.get("logical_objects") != expected:
        raise RuntimeError("dataset_manifest_verification_failed")
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != expected or any(row.get("schema") != SCHEMA for row in rows):
        raise RuntimeError("synthetic_dataset_schema_or_count_failed")
    return rows


def make_context(data_dir: Path):
    from scope_recall.contracts import InstanceBinding, TrustedContext

    binding = InstanceBinding("p13-benchmark-v3", "p13-installation-v3", data_dir.resolve(), frozenset({SCOPE_ID}), True)
    return TrustedContext(binding, "p13-session-v3", frozenset({SCOPE_ID}), "human_direct", project_id="P13", branch_id="main")


def make_core(data_dir: Path):
    install_source_alias()
    from scope_recall.core import CoreConfig, MemoryCore

    context = make_context(data_dir)
    core = MemoryCore(CoreConfig(context.binding))
    core.initialize()
    return core, context


def event_for(row: dict[str, Any], *, key: str | None = None, content: str | None = None) -> dict[str, Any]:
    return {
        "protocol_version": "1.1",
        "source_event_key": key or row["source_event_key"],
        "source_revision": 1,
        "origin": "human_direct",
        "role": "user",
        "content": content if content is not None else row["source"],
        "occurred_at": row["recorded_at"],
        "recorded_at": row["recorded_at"],
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
        "dataset_id": "P13-V3-SYNTHETIC-ONLY",
    }


def proposal_for(ref: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": "1.1",
        "source_refs": [f"{ref}@1"],
        "claim_proposals": [{
            "kind": "fact", "subject": row["object_id"], "predicate": "checkpoint",
            "value_text": row["claim_value"], "conditions": [], "statement_kind": "assertion",
            "valid_from": row["recorded_at"], "valid_to": None,
            "evidence_spans": [{"source_ref": ref, "source_revision": 1, "quote": row["source"]}],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }


def build_real_core(rows: list[dict[str, Any]], run_dir: Path, limit: int | None) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = min(len(rows), limit if limit is not None else len(rows))
    progress_path = run_dir / "progress.jsonl"
    completed = sum(1 for line in progress_path.read_text(encoding="utf-8").splitlines() if line.strip()) if progress_path.exists() else 0
    if completed > target:
        raise RuntimeError("build_progress_exceeds_requested_limit")
    core, context = make_core(run_dir / "core-data")
    with progress_path.open("a", encoding="utf-8", newline="\n") as progress:
        for row in rows[completed:target]:
            receipt = core.record_event(context, event_for(row), scope_id=SCOPE_ID, remaining_seconds=30)
            if not receipt.event_refs:
                raise RuntimeError(f"source_not_inserted_at:{row['ordinal']}")
            saved = core.source(context, receipt.event_refs[0].ref, 1)
            if saved is None:
                raise RuntimeError(f"source_not_readable_at:{row['ordinal']}")
            accepted = core.accept_claim_proposals(context, proposal_for(saved.ref, row), scope_id=SCOPE_ID, remaining_seconds=30)
            if not accepted.items:
                raise RuntimeError(f"claim_not_accepted_at:{row['ordinal']}")
            progress.write(json.dumps({"ordinal": row["ordinal"], "source_ref": saved.ref}, ensure_ascii=False) + "\n")
            if (row["ordinal"] + 1) % 100 == 0:
                progress.flush()
                os.fsync(progress.fileno())
    return {
        "schema": "p13.build-receipt.v3",
        "status": "built_real_core",
        "logical_objects": target,
        "source_events": target,
        "claim_proposals": target,
        "total_records": target * RECORDS_PER_OBJECT,
        "progress_sha256": sha256(progress_path),
        "data_dir": str((run_dir / "core-data").resolve()),
        "environment": environment_fingerprint(),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(kind: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["elapsed_seconds"]) for item in samples if item.get("ok")]
    return {
        "kind": kind,
        "sample_count": len(samples),
        "successful_count": len(values),
        "failure_count": len(samples) - len(values),
        "p50_seconds": percentile(values, 0.5),
        "p95_seconds": percentile(values, 0.95),
        "min_seconds": min(values) if values else None,
        "max_seconds": max(values) if values else None,
    }


def _sample(kind: str, index: int, started: float, ok: bool, **extra: Any) -> dict[str, Any]:
    return {
        "schema": "p13.sample.v3",
        "kind": kind,
        "sample_index": index,
        "elapsed_seconds": time.perf_counter() - started,
        "ok": ok,
        **extra,
    }


def cold_child(dataset: Path, manifest: Path, data_dir: Path, index: int) -> dict[str, Any]:
    """One cold subprocess sample; the child has no warm Core objects."""
    started = time.perf_counter()
    try:
        rows = load_rows(dataset, manifest, LOGICAL_OBJECTS)
        core, context = make_core(data_dir)
        row = rows[(index * 997) % len(rows)]
        request = {"protocol_version": "1.1", "request_id": f"p13-v3-cold-{index}", "query": row["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}
        packet = core.recall_packet(context, request, deadline_seconds=AUTO_DEADLINE_SECONDS)
        return {"schema": "p13.sample.v3", "kind": "cold_recall_packet", "sample_index": index,
                "elapsed_seconds": time.perf_counter() - started, "ok": True,
                "status": packet.get("status"), "item_count": len(packet.get("items", [])),
                "deadline_seconds": AUTO_DEADLINE_SECONDS}
    except Exception as exc:
        return {"schema": "p13.sample.v3", "kind": "cold_recall_packet", "sample_index": index,
                "elapsed_seconds": time.perf_counter() - started, "ok": False,
                "error": type(exc).__name__, "deadline_seconds": AUTO_DEADLINE_SECONDS}


def measure_real_core(dataset: Path, manifest: Path, run_dir: Path, data_dir: Path) -> dict[str, Any]:
    rows = load_rows(dataset, manifest, LOGICAL_OBJECTS)
    run_dir.mkdir(parents=True, exist_ok=False)
    core, context = make_core(data_dir)
    hot: list[dict[str, Any]] = []
    for index in range(SAMPLE_HOT):
        row = rows[(index * 499) % len(rows)]
        request = {"protocol_version": "1.1", "request_id": f"p13-v3-hot-{index}", "query": row["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}
        started = time.perf_counter()
        try:
            packet = core.recall_packet(context, request, deadline_seconds=AUTO_DEADLINE_SECONDS)
            hot.append(_sample("hot_recall_packet", index, started, True, status=packet.get("status"), item_count=len(packet.get("items", [])), deadline_seconds=AUTO_DEADLINE_SECONDS))
        except Exception as exc:
            hot.append(_sample("hot_recall_packet", index, started, False, error=type(exc).__name__, deadline_seconds=AUTO_DEADLINE_SECONDS))
    cold: list[dict[str, Any]] = []
    for index in range(SAMPLE_COLD):
        child_run = run_dir / f"cold-{index:02d}.json"
        command = [sys.executable, str(SCRIPT), "--phase", "cold-child", "--dataset", str(dataset),
                   "--manifest", str(manifest), "--data-dir", str(data_dir), "--sample-index", str(index)]
        started = time.perf_counter()
        completed = None
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"cold_child_exit_{completed.returncode}")
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            payload["parent_elapsed_seconds"] = time.perf_counter() - started
            child_run.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            cold.append(payload)
        except Exception as exc:
            payload = _sample("cold_recall_packet", index, started, False, error=type(exc).__name__, deadline_seconds=AUTO_DEADLINE_SECONDS)
            payload["child_stdout_tail"] = completed.stdout[-500:] if completed is not None else ""
            child_run.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            cold.append(payload)
    capture: list[dict[str, Any]] = []
    for index in range(SAMPLE_CAPTURE):
        row = rows[(index * 503) % len(rows)]
        started = time.perf_counter()
        try:
            receipt = core.record_event(context, event_for(row, key=f"p13-v3://capture/{index:03d}", content=row["source"] + f" capture sample {index}"), scope_id=SCOPE_ID, remaining_seconds=10)
            capture.append(_sample("capture", index, started, bool(receipt.event_refs), disposition=receipt.disposition))
        except Exception as exc:
            capture.append(_sample("capture", index, started, False, error=type(exc).__name__))
    samples = hot + cold + capture
    sample_path = run_dir / "samples.jsonl"
    with sample_path.open("x", encoding="utf-8", newline="\n") as stream:
        for item in samples:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    summary = {
        "schema": "p13.measurement.v3",
        "status": "measured_real_core",
        "dataset_sha256": sha256(dataset),
        "environment": environment_fingerprint(),
        "logical_objects": LOGICAL_OBJECTS,
        "hot_recall": summarize("hot_recall_packet", hot),
        "cold_recall": summarize("cold_recall_packet", cold),
        "capture": summarize("capture", capture),
        "deadline_seconds": AUTO_DEADLINE_SECONDS,
        "sample_count": len(samples),
        "samples_sha256": sha256(sample_path),
        "samples_path": str(sample_path.resolve()),
        "semantic_quality": "not measured; lexical/Core path only",
    }
    write_once(run_dir / "summary.json", json_bytes(summary))
    return summary


def worker_probe(run_dir: Path, count: int = 20) -> dict[str, Any]:
    """Bounded real-Core worker drain using deterministic TEST-only ports."""
    install_source_alias()
    data_dir = run_dir / "worker-data"
    run_dir.mkdir(parents=True, exist_ok=False)
    core, context = make_core(data_dir)
    rows = load_rows(DATASET, MANIFEST, LOGICAL_OBJECTS)

    class TestConsolidation:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            return json.dumps({"protocol_version": "1.1",
                               "source_refs": [f"{s.ref}@{s.revision}" for s in sources],
                               "claim_proposals": [], "resume_proposals": [],
                               "reference_proposals": []}, ensure_ascii=False)

    class TestEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            return {"ref": source.ref, "revision": source.revision,
                    "vector": deterministic_vector(source.revision)}

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            if not lease_guard():
                raise RuntimeError("test_lease_guard_rejected")

    started = time.perf_counter()
    captured = 0
    for index in range(min(count, SAMPLE_CAPTURE)):
        row = rows[index]
        receipt = core.record_event(context, event_for(row, key=f"p13-v3://worker/{index:03d}"), scope_id=SCOPE_ID, remaining_seconds=10)
        if receipt.event_refs:
            captured += 1
    worker_receipt = core.drain_worker(context, owner_id="p13-v3-worker", max_items=max(1, min(count * 2, 40)),
                                       consolidation=TestConsolidation(), embed=TestEmbed(), remaining_seconds=10)
    result = {"schema": "p13.worker-probe.v3", "status": "bounded_worker_probe", "captured": captured,
              "requested": count, "elapsed_seconds": time.perf_counter() - started,
              "worker": {"processed": worker_receipt.processed, "completed": worker_receipt.completed,
                          "failed": worker_receipt.failed, "retried": worker_receipt.retried,
                          "skipped": worker_receipt.skipped, "stale": worker_receipt.stale,
                          "obsolete": worker_receipt.obsolete,
                          "states": [item.state for item in worker_receipt.items]},
              "embedding": "deterministic TEST vectors only; no semantic quality measured",
              "environment": environment_fingerprint()}
    write_once(run_dir / "receipt.json", json_bytes(result))
    return result


def deterministic_vector(seed: int, dimensions: int = NATIVE_DIMENSIONS) -> list[float]:
    return [((seed * 131 + index * 17) % 1009) / 1009.0 for index in range(dimensions)]


def native_probe(run_dir: Path, count: int = 8) -> dict[str, Any]:
    if not NATIVE_PY.is_file():
        raise RuntimeError(f"native_python_missing:{NATIVE_PY}")
    actual_python = Path(sys.executable).resolve()
    if actual_python != NATIVE_PY.resolve():
        raise RuntimeError(f"native_probe_requires_test_python:{NATIVE_PY};actual={actual_python}")
    install_source_alias()
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    from scope_recall.vector_store import VectorRecord

    run_dir.mkdir(parents=True, exist_ok=False)
    store = ProcessLanceVectorStore(run_dir / "lance", table_name="P13_TEST_vectors", dimensions=NATIVE_DIMENSIONS)
    started = time.perf_counter()
    rows = [VectorRecord(f"p13-v3-vector-{i}", "p13-native-scope", f"source-{i}", "memory", "deterministic TEST vector", "", "2026-09-06T12:00:00Z", deterministic_vector(i)) for i in range(count)]
    try:
        store.open()
        store.upsert_records([{"id": row.id, "scope_id": row.scope_id, "source": row.source, "target": row.target, "content": row.content, "summary": row.summary, "updated_at": row.updated_at, "vector": row.vector} for row in rows])
        hits = store.search(deterministic_vector(0), scope_id="p13-native-scope", limit=3)
        result = {"status": "native_observed", "rows": count, "dimensions": NATIVE_DIMENSIONS, "hit_count": len(hits), "elapsed_seconds": time.perf_counter() - started, "python": str(actual_python), "semantic_quality": "not measured; deterministic TEST vectors only"}
    finally:
        store.close()
    write_once(run_dir / "receipt.json", json_bytes(result))
    return result


def scale_sample(dataset: Path, manifest: Path, sample_size: int = 1000) -> dict[str, Any]:
    rows = load_rows(dataset, manifest, SCALE_OBJECTS)
    started = time.perf_counter()
    sample = rows[:: max(1, len(rows) // sample_size)][:sample_size]
    query_terms = {term for row in sample for term in row["query"].lower().split()}
    near = sum(1 for row in sample if any(term in row["source"].lower() for term in query_terms))
    return {"schema": "p13.scale-sample.v3", "status": "bounded_scale_sample", "logical_objects": SCALE_OBJECTS, "sample_size": len(sample), "simple_overlap_count": near, "elapsed_seconds": time.perf_counter() - started, "semantic_quality": "not a semantic score; deterministic lexical sanity only"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("prepare-v3", "build-v3", "measure-v3", "cold-child", "worker-probe", "native-probe", "scale-sample"), default="prepare-v3")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample-index", type=int, default=0)
    args = parser.parse_args()
    if args.phase == "prepare-v3":
        print(json.dumps(prepare_v3(), ensure_ascii=False))
        return 0
    if args.phase == "build-v3":
        if args.run_dir is None:
            raise SystemExit("--run-dir-required-for-build-v3")
        print(json.dumps(build_real_core(load_rows(args.dataset, args.manifest, LOGICAL_OBJECTS), args.run_dir, args.limit), ensure_ascii=False))
        return 0
    if args.phase == "measure-v3":
        if args.run_dir is None or args.data_dir is None:
            raise SystemExit("--run-dir-and-data-dir-required-for-measure-v3")
        print(json.dumps(measure_real_core(args.dataset, args.manifest, args.run_dir, args.data_dir), ensure_ascii=False))
        return 0
    if args.phase == "cold-child":
        if args.data_dir is None:
            raise SystemExit("--data-dir-required-for-cold-child")
        print(json.dumps(cold_child(args.dataset, args.manifest, args.data_dir, args.sample_index), ensure_ascii=False))
        return 0
    if args.phase == "worker-probe":
        if args.run_dir is None:
            raise SystemExit("--run-dir-required-for-worker-probe")
        print(json.dumps(worker_probe(args.run_dir, args.limit or 20), ensure_ascii=False))
        return 0
    if args.phase == "native-probe":
        if args.run_dir is None:
            raise SystemExit("--run-dir-required-for-native-probe")
        print(json.dumps(native_probe(args.run_dir), ensure_ascii=False))
        return 0
    if args.phase == "scale-sample":
        print(json.dumps(scale_sample(SCALE_DATASET, SCALE_MANIFEST), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
