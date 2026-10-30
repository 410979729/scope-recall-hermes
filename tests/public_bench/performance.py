"""Frozen Windows/Linux synthetic scale benchmark without any embedding request.

Seed population uses captured row templates and the current lexical tokenizer;
measured writes, worker passes and recall use the production APIs. Vectors are
float32 synthetic values. The OS page cache is not flushed on a shared machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from cache import RequestCache, digest
from cases import Case, Turn
from environment import machine
from lab import Lab
from ports import Ports
from scope_recall.core.events import lexical_terms
from scope_recall.runtime.lance_port import physical_partition_scope_id

NOW = "2026-01-09T12:00:00Z"
SCOPES = frozenset({"public", *(f"public-{i}" for i in range(1, 8))})


def distribution(samples: list[float]) -> dict:
    """Retain raw milliseconds and nearest-rank percentiles, including startup costs."""
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "p50_ms": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "samples_ms": samples,
    }


def freeze(output: Path) -> dict:
    """Write immutable workload conditions before creating or measuring either scale."""
    conditions = {
        "schema": "scale-bench/1",
        "machine": machine(output),
        "seed": 42,
        "dimensions": 3072,
        "scales": [350000, 1000000],
        "sources_per_scale": "scale",
        "claims_per_scale": "scale // 10",
        "vectors_per_scale": "scale; source vectors only",
        "scope_count": 8,
        "vector_distribution": "normalized Gaussian float32; every 100th vector duplicates its predecessor",
        "foreground_entries": 4,
        "foreground_max_inflight": 1,
        "input_rate_per_second": 4,
        "foreground_samples": 60,
        "backfill_items": 200,
        "backfill_concurrency": 1,
        "cache": "application first-open then warm; OS cache uncontrolled",
        "backend": "3.16.1 default LanceDB with Windows helper; no vector index initially",
        "query_embedding_network_ms": None,
        "query_embedding": "deterministic local synthetic port",
        "deadlines": {
            "auto_p95_ms": 1000,
            "first_recall_ms": 2000,
            "retrieval_p95_ms": 200,
            "session_start_p95_ms": 200,
        },
        "clock": "real monotonic for measurements; injected UTC for corpus and queue age",
    }
    destination = output / "conditions.json"
    if destination.exists():
        recorded = json.loads(destination.read_text(encoding="utf-8"))
        if stable_conditions(recorded) != stable_conditions(conditions):
            raise ValueError("frozen conditions differ")
        return recorded
    destination.write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    return conditions


def stable_conditions(value):
    """Ignore observation time and disk occupancy while retaining hardware and workload."""
    if isinstance(value, dict):
        return {
            key: stable_conditions(item) for key, item in value.items() if key not in {"observed_at", "used", "free"}
        }
    if isinstance(value, list):
        return [stable_conditions(item) for item in value]
    return value


def seed_truth(lab: Lab, count: int) -> list[str]:
    """Populate a synthetic schema from actual captured rows; never touch an existing user store."""
    seed = Turn("TEST-performance-template", "TEST-template", "user", "TESTtopic00000 color amber.", NOW)
    receipt = lab.capture(seed)
    source_ref = receipt.event_refs[0].ref
    source = lab.core.source(lab.context, source_ref, 1)
    proposal = {
        "kind": "decision",
        "subject": "TESTtopic00000",
        "predicate": "color",
        "value_text": "amber",
        "conditions": [],
        "statement_kind": "decision",
        "valid_from": NOW,
        "valid_to": None,
        "evidence_spans": [{"source_ref": source_ref, "source_revision": 1, "quote": source.event["content"]}],
    }
    lab.core.records.accept_claim_proposals(
        lab.context,
        {
            "protocol_version": "1.1",
            "source_refs": [f"{source_ref}@1"],
            "claim_proposals": [proposal],
            "resume_proposals": [],
            "reference_proposals": [],
        },
        scope_id="public",
        remaining_seconds=60,
    )
    refs = []
    with sqlite3.connect(lab.core.storage.path) as connection:
        connection.row_factory = sqlite3.Row
        template = dict(connection.execute("SELECT * FROM source_events WHERE event_id=?", (source_ref,)).fetchone())
        claim = dict(connection.execute("SELECT * FROM claims LIMIT 1").fetchone())
        version = dict(connection.execute("SELECT * FROM claim_versions LIMIT 1").fetchone())
        columns = list(template)
        insert = f"INSERT INTO source_events({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        term_ids = dict(connection.execute("SELECT term,term_id FROM lexical_terms"))
        next_term = max(term_ids.values(), default=0) + 1
        base_id = template["source_id"]
        for start in range(1, count, 2000):
            rows, postings, claims, versions, links = [], [], [], [], []
            for index in range(start, min(count, start + 2000)):
                ref = "event-" + hashlib.sha256(f"TEST-performance:{index}".encode()).hexdigest()
                refs.append(ref)
                topic = f"TESTtopic{index % 10000:05d}"
                text = f"{topic} color amber. Synthetic source number {index}."
                row = dict(
                    template,
                    event_id=ref,
                    source_event_key=f"TEST-performance:{index}",
                    source_group_key=f"TEST-performance:{index}",
                    session_id=f"TEST-session-{index // 10}",
                    scope_id="public" if index % 8 == 0 else f"public-{index % 8}",
                    content=text,
                    content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    event_sha256=digest([index, text]),
                    source_id=base_id + index,
                )
                rows.append(tuple(row[name] for name in columns))
                for term in lexical_terms(text):
                    if term not in term_ids:
                        term_ids[term] = next_term
                        connection.execute("INSERT INTO lexical_terms VALUES (?,?)", (next_term, term))
                        next_term += 1
                    postings.append((term_ids[term], row["source_id"]))
                if index % 10 == 0:
                    cid = "claim-" + hashlib.sha256(f"TEST-claim:{index}".encode()).hexdigest()
                    head = dict(
                        claim, claim_id=cid, scope_id=row["scope_id"], subject=topic, slot_key=digest([index, "color"])
                    )
                    payload = dict(
                        proposal,
                        subject=topic,
                        evidence_spans=[{"source_ref": ref, "source_revision": 1, "quote": text}],
                    )
                    body = dict(version, claim_id=cid, payload_json=json.dumps(payload))
                    claims.append(tuple(head.values()))
                    versions.append(tuple(body.values()))
                    links.append(("claim", cid, 1, ref, 1, "supports", text, None))
            connection.executemany(insert, rows)
            connection.executemany("INSERT OR IGNORE INTO lexical_postings VALUES (?,?)", postings)
            if claims:
                connection.executemany(f"INSERT INTO claims VALUES ({','.join('?' for _ in claim)})", claims)
                connection.executemany(
                    f"INSERT INTO claim_versions VALUES ({','.join('?' for _ in version)})", versions
                )
                connection.executemany("INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?,?)", links)
            connection.commit()
        connection.execute("UPDATE work_items SET state='done'")
        connection.commit()
    return [source_ref, *refs]


def seed_vectors(lab: Lab, refs: list[str], output: Path) -> dict:
    """Stream float32 fixture batches, then reopen the production Windows helper.

    Fixture bulk loading avoids measuring repeated JSON pipe serialization as
    data generation. Measured reads and backfill writes still use the shipped
    process store and its fencing. No index or quantization is introduced.
    """
    import lancedb
    import pyarrow as pa

    rng = np.random.default_rng(42)
    space = digest(lab.ports.config["embedding"])
    started = time.perf_counter()
    lab.companion.close()
    database = lancedb.connect(str(lab.binding.data_directory.parent / "vectors" / "lancedb"))
    table = database.open_table("public_bench")
    partitions = {
        scope: physical_partition_scope_id(
            agent_id=lab.binding.agent_id,
            installation_id=lab.binding.installation_id,
            embedding_space=space,
            logical_scope_id=scope,
            project_id=None,
            branch_id=None,
        )
        for scope in SCOPES
    }

    def batches():
        for start in range(0, len(refs), 4096):
            end = min(len(refs), start + 4096)
            matrix = rng.standard_normal((end - start, 3072), dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
            metadata, scopes, ids = [], [], []
            for index in range(start, end):
                if index % 100 == 0 and index > start:
                    matrix[index - start] = matrix[index - start - 1]
                scope = "public" if index % 8 == 0 else f"public-{index % 8}"
                vector_id = f"TEST-vector-{index}"
                scopes.append(partitions[scope])
                ids.append(vector_id)
                metadata.append(
                    json.dumps(
                        {
                            "object_kind": "event",
                            "object_ref": refs[index],
                            "object_revision": 1,
                            "vector_id": vector_id,
                            "embedding_space": space,
                            "agent_id": lab.binding.agent_id,
                            "installation_id": lab.binding.installation_id,
                            "project_id": None,
                            "branch_id": None,
                            "logical_scope_id": scope,
                        }
                    )
                )
            columns = {
                "id": pa.array(ids),
                "scope_id": pa.array(scopes),
                "source": pa.array(refs[start:end]),
                "target": pa.array(metadata),
                "content": pa.array([""] * (end - start)),
                "summary": pa.array([""] * (end - start)),
                "updated_at": pa.array([NOW] * (end - start)),
                "vector": pa.FixedSizeListArray.from_arrays(pa.array(matrix.reshape(-1)), 3072),
            }
            yield pa.RecordBatch.from_arrays([columns[name] for name in table.schema.names], schema=table.schema)
            if start % 40960 == 0:
                print(f"vectors {start}/{len(refs)}", flush=True)

    table.add(pa.RecordBatchReader.from_batches(table.schema, batches()))
    if table.count_rows() != len(refs):
        raise RuntimeError("vector row count mismatch")
    lab.companion.open()
    return {"rows": len(refs), "seconds": time.perf_counter() - started}


def backlog(path: Path) -> dict:
    """Measure pending queue size and oldest synthetic available time."""
    with sqlite3.connect(path) as connection:
        count, oldest = connection.execute(
            "SELECT count(*),min(available_at) FROM work_items WHERE state IN ('pending','leased')"
        ).fetchone()
    age = (
        max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds())
        if oldest
        else None
    )
    return {"count": count, "oldest_available_at": oldest, "oldest_age_seconds": age}


def foreground(lab: Lab, *, count=60, pace=4) -> dict:
    """Four entries continuously capture and automatically recall at a frozen input rate."""
    captures, recalls, gaps = [], [], []
    started = time.perf_counter()
    for index in range(count):
        due = started + index / pace
        if due > time.perf_counter():
            time.sleep(due - time.perf_counter())
        at = datetime.now(UTC).isoformat()
        turn = Turn(
            f"TEST-live-{time.time_ns()}-{index}",
            f"TEST-entry-{index % 4}",
            "user",
            f"TESTtopic{index:05d} live preference is amber.",
            at,
        )
        tick = time.perf_counter()
        lab.capture(turn)
        captures.append((time.perf_counter() - tick) * 1000)
        case = Case(f"live-{index}", "synthetic", f"TESTtopic{index:05d} color", at, "", (), ())
        packet, ms = lab.recall(case)
        recalls.append(ms)
        gaps.append(packet.get("gaps", []))
    return {
        "capture": distribution(captures),
        "auto_recall": distribution(recalls),
        "gaps": gaps,
        "actual_seconds": time.perf_counter() - started,
        "backlog": backlog(lab.core.storage.path),
    }


def measure(output: Path, count: int) -> dict:
    """Build one scale and measure warm, first-read and concurrent-backfill behavior."""
    root = output / str(count)
    ports = Ports(RequestCache(output / f"cache-{count}.sqlite3"))
    lab = Lab(root, ports, now=NOW, backend="lancedb", scopes=SCOPES)
    try:
        lab.clock.realtime = True
        tick = time.perf_counter()
        refs = seed_truth(lab, count)
        truth_seconds = time.perf_counter() - tick
        vectors = seed_vectors(lab, refs, output)
        conditions = {"sources": count, "claims": count // 10, "vectors": len(refs)}
        first_case = Case("first", "synthetic", "TESTtopic00000 color", NOW, "", (), ())
        lab.context = replace(lab.context, allowed_scope_ids=frozenset({"public"}))
        first_packet, first_ms = lab.recall(first_case)
        warm = foreground(lab)
        with sqlite3.connect(lab.core.storage.path) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO work_items(work_type,subject_ref,subject_revision,scope_id,state,available_at) "
                "VALUES ('embed',?,1,'public','pending',?)",
                [(ref, datetime.now(UTC).isoformat()) for index, ref in enumerate(refs) if index % 8 == 0][:200],
            )
            connection.commit()
        before = backlog(lab.core.storage.path)
        began = time.perf_counter()

        def backfill():
            return lab.core.drain_worker(
                lab.context,
                owner_id="TEST-backfill",
                consolidation=None,
                embed=lab.embed,
                purge=lab.purge,
                max_items=200,
                remaining_seconds=120,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(backfill)
            concurrent = foreground(lab)
            receipt = worker.result(timeout=180)
        after = backlog(lab.core.storage.path)
        sizes = {str(path.relative_to(root)): path.stat().st_size for path in root.rglob("*") if path.is_file()}
        return {
            "conditions": conditions,
            "truth_seed_seconds": truth_seconds,
            "vectors": vectors,
            "first_recall_ms": first_ms,
            "first_gaps": first_packet.get("gaps"),
            "warm": warm,
            "concurrent_backfill": concurrent,
            "backlog_before": before,
            "backlog_after": after,
            "backfill_processed": receipt.processed,
            "backfill_wall_seconds": time.perf_counter() - began,
            "file_bytes": sizes,
            "total_bytes": sum(sizes.values()),
            "query_embedding_network_ms": None,
        }
    finally:
        lab.close()


def main():
    """Freeze conditions, then run the two explicitly requested data sizes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=int, choices=[1000, 350000, 1000000], required=True)
    parser.add_argument("--channels-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    freeze(args.output)
    result = measure_channels(args.output, args.scale) if args.channels_only else measure(args.output, args.scale)
    prefix = "channels" if args.channels_only else "results"
    (args.output / f"{prefix}-{args.scale}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "file_bytes"}))


def measure_channels(output: Path, count: int) -> dict:
    """Measure lexical and vector retrieval independently on the existing task fixture."""
    ports = Ports(RequestCache(output / f"cache-{count}.sqlite3"))
    started = time.perf_counter()
    store = Lab(output / str(count), ports, now=NOW, backend="lancedb", scopes=SCOPES, existing=True)
    opened = (time.perf_counter() - started) * 1000
    store.clock.realtime = True
    context = replace(store.context, allowed_scope_ids=frozenset({"public"}))
    vector = np.random.default_rng(42).standard_normal((1, 3072), dtype=np.float32)[0]
    vector /= np.linalg.norm(vector)
    partition = physical_partition_scope_id(
        agent_id=store.binding.agent_id,
        installation_id=store.binding.installation_id,
        embedding_space=digest(ports.config["embedding"]),
        logical_scope_id="public",
        project_id=None,
        branch_id=None,
    )
    lexical, semantic, vector_counts = [], [], []
    try:
        for index in range(20):
            tick = time.perf_counter()
            store.core.search_sources(context, f"TESTtopic{index:05d} color", limit=10)
            lexical.append((time.perf_counter() - tick) * 1000)
            tick = time.perf_counter()
            hits = store.companion.search_scopes(vector.tolist(), scope_ids=[partition], limit=10)
            semantic.append((time.perf_counter() - tick) * 1000)
            vector_counts.append(len(hits))
        return {
            "scale": count,
            "reopen_ms": opened,
            "lexical": distribution(lexical),
            "vector": distribution(semantic),
            "vector_hit_counts": vector_counts,
            "query_embedding_network_ms": None,
            "cache": "reopened helper then warm; OS page cache uncontrolled",
        }
    finally:
        store.close()


if __name__ == "__main__":
    main()
