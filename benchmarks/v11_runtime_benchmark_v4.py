"""P13 v4 actual bounded Core/Lance measurement.

This runner is TEST-only and independent from the preserved v2/v3 receipts.
The 10k path uses real SQLite hydration plus a native 3072-dimensional Lance
projection.  The 100k path is a separately copied, schema-checked SQLite
fixture plus a batched 32-dimensional deterministic Lance interference index;
neither path claims semantic model quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from typing import Any

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
V3 = ROOT / "benchmarks" / "v11_runtime_benchmark_v3.py"
V3_DIR = ROOT / ".execution" / "TEST-P13-PREP-v3"
OUT = ROOT / ".execution" / "TEST-P13-PREP-v4"
CORE10 = V3_DIR / "runs" / "core-10000" / "core-data"
PROGRESS = V3_DIR / "runs" / "core-10000" / "progress.jsonl"
DATASET10 = V3_DIR / "dataset-10000" / "objects.jsonl"
MANIFEST10 = V3_DIR / "dataset-10000" / "manifest.json"
DATASET100 = V3_DIR / "dataset-100000" / "objects.jsonl"
MANIFEST100 = V3_DIR / "dataset-100000" / "manifest.json"
NATIVE_PY = Path(os.environ.get("P13_NATIVE_PY", str(ROOT.parent / "scope-recall-v1.1" / ".execution" / "TEST-P08-native" / "Scripts" / "python.exe")))
SCOPE = "p13-scope-v3"
SPACE_ID = ""  # filled from the production policy at runtime
DIM10 = 3072
DIM100 = 32
HOT = 20
COLD = 5
CAPTURE = 20


def alias() -> None:
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


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def source_refs() -> dict[int, str]:
    result: dict[int, str] = {}
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        result[int(item["ordinal"])] = str(item["source_ref"])
    if len(result) != 10_000:
        raise RuntimeError(f"core_progress_count:{len(result)}")
    return result


def actual_sqlite_counts(data_dir: Path) -> dict[str, int]:
    db = data_dir / "memory.sqlite3"
    with sqlite3.connect(db) as connection:
        return {
            "source_events": int(connection.execute("SELECT count(*) FROM source_events").fetchone()[0]),
            "claims": int(connection.execute("SELECT count(*) FROM claims").fetchone()[0]),
            "claim_versions": int(connection.execute("SELECT count(*) FROM claim_versions").fetchone()[0]),
            "lexical_projection": int(connection.execute("SELECT count(*) FROM lexical_projection").fetchone()[0]),
        }


def deterministic_vector(seed: int, dimensions: int) -> tuple[float, ...]:
    return tuple(((seed * 131 + index * 17) % 1009) / 1009.0 for index in range(dimensions))


def make_record(record_cls, ref: str, index: int, dimensions: int, *, agent: str, installation: str):
    return record_cls(
        object_kind="event", object_ref=ref, object_revision=1,
        vector_id=f"p13-v4-{dimensions}-{index:06d}", embedding_space=SPACE_ID,
        embedding=deterministic_vector(index, dimensions), scope_id=SCOPE,
        agent_id=agent, installation_id=installation, project_id="P13", branch_id="main",
    )


def build_index(index_dir: Path, refs: dict[int, str], *, dimensions: int, count: int, batch: int = 250) -> dict[str, Any]:
    alias()
    from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorRecord
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    if not NATIVE_PY.is_file():
        raise RuntimeError(f"native_python_missing:{NATIVE_PY}")
    store = ProcessLanceVectorStore(index_dir, table_name=f"P13_v4_{dimensions}", dimensions=dimensions)
    started = time.perf_counter()
    store.open()
    try:
        writer = LanceIndexWriter(store)
        for begin in range(0, count, batch):
            records = [make_record(LanceVectorRecord, refs.get(i, f"p13-v4-scale-{i:06d}"), i, dimensions,
                                    agent="p13-benchmark-v3", installation="p13-installation-v3")
                       for i in range(begin, min(begin + batch, count))]
            writer.upsert_records(records)
        actual = store.count_rows()
    finally:
        store.close()
    if actual != count:
        raise RuntimeError(f"native_count_mismatch:{actual}:{count}")
    return {"rows_requested": count, "rows_observed": actual, "dimensions": dimensions,
            "batch": batch, "elapsed_seconds": time.perf_counter() - started,
            "semantic_quality": "not measured; deterministic TEST vectors only"}


def copy_core(source: Path, target: Path) -> None:
    if target.exists():
        return
    shutil.copytree(source, target)
    db = target / "memory.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE instance_meta SET data_directory=? WHERE singleton=1",
                           (os.path.normcase(os.path.abspath(os.fspath(target))),))
        connection.commit()


def core_for(data_dir: Path, vectors=None):
    alias()
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.recall_policy import RecallPolicy
    binding = InstanceBinding("p13-benchmark-v3", "p13-installation-v3", data_dir.resolve(), frozenset({SCOPE}), True)
    context = TrustedContext(binding, "p13-v4-session", frozenset({SCOPE}), "human_direct", project_id="P13", branch_id="main")
    # Development-only deterministic threshold so this measurement exercises
    # the vector path; it is not a calibrated production value.
    core = MemoryCore(CoreConfig(binding), vectors=vectors, retrieval_policy=RecallPolicy(vector_threshold=0.0))
    core.initialize()
    return core, context


class QueryEmbedding:
    def __init__(self, dimensions: int):
        self.dimensions = dimensions
        self.ordinal = 0

    def select(self, ordinal: int) -> None:
        self.ordinal = ordinal

    def embed_query(self, text: str, *, remaining_seconds: float):
        del text
        if remaining_seconds <= 0:
            return ()
        return deterministic_vector(self.ordinal, self.dimensions)


def measure_10k(index_dir: Path, data_dir: Path, refs: dict[int, str], dataset: list[dict[str, Any]]) -> dict[str, Any]:
    alias()
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import RecallPolicy
    import importlib
    policy = importlib.import_module("scope_recall.core.recall_policy")
    global SPACE_ID
    SPACE_ID = policy.SPACE_ID
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    store = ProcessLanceVectorStore(index_dir, table_name=f"P13_v4_{DIM10}", dimensions=DIM10)
    store.open()
    query_embedding = QueryEmbedding(DIM10)
    port = LanceVectorPort(store, query_embedding, expected_embedding_space=SPACE_ID)
    core, context = core_for(data_dir, vectors=port)
    results: list[dict[str, Any]] = []
    for kind, indexes in (("hot", [(i * 499) % 10_000 for i in range(HOT)]), ("capture", [(i * 503) % 10_000 for i in range(CAPTURE)])):
        for index in indexes:
            query_embedding.select(index)
            started = time.perf_counter()
            expected = refs[index]
            try:
                if kind == "hot":
                    request = {"protocol_version": "1.1", "request_id": f"p13-v4-hot-{index}",
                               "query": dataset[index]["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}
                    packet = core.recall_packet(context, request, deadline_seconds=1.5)
                    items = list(packet.get("items", []))
                    hit = any(getattr(item, "ref", None) == expected or (isinstance(item, dict) and item.get("ref") == expected) for item in items)
                    results.append({"kind": kind, "index": index, "elapsed_seconds": time.perf_counter() - started,
                                    "ok": bool(hit), "expected_ref": expected, "hit": hit,
                                    "status": packet.get("status"), "gaps": list(packet.get("gaps", [])),
                                    "item_count": len(items)})
                else:
                    from v11_runtime_benchmark_v3 import event_for
                    receipt = core.record_event(context, event_for(dataset[index], key=f"p13-v4-capture/{index:06d}"), scope_id=SCOPE, remaining_seconds=10)
                    results.append({"kind": kind, "index": index, "elapsed_seconds": time.perf_counter() - started,
                                    "ok": bool(receipt.event_refs), "expected_ref": expected,
                                    "hit": None, "status": receipt.disposition, "gaps": [], "item_count": 0})
            except Exception as exc:
                results.append({"kind": kind, "index": index, "elapsed_seconds": time.perf_counter() - started,
                                "ok": False, "expected_ref": expected, "hit": False,
                                "status": "error", "gaps": [type(exc).__name__], "item_count": 0})
    store.close()
    cold: list[dict[str, Any]] = []
    for index in range(COLD):
        started = time.perf_counter()
        command = [sys.executable, str(SCRIPT), "--phase", "cold-child", "--index-dir", str(index_dir),
                   "--data-dir", str(data_dir), "--ordinal", str((index * 997) % 10_000)]
        completed = None
        try:
            completed = subprocess.run(command, cwd=os.environ.get("TEMP") or str(ROOT), capture_output=True,
                                       text=True, timeout=30, check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"cold_child_exit:{completed.returncode}")
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            payload["parent_elapsed_seconds"] = time.perf_counter() - started
            cold.append(payload)
        except Exception as exc:
            cold.append({"kind": "cold", "index": index, "elapsed_seconds": time.perf_counter() - started,
                         "parent_elapsed_seconds": time.perf_counter() - started, "ok": False,
                         "status": "error", "gaps": [type(exc).__name__], "item_count": 0})
    cold_good = [item["parent_elapsed_seconds"] for item in cold if item.get("ok")]
    values = {kind: [r["elapsed_seconds"] for r in results if r["kind"] == kind and r["ok"]] for kind in ("hot", "capture")}
    def summary(kind: str) -> dict[str, Any]:
        sample = [r for r in results if r["kind"] == kind]
        good = sorted(values[kind])
        return {"sample_count": len(sample), "successful_count": len(good), "empty_or_failed_count": len(sample) - len(good),
                "p50_seconds": good[(len(good)-1)//2] if good else None,
                "p95_seconds": good[min(len(good)-1, int(len(good)*.95))] if good else None}
    return {"schema": "p13.measurement.v4", "logical_objects": 10_000,
            "sqlite_counts": actual_sqlite_counts(data_dir), "hot": summary("hot"),
            "capture": summary("capture"),
            "cold": {"sample_count": len(cold), "successful_count": len(cold_good),
                     "empty_or_failed_count": len(cold) - len(cold_good),
                     "p50_seconds": sorted(cold_good)[(len(cold_good)-1)//2] if cold_good else None,
                     "p95_seconds": sorted(cold_good)[min(len(cold_good)-1, int(len(cold_good)*.95))] if cold_good else None},
            "samples": results + cold, "deadline_seconds": 1.5,
            "semantic_quality": "not measured; injected deterministic TEST query vectors"}


def cold_query(index_dir: Path, data_dir: Path, ordinal: int) -> dict[str, Any]:
    """One real cold Core+SQLite+native-Lance query in a fresh process."""
    alias()
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID as frozen_space
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    dataset = rows(DATASET10)
    refs = source_refs()
    started = time.perf_counter()
    store = ProcessLanceVectorStore(index_dir, table_name=f"P13_v4_{DIM10}", dimensions=DIM10)
    store.open()
    embedding = QueryEmbedding(DIM10)
    embedding.select(ordinal)
    core, context = core_for(data_dir, vectors=LanceVectorPort(store, embedding, expected_embedding_space=frozen_space))
    request = {"protocol_version": "1.1", "request_id": f"p13-v4-cold-{ordinal}",
               "query": dataset[ordinal]["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}
    try:
        packet = core.recall_packet(context, request, deadline_seconds=1.5)
        items = list(packet.get("items", []))
        hit = any(getattr(item, "ref", None) == refs[ordinal] or (isinstance(item, dict) and item.get("ref") == refs[ordinal]) for item in items)
        return {"kind": "cold", "index": ordinal, "parent_elapsed_seconds": time.perf_counter() - started,
                "ok": bool(hit), "expected_ref": refs[ordinal], "hit": hit, "status": packet.get("status"),
                "gaps": list(packet.get("gaps", [])), "item_count": len(items)}
    except Exception as exc:
        return {"kind": "cold", "index": ordinal, "parent_elapsed_seconds": time.perf_counter() - started,
                "ok": False, "expected_ref": refs[ordinal], "hit": False, "status": "error",
                "gaps": [type(exc).__name__], "item_count": 0}
    finally:
        store.close()


def build_scale_sqlite(source: Path, target: Path, rows100: list[dict[str, Any]], *, count: int = 100_000) -> dict[str, Any]:
    copy_core(source, target)
    db = target / "memory.sqlite3"
    alias()
    from scope_recall.core.events import lexical_terms
    now = "2026-09-06T12:00:00Z"
    with sqlite3.connect(db) as connection:
        existing = int(connection.execute("SELECT count(*) FROM source_events").fetchone()[0])
        if existing < count:
            for begin in range(existing, count, 1000):
                batch = []
                terms = []
                for index in range(begin, min(begin + 1000, count)):
                    row = rows100[index]
                    ref = f"p13-v4-scale-event-{index:06d}"
                    content = row["source"]
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    event_hash = hashlib.sha256(f"{ref}@1:{content}".encode()).hexdigest()
                    batch.append((ref, f"p13-v4-scale-source/{index:06d}", 1, ref, 0, None, SCOPE, "p13-v4-session", "P13", "main", "human_direct", "user", content, digest, event_hash, now, now, now, "instant", "complete", None, "P13-V4-SQLITE-FIXTURE", None, "{}", "[]", 0, 0))
                    terms.extend((term, ref, 1) for term in lexical_terms(content))
                connection.executemany("""INSERT OR IGNORE INTO source_events(event_id,source_event_key,source_revision,source_group_key,segment_index,segment_total,scope_id,session_id,project_id,branch_id,origin,role,content,content_sha256,event_sha256,occurred_at,recorded_at,persisted_at,time_precision,capture_state,source_original_origin,dataset_id,import_provenance_sha256,extra_json,capture_gaps_json,read_blocked,suppressed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
                connection.executemany("INSERT OR IGNORE INTO lexical_projection(term,event_id,source_revision) VALUES (?,?,?)", terms)
            connection.execute(
                "UPDATE instance_meta SET data_directory=? WHERE singleton=1",
                (os.path.normcase(os.path.abspath(os.fspath(target))),),
            )
            connection.commit()
        observed = int(connection.execute("SELECT count(*) FROM source_events").fetchone()[0])
    if observed != count:
        raise RuntimeError(f"scale_sqlite_count:{observed}:{count}")
    return {"source_events_observed": observed, "fixture": "batch SQLite source+lexical rows validated against Core schema and v3 source shape"}


def scale_probe(index_dir: Path, data_dir: Path, refs: dict[int, str], rows100: list[dict[str, Any]]) -> dict[str, Any]:
    alias()
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import SPACE_ID as frozen_space
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    store = ProcessLanceVectorStore(index_dir, table_name=f"P13_v4_{DIM100}", dimensions=DIM100)
    store.open()
    embedding = QueryEmbedding(DIM100)
    port = LanceVectorPort(store, embedding, expected_embedding_space=frozen_space)
    core, context = core_for(data_dir, vectors=port)
    samples = []
    for index in (17, 4096, 9001):
        embedding.select(index)
        started = time.perf_counter()
        request = {"protocol_version": "1.1", "request_id": f"p13-v4-scale-{index}", "query": rows100[index]["query"], "mode": "current", "max_items": 4, "budget_tokens": 1200}
        try:
            result = core.recall(context, request, deadline_seconds=1.5)
            samples.append({"index": index, "elapsed_seconds": time.perf_counter() - started, "ok": bool(result.items),
                            "item_count": len(result.items), "gaps": list(result.gaps), "expected_source_ref": refs.get(index)})
        except Exception as exc:
            samples.append({"index": index, "elapsed_seconds": time.perf_counter() - started, "ok": False, "item_count": 0, "gaps": [type(exc).__name__], "expected_source_ref": refs.get(index)})
    observed = store.count_rows()
    store.close()
    return {"schema": "p13.scale.v4", "sqlite_counts": actual_sqlite_counts(data_dir), "native_rows_observed": observed,
            "dimensions": DIM100, "samples": samples, "semantic_quality": "not measured; deterministic TEST vectors only"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("build-10k-index", "measure-10k", "cold-child", "build-100k", "scale-probe"), required=True)
    parser.add_argument("--run-dir", type=Path, default=OUT)
    parser.add_argument("--index-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--ordinal", type=int, default=0)
    args = parser.parse_args()
    global SPACE_ID
    alias()
    from scope_recall.core.recall_policy import SPACE_ID as frozen_space
    SPACE_ID = frozen_space
    refs = source_refs()
    data10 = rows(DATASET10)
    if args.phase == "build-10k-index":
        print(json.dumps({"phase": args.phase, **build_index(args.index_dir or args.run_dir / "lance-10k", refs, dimensions=DIM10, count=10_000)}, ensure_ascii=False)); return 0
    if args.phase == "measure-10k":
        print(json.dumps(measure_10k(args.index_dir, args.data_dir, refs, data10), ensure_ascii=False)); return 0
    if args.phase == "cold-child":
        print(json.dumps(cold_query(args.index_dir, args.data_dir, args.ordinal), ensure_ascii=False)); return 0
    data100 = rows(DATASET100)
    if args.phase == "build-100k":
        scale_data = args.run_dir / "sqlite-100k"
        sqlite_result = build_scale_sqlite(CORE10, scale_data, data100)
        index_result = build_index(args.index_dir or args.run_dir / "lance-100k-32", refs, dimensions=DIM100, count=100_000, batch=500)
        print(json.dumps({"phase": args.phase, "sqlite": sqlite_result, "index": index_result}, ensure_ascii=False)); return 0
    if args.phase == "scale-probe":
        print(json.dumps(scale_probe(args.index_dir, args.data_dir, refs, data100), ensure_ascii=False)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
