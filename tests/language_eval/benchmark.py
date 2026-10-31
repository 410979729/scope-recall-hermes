"""Compare a fixed bilingual synthetic benchmark through the public benchmark pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/public_bench"))

from cache import RequestCache  # noqa: E402
from cases import Case, Turn  # noqa: E402
from paired_statistics import compare_reports  # noqa: E402
from ports import Ports  # noqa: E402
from run import isolated, run_case  # noqa: E402

JUDGE = (
    "Judge whether the response correctly answers the question using the reference. "
    "For an unanswerable reference the response must refuse to invent an answer. "
    "Return only yes or no. Question: {} Reference: {} Response: {}"
)


class EvaluationPorts(Ports):
    """Keep the benchmark judge instruction in the leading system message."""

    def complete(self, messages, *, operation):
        """Forward answers and attach the reference judge's leading system instruction."""
        if operation == "judge":
            messages = [
                {"role": "system", "content": "Evaluate the response against the supplied reference."},
                *messages,
            ]
        return super().complete(messages, operation=operation)


def accuracy(rows):
    """Count every fixed question, including incomplete cases, in its category denominator."""
    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row.get("judge_correct") is True)
    return {name: sum(values) / len(values) for name, values in groups.items()}


def main():
    """Require explicit live routes; fake mode proves only store, recall and result plumbing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--approved-endpoint", action="append", default=[])
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.fake and args.config:
        parser.error("fake mode accepts no live route")
    if not args.fake and (not args.config or not args.allow_model_calls):
        parser.error("live mode requires --config and --allow-model-calls")
    args.output.mkdir(parents=True, exist_ok=False)
    corpus = ROOT / "fixtures/language/benchmark_cases.json"
    cases = [
        Case(**{**row, "sessions": tuple(row["sessions"]), "turns": tuple(Turn(**t) for t in row["turns"])})
        for row in json.loads(corpus.read_text(encoding="utf-8"))
    ]
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    cache = RequestCache(args.output / "requests.sqlite3")
    ports = EvaluationPorts(cache, config, approved=args.approved_endpoint, ledger_root=args.output)
    rows = [
        isolated(
            lambda one: run_case(
                one,
                root=args.output / "stores",
                ports=ports,
                prompts={"default": JUDGE},
                method="scope-recall",
                retrieval_only=False,
                settle_seconds=180,
            ),
            case,
            cache_only=False,
        )
        for case in cases
    ]
    report = dict(
        schema="language-benchmark/1",
        mode="fake" if args.fake else "live",
        corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
        model_config_sha256=hashlib.sha256(json.dumps(ports.config, sort_keys=True).encode()).hexdigest(),
        results=rows,
        category_accuracy=accuracy(rows),
        quality_claim="workflow only" if args.fake else "fixed synthetic corpus only",
    )
    result_path = args.output / "results.json"
    result_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    if args.baseline:
        before = json.loads(args.baseline.read_text(encoding="utf-8"))
        for field in ("corpus_sha256", "model_config_sha256", "mode"):
            if before[field] != report[field]:
                raise ValueError("baseline differs: " + field)
        if not args.fake:
            report["paired"] = compare_reports(args.baseline, result_path)
        report["not_worse"] = all(
            report["category_accuracy"][name] >= value for name, value in before["category_accuracy"].items()
        )
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"mode": report["mode"], "cases": len(rows), "complete": sum(r["status"] == "complete" for r in rows)}
        )
    )
    return int(any(r["status"] != "complete" for r in rows) or report.get("not_worse") is False)


if __name__ == "__main__":
    raise SystemExit(main())
