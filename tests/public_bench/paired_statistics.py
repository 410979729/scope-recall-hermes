"""Predeclared paired comparisons for memory benchmarks; no model calls."""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path


def paired_binary(before: dict[str, bool], after: dict[str, bool]) -> dict:
    """Exact two-sided McNemar test plus a seeded paired bootstrap interval.

    Runs must contain identical question ids. The unit is a question, never an
    individual retrieved item or a turn within that question. The reported
    confidence interval estimates the change in the fraction correct.
    """
    if before.keys() != after.keys() or not before:
        raise ValueError("paired runs need the same nonempty question set")
    ids = sorted(before)
    differences = [int(after[qid]) - int(before[qid]) for qid in ids]
    wins, losses = differences.count(1), differences.count(-1)
    discordant = wins + losses
    p_value = min(1.0, 2 * sum(math.comb(discordant, i) for i in range(min(wins, losses) + 1)) / 2**discordant)
    rng = random.Random(42)
    samples = sorted(sum(rng.choices(differences, k=len(ids))) / len(ids) for _ in range(10000))
    return {
        "n": len(ids),
        "wins": wins,
        "losses": losses,
        "delta": sum(differences) / len(ids),
        "p_value": p_value,
        "delta_ci95": [samples[249], samples[9749]],
        "bootstrap_seed": 42,
    }


def holm(p_values: dict[str, float], *, alpha=0.05) -> dict[str, bool]:
    """Control familywise error across all predeclared experiment/category comparisons."""
    ordered = sorted(p_values, key=p_values.get)
    decisions, stopped = {}, False
    for index, name in enumerate(ordered):
        rejected = not stopped and p_values[name] <= alpha / (len(ordered) - index)
        decisions[name], stopped = rejected, stopped or not rejected
    return decisions


def compare_reports(before: Path, after: Path, *, metric="judge_correct") -> dict:
    """Pair complete result files by question id and compare one binary outcome.

    Retrieval comparisons exclude only evidence labels absent on both sides.
    Continuous answer metrics and incomplete quality runs are rejected.
    """
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in (before, after)]
    for report in reports:
        rows = report["results"]
        if report.get("incomplete_results") or any(
            row.get("status", "complete") != "complete"
            or row.get("backend_state", {}).get("status", "complete") not in {"complete", "not_required"}
            for row in rows
        ):
            raise ValueError("incomplete_questions_cannot_be_compared")
        if report.get("questions", len(rows)) != len(rows):
            raise ValueError("question_coverage_is_incomplete")
        if metric == "judge_correct" and (
            report.get("mode") != "live" or report.get("scoring", {}).get("status", "complete") != "complete"
        ):
            raise ValueError("complete_live_binary_scores_required")
    for key in ("dataset", "dataset_manifest", "models", "split_manifest_sha256", "official_judge_prompts_sha256"):
        if reports[0].get(key) != reports[1].get(key):
            raise ValueError(f"comparison_conditions_differ:{key}")
    indexed = [{row["id"]: row for row in report["results"]} for report in reports]
    if any(len(rows) != len(report["results"]) for rows, report in zip(indexed, reports, strict=True)):
        raise ValueError("duplicate_question_ids")
    if indexed[0].keys() != indexed[1].keys():
        raise ValueError("paired results need identical question ids")
    values, groups, excluded = [{}, {}], {}, []
    for qid, first in indexed[0].items():
        second = indexed[1][qid]
        if first["category"] != second["category"]:
            raise ValueError(f"question_category_differs:{qid}")
        pair = [
            row.get(metric) if metric == "judge_correct" else row.get("retrieval", {}).get(metric)
            for row in (first, second)
        ]
        if metric != "judge_correct" and pair == [None, None]:
            excluded.append(qid)
            continue
        if any(type(value) is not bool for value in pair):
            raise ValueError(f"binary_metric_unavailable:{qid}:{metric}")
        for side in (0, 1):
            values[side][qid] = pair[side]
        groups.setdefault(first["category"], []).append(qid)
    overall = paired_binary(*values)
    categories = {
        category: paired_binary({qid: values[0][qid] for qid in ids}, {qid: values[1][qid] for qid in ids})
        for category, ids in groups.items()
    }
    decisions = holm(
        {"overall": overall["p_value"], **{name: result["p_value"] for name, result in categories.items()}}
    )
    return {
        "metric": metric,
        "overall": overall,
        "categories": categories,
        "holm_rejected": decisions,
        "unlabeled_question_ids": excluded,
    }


def main(argv=None) -> None:
    """Read two result files and emit an offline paired comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--metric", choices=["judge_correct", "session_recall_all", "turn_recall_all"], default="judge_correct"
    )
    args = parser.parse_args(argv)
    result = compare_reports(args.before, args.after, metric=args.metric)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
