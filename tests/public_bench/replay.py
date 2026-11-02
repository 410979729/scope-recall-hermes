"""Replay retrieval from copied public-benchmark stores and cached query vectors.

Only development and regression runs are accepted. No model adapters are built,
and a missing query vector fails the run rather than invoking an endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

from admission_variants import SCHEMES, ReplayPolicy, SnapshotWeights
from cache import CONTRACT, digest
from cases import longmemeval
from lab import Lab
from run import evidence_metrics
from scope_recall.core.storage import _directory
from scope_recall.vector.imports import visible_in_truth


class CachedQueries:
    """Read query embeddings from a closed cache without creating a writer or a model port."""

    def __init__(self, path: Path, *, model: dict | None = None) -> None:
        self.queries = {}
        self.misses = 0
        self.model = model
        self.uri = path.resolve().as_uri() + "?mode=ro"
        if model is not None:
            return
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            for request, response in connection.execute(
                "SELECT request,response FROM responses WHERE json_extract(request,'$.operation')='query_embedding'"
            ):
                envelope = json.loads(request)
                if envelope.get("operation") != "query_embedding":
                    continue
                text, vector = envelope["request"]["text"], tuple(json.loads(response))
                if text in self.queries and self.queries[text] != vector:
                    raise ValueError("ambiguous_cached_query_vector")
                self.queries[text] = vector

    def embed_query(self, text: str, remaining_seconds=1.0):
        """Return an exact cached vector, recording misses even if a caller catches them."""
        if text not in self.queries and self.model is not None:
            envelope = {
                "operation": "query_embedding",
                "request": {"text": text, "encoding": "query"},
                "model": self.model,
                "contract": CONTRACT,
            }
            with closing(sqlite3.connect(self.uri, uri=True)) as connection:
                row = connection.execute("SELECT response FROM responses WHERE key=?", (digest(envelope),)).fetchone()
            if row is not None:
                self.queries[text] = tuple(json.loads(row[0]))
        if text not in self.queries:
            self.misses += 1
            raise ValueError("query_embedding_cache_miss")
        return self.queries[text]

    def embed_source(self, source, remaining_seconds=1.0):
        """Refuse document embedding even when a store wires its capture port."""
        raise ValueError("source_embedding_disabled")

    def embed_queries(self, texts, remaining_seconds=1.0):
        """Return only cached query vectors in caller order."""
        return tuple(self.embed_query(text, remaining_seconds) for text in texts)

    def raise_if_missing(self) -> None:
        """Prevent a degraded recall from concealing a cache miss."""
        if self.misses:
            raise ValueError("query_embedding_cache_miss")


def shifted_pairs(ids: list[str], offsets: tuple[int, ...]) -> list[tuple[str, str]]:
    """Deterministic ordered mismatches within a shard; reject self-pairs and repeated offsets."""
    if len(ids) < 2:
        raise ValueError("a_negative_shard_needs_two_questions")
    shifts = sorted({offset % len(ids) for offset in offsets} - {0})
    return [(qid, ids[(index + offset) % len(ids)]) for index, qid in enumerate(ids) for offset in shifts]


def index_stores(root: Path) -> dict[str, list[Path]]:
    """Index closed fixture locations once, retaining duplicate identities as errors."""
    result = defaultdict(list)
    for path in root.glob("shard-*/stores-*/*"):
        if path.is_dir():
            result[path.name].append(path)
    return dict(result)


def store_for(root: Path, question_id: str, *, index: dict[str, list[Path]] | None = None) -> Path:
    """Resolve an exact question digest without mistaking an unreadable directory for an empty store."""
    name = digest(question_id)[:20]
    matches = sorted(index.get(name, ())) if index is not None else sorted(root.glob(f"shard-*/stores-*/{name}"))
    if len(matches) != 1:
        raise ValueError(f"question_store_count:{question_id}:{len(matches)}")
    # Opening the known file surfaces permission errors that glob would otherwise suppress.
    with (matches[0] / "truth/memory.sqlite3").open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            raise ValueError("question_store_not_sqlite")
    return matches[0]


def copy_store(source: Path, destination: Path) -> dict:
    """Copy one fixture without preserving its ACL; record every input file's digest."""
    if destination.exists():
        raise FileExistsError(destination)
    fingerprints = {}
    for folder in ("truth", "vectors"):
        origin = source / folder
        # scandir raises on an inaccessible input; Path.rglob may silently omit it.
        with os.scandir(origin) as entries:
            if not list(entries):
                raise ValueError(f"empty_fixture_directory:{folder}")
        shutil.copytree(origin, destination / folder, copy_function=shutil.copyfile)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                fingerprints[str(path.relative_to(destination))] = hashlib.file_digest(stream, "sha256").hexdigest()
    # Local fixtures pin their absolute truth path. Relocate only the copy's
    # directory identity, keeping the sources, scopes and queue state intact.
    with closing(sqlite3.connect(destination / "truth/memory.sqlite3")) as connection:
        connection.execute(
            "UPDATE instance_meta SET data_directory=? WHERE singleton=1", (_directory(destination / "truth"),)
        )
        connection.commit()
    return fingerprints


