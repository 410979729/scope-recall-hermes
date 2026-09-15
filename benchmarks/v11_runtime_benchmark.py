"""P13 bounded runtime measurement against the v1.1 Core API.

The fixture is synthetic and is written only through ``MemoryCore``.  The
benchmark deliberately has no host/provider dependency and does not insert
rows into SQLite directly. ``prepare-v2`` and ``build`` are separate so a
completed build can be measured repeatedly without recreating its database.
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
PREP = ROOT / ".execution" / "TEST-P13-PREP-v2"
DATASET = PREP / "dataset" / "objects.jsonl"
MANIFEST = PREP / "dataset" / "manifest.json"
LOGICAL_OBJECTS = 5_000
TOTAL_RECORDS = LOGICAL_OBJECTS * 2
SCHEMA = "p13.synthetic.core-record.v2"
SCOPE_ID = "p13-scope"
SAMPLE_HOT = 20
SAMPLE_COLD = 5
SAMPLE_CAPTURE = 20


def install_source_alias() -> None:
    """Match the test source bootstrap without importing the legacy host layer."""
    if "scope_recall" not in sys.modules:
        package = types.ModuleType("scope_recall")
        package.__path__ = [str(ROOT)]
        sys.modules["scope_recall"] = package


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def git_revision() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()
    except Exception:
        return None


def environment_fingerprint() -> dict[str, Any]:
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version,
        "python_executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(), "os": os.name, "machine": platform.machine(),
        "processor": platform.processor(), "cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version,
        "git_revision": git_revision(), "core_path": "scope_recall.core.MemoryCore", "model": "none",
        "external_embedding_latency": "not_measured", "measurement_mode": "basic_core_sqlite_lexical",
    }


def object_row(index: int) -> dict[str, Any]:
    project = f"P13 synthetic project {index % 100:03d}"
    topic = f"topic {index % 257:03d}"
    value = f"checkpoint {index:05d}"
    source = (f"The source for {project} records {topic}; the current checkpoint is {value}. "
              "This bounded synthetic record contains no user data.")
    return {"schema": SCHEMA, "ordinal": index, "object_id": f"p13-object-{index:05d}",
            "source_event_key": f"p13://source/{index:05d}", "source": source,
            "claim_value": value, "query": f"Which checkpoint is recorded for {project} {topic}?",
            "recorded_at": "2026-09-06T12:00:00Z"}


def prepare(dataset: Path, manifest: Path) -> dict[str, Any]:
    """Create the independent v2 dataset, refusing to overwrite it."""
    dataset.parent.mkdir(parents=True, exist_ok=True)
    if dataset.exists():
        actual = sha256(dataset)
        old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        if old.get("dataset_sha256") != actual or old.get("logical_objects") != LOGICAL_OBJECTS:
            raise RuntimeError("existing_p13_v2_dataset_manifest_mismatch_refusing_overwrite")
    else:
        with dataset.open("x", encoding="utf-8", newline="\n") as stream:
            for index in range(LOGICAL_OBJECTS):
                stream.write(json.dumps(object_row(index), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush(); os.fsync(stream.fileno())
    actual = sha256(dataset)
    payload = {"schema": "p13.dataset-manifest.v2", "dataset": str(dataset.resolve()),
               "dataset_sha256": actual, "logical_objects": LOGICAL_OBJECTS,
               "source_events": LOGICAL_OBJECTS, "claim_proposals": LOGICAL_OBJECTS,
               "total_records": TOTAL_RECORDS, "object_schema": SCHEMA, "seed": 1301,
               "generation": "deterministic synthetic source+claim rows",
               "write_path": "MemoryCore.record_event + MemoryCore.accept_claim_proposals; no direct SQL inserts",
               "environment": environment_fingerprint()}
    if manifest.exists():
        old = json.loads(manifest.read_text(encoding="utf-8"))
        if old.get("dataset_sha256") != actual or old.get("total_records") != TOTAL_RECORDS:
            raise RuntimeError("manifest_exists_with_different_content_refusing_overwrite")
        return old
    write_once(manifest, json_bytes(payload)); return payload


def load_rows(dataset: Path, manifest: Path) -> list[dict[str, Any]]:
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if meta.get("dataset_sha256") != sha256(dataset) or meta.get("logical_objects") != LOGICAL_OBJECTS:
        raise RuntimeError("dataset_manifest_verification_failed")
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != LOGICAL_OBJECTS or any(row.get("schema") != SCHEMA for row in rows):
        raise RuntimeError("synthetic_dataset_schema_or_count_failed")
    return rows


def make_context(data_dir: Path):
    from scope_recall.contracts import InstanceBinding, TrustedContext
    binding = InstanceBinding("p13-benchmark", "p13-installation", data_dir.resolve(), frozenset({SCOPE_ID}), True)
    return TrustedContext(binding, "p13-session", frozenset({SCOPE_ID}), "human_direct", project_id="P13", branch_id="main")


def make_core(data_dir: Path):
    install_source_alias()
    from scope_recall.core import CoreConfig, MemoryCore
    context = make_context(data_dir)
    core = MemoryCore(CoreConfig(context.binding)); core.initialize()
    return core, context


def proposal_for(ref: str, row: dict[str, Any]) -> dict[str, Any]:
    return {"protocol_version": "1.1", "source_refs": [f"{ref}@1"], "claim_proposals": [{
        "kind": "fact", "subject": row["object_id"], "predicate": "checkpoint",
        "value_text": row["claim_value"], "conditions": [], "statement_kind": "assertion",
        "valid_from": row["recorded_at"], "valid_to": None,
        "evidence_spans": [{"source_ref": ref, "source_revision": 1, "quote": row["source"]}],
    }], "resume_proposals": [], "reference_proposals": []}


def event_for(row: dict[str, Any], *, key: str | None = None, content: str | None = None) -> dict[str, Any]:
    return {"protocol_version": "1.1", "source_event_key": key or row["source_event_key"], "source_revision": 1,
            "origin": "human_direct", "role": "user", "content": content if content is not None else row["source"],
            "occurred_at": row["recorded_at"], "recorded_at": row["recorded_at"], "time_precision": "instant",
            "capture_state": "complete", "evidence_refs": [], "dataset_id": "P13-SYNTHETIC-ONLY"}


def build(rows: list[dict[str, Any]], run_dir: Path, limit: int | None) -> dict[str, Any]:
    data_dir = run_dir / "core-data"; progress_path = run_dir / "build-progress.json"; run_dir.mkdir(parents=True, exist_ok=True)
    target = min(len(rows), limit if limit is not None else len(rows))
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.is_file() else {"schema": "p13.build-progress.v2", "completed": 0, "count": target, "source_refs": []}
    completed = int(progress.get("completed", 0))
    if completed > target: raise RuntimeError("build_progress_exceeds_requested_limit")
    core, context = make_core(data_dir)
    for row in rows[completed:target]:
        receipt = core.record_event(context, event_for(row), scope_id=SCOPE_ID, remaining_seconds=10)
        saved = core.source(context, receipt.event_refs[0].ref, 1)
        if saved is None: raise RuntimeError(f"source_not_readable_at:{row['ordinal']}")
        accepted = core.accept_claim_proposals(context, proposal_for(saved.ref, row), scope_id=SCOPE_ID, remaining_seconds=10)
        if not accepted.items: raise RuntimeError(f"claim_not_accepted_at:{row['ordinal']}")
        completed += 1
        progress = {"schema": "p13.build-progress.v2", "completed": completed, "count": target,
                    "source_refs": [*progress.get("source_refs", []), saved.ref]}
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "built", "logical_objects": completed, "source_events": completed,
            "claim_proposals": completed, "total_records": completed * 2,
            "progress_sha256": sha256(progress_path), "data_dir": str(data_dir.resolve())}


def sample(kind: str, index: int, elapsed: float, ok: bool, *, error: str = "", **extra: Any) -> dict[str, Any]:
    return {"schema": "p13.sample.v2", "kind": kind, "sample_index": index,
            "elapsed_seconds": elapsed, "ok": ok, "error": error, **extra}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values: return None
    ordered = sorted(values); position = (len(ordered) - 1) * fraction; low = int(position); high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(kind: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(s["elapsed_seconds"]) for s in samples if s.get("ok")]
    return {"kind": kind, "sample_count": len(samples), "successful_count": len(values), "failure_count": len(samples) - len(values),
            "p50_seconds": percentile(values, .5), "p95_seconds": percentile(values, .95),
            "min_seconds": min(values) if values else None, "max_seconds": max(values) if values else None}


def measure_capture(core, context, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = []
    for index in range(SAMPLE_CAPTURE):
        row = rows[(index * 503) % len(rows)]; key = f"p13://capture/{index:02d}"; started = time.perf_counter()
        try:
            receipt = core.record_event(context, event_for(row, key=key, content=row["source"] + f" capture sample {index}"), scope_id=SCOPE_ID, remaining_seconds=10)
            source = core.source(context, receipt.event_refs[0].ref, 1)
            if source is None: raise RuntimeError("capture_source_missing")
            accepted = core.accept_claim_proposals(context, proposal_for(source.ref, row), scope_id=SCOPE_ID, remaining_seconds=10)
            samples.append(sample("capture_and_claim", index, time.perf_counter() - started, bool(accepted.items), records=2))
        except Exception as exc:
            samples.append(sample("capture_and_claim", index, time.perf_counter() - started, False, error=type(exc).__name__, records=0))
    return samples


def measure_hot(core, context, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = []
    for index in range(SAMPLE_HOT):
        row = rows[(index * 499) % len(rows)]; request = {"protocol_version": "1.1", "request_id": f"p13-hot-{index}", "query": row["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}; started = time.perf_counter()
        try:
            packet = core.recall_packet(context, request, deadline_seconds=5)
            samples.append(sample("hot_recall_packet", index, time.perf_counter() - started, True, status=packet.get("status"), item_count=len(packet.get("items", []))))
        except Exception as exc:
            samples.append(sample("hot_recall_packet", index, time.perf_counter() - started, False, error=type(exc).__name__))
    return samples


def cold_child(data_dir: Path, dataset: Path, manifest: Path) -> int:
    rows = load_rows(dataset, manifest); started = time.perf_counter()
    try:
        core, context = make_core(data_dir); request = {"protocol_version": "1.1", "request_id": "p13-cold", "query": rows[0]["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}; packet = core.recall_packet(context, request, deadline_seconds=5)
        print(json.dumps(sample("cold_start_and_recall", 0, time.perf_counter() - started, True, status=packet.get("status"), item_count=len(packet.get("items", []))), ensure_ascii=False), flush=True); return 0
    except Exception as exc:
        print(json.dumps(sample("cold_start_and_recall", 0, time.perf_counter() - started, False, error=type(exc).__name__), ensure_ascii=False), flush=True); return 1


def measure(dataset: Path, manifest: Path, run_dir: Path, data_dir: Path) -> dict[str, Any]:
    rows = load_rows(dataset, manifest); run_dir.mkdir(parents=True, exist_ok=False); core, context = make_core(data_dir)
    hot = measure_hot(core, context, rows); capture = measure_capture(core, context, rows); cold = []
    for index in range(SAMPLE_COLD):
        started = time.perf_counter(); command = [sys.executable, str(SCRIPT), "--phase", "cold-child", "--dataset", str(dataset), "--manifest", str(manifest), "--data-dir", str(data_dir)]
        try:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45); lines = [line for line in result.stdout.splitlines() if line.strip()]; child = json.loads(lines[-1]) if lines else {}
            cold.append(sample("cold_start_total", index, time.perf_counter() - started, result.returncode == 0 and bool(child.get("ok")), error=child.get("error", "") or ("child_exit" if result.returncode else ""), child=child, stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest()))
        except Exception as exc: cold.append(sample("cold_start_total", index, time.perf_counter() - started, False, error=type(exc).__name__))
    samples = hot + capture + cold; samples_path = run_dir / "samples.jsonl"
    with samples_path.open("x", encoding="utf-8", newline="\n") as stream:
        for item in samples: stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    summary = {"status": "measured", "dataset_sha256": sha256(dataset), "environment": environment_fingerprint(), "model": "none", "basic_measurement": "Core SQLite lexical path", "external_embedding": "not_measured", "hot_recall": summarize("hot_recall_packet", hot), "capture": summarize("capture_and_claim", capture), "cold_start": summarize("cold_start_total", cold), "samples_sha256": sha256(samples_path), "samples_path": str(samples_path.resolve())}
    write_once(run_dir / "summary.json", json_bytes(summary)); return summary


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--phase", choices=("prepare-v2", "build", "measure", "cold-child"), default="prepare-v2"); parser.add_argument("--dataset", type=Path, default=DATASET); parser.add_argument("--manifest", type=Path, default=MANIFEST); parser.add_argument("--run-dir", type=Path); parser.add_argument("--data-dir", type=Path); parser.add_argument("--limit", type=int); args = parser.parse_args()
    if args.phase == "prepare-v2": print(json.dumps(prepare(args.dataset, args.manifest), ensure_ascii=False)); return 0
    rows = load_rows(args.dataset, args.manifest)
    if args.phase == "build":
        if args.run_dir is None: raise SystemExit("--run-dir-required-for-build")
        print(json.dumps(build(rows, args.run_dir, args.limit), ensure_ascii=False)); return 0
    if args.phase == "cold-child":
        if args.data_dir is None: raise SystemExit("--data-dir-required-for-cold-child")
        return cold_child(args.data_dir, args.dataset, args.manifest)
    if args.run_dir is None or args.data_dir is None: raise SystemExit("--run-dir-and-data-dir-required-for-measure")
    print(json.dumps(measure(args.dataset, args.manifest, args.run_dir, args.data_dir), ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
