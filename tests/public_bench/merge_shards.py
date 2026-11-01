"""Join the reports of one split run as N shards (``run.py --shard K/N``) into the report one process would write.

Usage: python merge_shards.py --data <data root> --output <dir> <shard dir> ...

Every shard must come from the same split, method, models, prompts and settle limit, and the shards must be exactly
0/N to N-1/N. Rows return to the split's question order, scoring is recomputed from them, cache counts are summed,
``workers`` is the shards' total and ``total_seconds`` the slowest shard's. The merged report is written to
``<dir>/results.json``; like run.py, the command exits 1 when any question is incomplete.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from run import wilson
from scoring import summarize

#: Report fields that describe the run's conditions; shards that differ in any of them cannot be one run.
SAME = (
    "schema",
    "mode",
    "dataset",
    "dataset_manifest",
    "split",
    "split_manifest_sha256",
    "method",
    "retrieval_only",
    "models",
    "official_judge_prompts_sha256",
    "corpus_categories",
    "settle_seconds",
    "start_interval_seconds",
    "quality_claim",
)


def merge(reports: list[dict], order: list[str]) -> dict:
    """The single report for ``reports``, rows ordered by ``order`` (the split's question ids)."""
    first = reports[0]
    for key in SAME:
        if any(report.get(key) != first.get(key) for report in reports):
            raise ValueError(f"shards_differ:{key}")
    count = len(reports)
    if sorted(report.get("shard") or "" for report in reports) != sorted(f"{k}/{count}" for k in range(count)):
        raise ValueError("shards_incomplete")
    position = {qid: index for index, qid in enumerate(order)}
    results = sorted((row for report in reports for row in report["results"]), key=lambda row: position[row["id"]])
    incomplete = sorted(
        (row for report in reports for row in report["incomplete_results"]), key=lambda row: position[row["id"]]
    )
    scores = summarize(results, incomplete, fake=first["mode"] == "fake-smoke", retrieval_only=first["retrieval_only"])
    operations: dict[str, dict[str, int]] = {}
    for report in reports:
        for name, counts in report["cache"]["operations"].items():
            merged = operations.setdefault(name, {"hits": 0, "misses": 0})
            merged["hits"] += counts["hits"]
            merged["misses"] += counts["misses"]
    selected = Counter()
    for report in reports:
        selected.update(report["selected_categories"])
    return {
        **{key: first[key] for key in SAME[:7]},
        "shard": None,
        "shards": count,
        **{key: first[key] for key in SAME[7:11]},
        "selected_categories": dict(selected),
        "questions": sum(report["questions"] for report in reports),
        "completed_questions": len(results),
        "pipeline_states": dict(Counter((row.get("backend_state") or {}).get("status") for row in results)),
        "results": results,
        "incomplete_results": incomplete,
        "workers": sum(report["workers"] for report in reports),
        "settle_seconds": first["settle_seconds"],
        "start_interval_seconds": first["start_interval_seconds"],
        "total_seconds": max(report["total_seconds"] for report in reports),
        "cache": {
            "hits": sum(report["cache"]["hits"] for report in reports),
            "misses": sum(report["cache"]["misses"] for report in reports),
            "operations": operations,
        },
        "scoring": scores,
        "accuracy": scores["accuracy"],
        "accuracy_ci95": wilson(round(scores["accuracy"] * len(results)), len(results))
        if scores["accuracy"] is not None
        else None,
        "quality_claim": first["quality_claim"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("shards", type=Path, nargs="+")
    args = parser.parse_args()
    reports = [json.loads((shard / "results.json").read_text(encoding="utf-8")) for shard in args.shards]
    splits = json.loads((args.data / "longmemeval-splits.json").read_text(encoding="utf-8"))
    report = merge(reports, splits[reports[0]["split"]])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"results", "dataset_manifest"}}))
    if report["incomplete_results"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