def prepare_vectors(lab: Lab) -> dict:
    """Import existing vectors and publish the current cache, with no embedding work."""
    with closing(sqlite3.connect(lab.core.storage.path)) as truth:
        truth.row_factory = sqlite3.Row
        for _ in range(10000):
            receipt = lab.companion.import_legacy_page(visible=lambda row: visible_in_truth(truth, row))
            if receipt["outcome"] == "complete":
                break
            if receipt["outcome"] not in {"copy", "verify", "catchup"}:
                raise RuntimeError(f"vector_fixture_import:{receipt}")
        else:
            raise RuntimeError("vector_fixture_import_did_not_finish")
    for _ in range(10000):
        # Fixture preparation publishes pages directly; a copied scheduler
        # cooldown does not describe this offline process's available time.
        receipt = lab.companion.maintain_index(max_rows=512)
        if not receipt or receipt["outcome"] != "indexed":
            raise RuntimeError(f"vector_fixture_cache:{receipt}")
        if receipt["rows"] < 512:
            return lab.companion.diagnostics()
    raise RuntimeError("vector_fixture_cache_did_not_finish")


def source_metadata(lab: Lab) -> dict:
    """Load corpus identities and roles from the copied truth, never from answer text."""
    with closing(sqlite3.connect(lab.core.storage.path)) as connection:
        rows = connection.execute("SELECT event_id,source_event_key,session_id,role FROM source_events").fetchall()
    metadata = {ref: (key, session, role) for ref, key, session, role in rows}
    lab.keys.update({ref: row[0] for ref, row in metadata.items()})
    lab.sessions.update({ref: row[1] for ref, row in metadata.items()})
    return metadata


def measure(lab: Lab, case, metadata: dict) -> dict:
    """Measure the automatic packet and count the selected items' representative channels."""
    pipeline, channels = lab.core.recall_pipeline, {}
    original = pipeline.search

    def observe(context):
        result = original(context)
        channels.update({item.key: item.source for item in result.candidates})
        return result

    pipeline.search = observe
    try:
        packet, latency = lab.recall(case)
    finally:
        pipeline.search = original
    turns, sessions = lab.retrieved_sources(packet)
    events = [metadata[item["ref"]] for item in packet["items"] if item["kind"] == "event"]
    counts = Counter(
        channels.get(
            ("claim" if item["kind"] == "procedure" else item["kind"], item["ref"], item["revision"]), "unknown"
        )
        for item in packet["items"]
    )
    return {
        "id": case.id,
        "category": case.category,
        "retrieval": evidence_metrics(case, turns, sessions),
        "evidence_present": bool(set(case.sessions) & sessions),
        "items": len(packet["items"]),
        "channels": dict(counts),
        "event_items": len(events),
        "assistant_items": sum(row[2] == "assistant" for row in events),
        "off_gold_items": sum(row[1] not in case.sessions for row in events),
        "injected": bool(packet["items"]),
        "recall_ms": latency,
        "gaps": packet["gaps"],
        "selected": [
            {"ref": item["ref"], "revision": item["revision"], "kind": item["kind"]} for item in packet["items"]
        ],
    }


def summarize(rows: list[dict]) -> dict:
    """Aggregate rates with explicit denominators, separating abstention from answerable recall."""
    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
        groups["all"].append(row)
    result = {}
    for category, items in sorted(groups.items()):
        answerable = [row for row in items if row["category"] != "abstention"]
        stats = {"questions": len(items), "injection_rate": mean(row["injected"] for row in items)}
        for metric in ("session_recall", "turn_recall", "session_recall_all", "turn_recall_all"):
            values = [row["retrieval"][metric] for row in answerable if row["retrieval"][metric] is not None]
            stats[metric] = mean(values) if values else None
        stats["evidence_present"] = mean(row["evidence_present"] for row in answerable) if answerable else None
        for field in ("items", "event_items", "assistant_items", "off_gold_items"):
            stats[field] = sum(row[field] for row in items)
        stats["assistant_fraction"] = stats["assistant_items"] / stats["event_items"] if stats["event_items"] else 0.0
        stats["off_gold_fraction"] = stats["off_gold_items"] / stats["event_items"] if stats["event_items"] else 0.0
        stats["channels"] = dict(sum((Counter(row["channels"]) for row in items), Counter()))
        result[category] = stats
    return result


def replay(args) -> dict:
    """Replay all selected questions and same-shard mismatches on fresh fixture copies."""
    report = json.loads((args.run / "results.json").read_text(encoding="utf-8"))
    if report["split"] not in {"dev", "regression"} or report.get("incomplete_results"):
        raise ValueError("only_complete_development_or_regression_runs")
    cases = {case.id: case for case in longmemeval(args.data, ids={row["id"] for row in report["results"]})}
    if len(cases) != len(report["results"]):
        raise ValueError("question_manifest_mismatch")
    cache = CachedQueries(args.run / "cache.sqlite3", model=report["models"]["embedding"])
    for case in cases.values():
        cache.embed_query(case.question)
    locations = index_stores(args.stores_root or args.run)
    roots = {qid: store_for(args.stores_root or args.run, qid, index=locations) for qid in cases}
    shards = defaultdict(list)
    for qid, root in roots.items():
        shards[root.parent.parent.name].append(qid)
    pairs = [pair for ids in shards.values() for pair in shifted_pairs(sorted(ids), tuple(args.offsets))]
    if report["split"] == "dev" and len(pairs) < 200:
        raise ValueError("development_requires_at_least_200_mismatched_pairs")
    result = {
        "schema": "recall-replay/1",
        "split": report["split"],
        "lexical_coverage": args.coverage,
        "thresholds": args.thresholds,
        "negative_pairs": len(pairs),
        "fixtures": {},
        "rows": [],
    }
    ports = SimpleNamespace(config={"embedding": report["models"]["embedding"]}, embedder=cache, cache=cache)
    for index, (qid, source) in enumerate(roots.items()):
        destination = args.scratch / digest(qid)[:20]
        result["fixtures"][qid] = {"source": str(source), "sha256": copy_store(source, destination)}
        lab = Lab(destination, ports, now=cases[qid].at, existing=True)
        try:
            result["fixtures"][qid]["vectors"] = prepare_vectors(lab)
            metadata = source_metadata(lab)
            original_policy = lab.core.recall_pipeline.policy
            queries = [(cases[qid], False)] + [(cases[a], True) for a, b in pairs if b == qid]
            with SnapshotWeights(lab.core.storage.path) as statistics:
                for threshold in args.thresholds:
                    policy = (
                        replace(original_policy, vector_threshold=threshold)
                        if args.coverage == "product"
                        else ReplayPolicy.from_policy(original_policy, args.coverage, statistics, threshold)
                    )
                    lab.core.recall_pipeline.policy = policy
                    for case, negative in queries:
                        row = measure(lab, case, metadata)
                        row.update(store_id=qid, negative=negative, vector_threshold=policy.vector_threshold)
                        result["rows"].append(row)
        finally:
            lab.close()
        print(json.dumps({"done": index + 1, "questions": len(roots), "split": report["split"]}), flush=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    result["curve"] = []
    for threshold in args.thresholds:
        rows = [row for row in result["rows"] if row["vector_threshold"] == threshold]
        positive, negative = [row for row in rows if not row["negative"]], [row for row in rows if row["negative"]]
        result["curve"].append(
            {"vector_threshold": threshold, "positive": summarize(positive), "negative": summarize(negative)}
        )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    """Require explicit local inputs and refuse existing scratch directories."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--stores-root", type=Path)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thresholds", type=float, nargs="+", required=True)
    parser.add_argument("--offsets", type=int, nargs="+", default=[1, 2, 3, 4])
    parser.add_argument("--coverage", choices=["product", *SCHEMES], default="product")
    args = parser.parse_args()
    if args.scratch.exists() or args.output.exists():
        parser.error("scratch and output must be new task-owned paths")
    args.scratch.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = replay(args)
    print(json.dumps(result["curve"]))


if __name__ == "__main__":
    main()
