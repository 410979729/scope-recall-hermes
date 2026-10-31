"""Run pinned local public datasets through isolated stores and explicit ports.

Run with --fake for a plumbing smoke. Live requests require --allow-model-calls,
an explicit route file and exact --approved-endpoint arguments. Evaluation does
not download, discover host settings, invoke host programs or use a CLI model.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cache import RequestCache, digest
from cases import category_counts, factconsolidation, fixed_splits, locomo, longmemeval
from dataset_files import verify_inputs
from lab import Lab
from ports import Ports
from scoring import BINARY_METRICS, score_local, summarize

ANSWER_SYSTEM = "Answer using only the supplied history. If the history does not answer the question, say so."


def official_prompts(path: Path) -> dict[str, str]:
    """Read only literal prompt strings from upstream source text; never execute it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_anscheck_prompt"
    )
    templates = [
        node.value.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "template" for target in node.targets)
    ]
    if len(templates) != 5 or any(not isinstance(template, str) for template in templates):
        raise ValueError("upstream judge contract changed")
    selected = {}
    for template in templates:
        if "unanswerable question" in template:
            selected["abstention"] = template
        elif "off-by-one" in template:
            selected["temporal-reasoning"] = template
        elif "previous information" in template:
            selected["knowledge-update"] = template
        elif "rubric for desired" in template:
            selected["single-session-preference"] = template
        else:
            selected["default"] = template
    return selected


def wilson(successes: int, total: int) -> list[float] | None:
    """Return a two-sided 95% Wilson interval for a binary question score."""
    if not total:
        return None
    z, proportion = 1.959963984540054, successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [centre - radius, centre + radius]


def score_retrieval(case, lab: Lab, packet: dict) -> dict:
    """Report session and turn evidence coverage; unlabeled gold stays null."""
    turns, sessions = lab.retrieved_sources(packet)
    return evidence_metrics(case, turns, sessions)


def evidence_metrics(case, turns: set, sessions: set) -> dict:
    """Score the evidence actually selected by the measured retrieval method."""
    gold_turns = {turn.key for turn in case.turns if turn.answer}
    gold_sessions = set(case.sessions)
    return {
        "session_recall": len(gold_sessions & sessions) / len(gold_sessions) if gold_sessions else None,
        "session_recall_all": gold_sessions <= sessions if gold_sessions else None,
        "turn_recall": len(gold_turns & turns) / len(gold_turns) if gold_turns else None,
        "turn_recall_all": gold_turns <= turns if gold_turns else None,
    }


def alternative_context(case, ports: Ports, method: str) -> tuple[str, dict, float]:
    """Build full-history or raw-vector context without paying for consolidation."""
    started = time.perf_counter()
    selected = case.turns
    if method == "naive-rag":
        query = ports.evaluator_embedder.embed_query(case.question, remaining_seconds=30)
        vectors = ports.evaluator_embedder.embed_texts([turn.content for turn in case.turns], remaining_seconds=30)
        ranked = []
        for turn, vector in zip(case.turns, vectors, strict=True):
            ranked.append((sum(a * b for a, b in zip(query, vector, strict=True)), turn))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        selected = tuple(turn for _, turn in ranked[:6])
    context = json.dumps(
        [{"role": turn.role, "content": turn.content, "at": turn.at, "session": turn.session} for turn in selected],
        ensure_ascii=False,
    )
    metrics = evidence_metrics(case, {turn.key for turn in selected}, {turn.session for turn in selected})
    return context, metrics, (time.perf_counter() - started) * 1000


def judge_label(text: str) -> bool:
    """LongMemEval's own rule: a judge reply counts as correct when it contains "yes"."""
    return "yes" in text.lower()


def isolated(run, case, *, cache_only: bool) -> dict:
    """One question's failure is recorded and the split continues; a cache-only run still stops on any error."""
    started = time.perf_counter()
    try:
        return run(case)
    except Exception as exc:
        if cache_only:
            raise
        return {
            "id": case.id,
            "category": case.category,
            "status": "failed",
            "turns": len(case.turns),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            # Code locations only, innermost last: a ContractError message names a code, not where it was raised.
            "error_at": [
                f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)[-4:]
            ],
            "answer": None,
            "judge_correct": None,
            "evaluation": {"status": "not_run", "reason": "question_failed"},
            "total_seconds": time.perf_counter() - started,
        }


def run_case(
    case,
    *,
    root: Path,
    ports: Ports,
    prompts: dict,
    method: str,
    retrieval_only: bool,
    dataset="longmemeval",
    settle_seconds=900,
) -> dict:
    """Capture, settle, recall, answer and judge a question without leaking gold."""
    started = time.perf_counter()
    lab = Lab(root / digest(case.id)[:20], ports, now=case.turns[0].at) if method == "scope-recall" else None
    try:
        admissions = []
        packet, state = None, {"status": "not_required"}
        if lab is not None:
            for turn in case.turns:
                receipt = lab.capture(turn)
                if receipt.durability != "persisted":
                    admissions.append({"turn": turn.key, "error": receipt.error_code, "gaps": list(receipt.gaps)})
            state = lab.settle(wall_seconds=settle_seconds)
            ports.cache.raise_if_missing()
            # Work the product gave up on is its own outcome, and its owner is still answered from what the store
            # holds. Pending work, a limit reached or an unavailable capability is not an outcome yet.
            if state["status"] not in {"complete", "failed"}:
                return {
                    "id": case.id,
                    "category": case.category,
                    "status": "incomplete",
                    "turns": len(case.turns),
                    "admission_failures": admissions,
                    "backend_state": state,
                    "answer": None,
                    "judge_correct": None,
                    "evaluation": {"status": "not_run", "reason": "pipeline_incomplete"},
                    "total_seconds": time.perf_counter() - started,
                }
            packet, latency = lab.recall(case)
            context = json.dumps(packet, ensure_ascii=False)
            retrieval = score_retrieval(case, lab, packet)
        else:
            context, retrieval, latency = alternative_context(case, ports, method)
        ports.cache.raise_if_missing()
        answer_messages = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": f"History:\n{context}\nQuestion date: {case.at}\nQuestion: {case.question}"},
        ]
        answer = verdict = None
        evaluation = {"status": "not_run", "reason": "retrieval_only"}
        if not retrieval_only:
            answer = ports.complete(answer_messages, operation="answer")
            if dataset == "longmemeval":
                template = prompts.get(case.category, prompts["default"])
                judge_prompt = template.format(case.question, case.gold, answer)
                judge = ports.complete([{"role": "user", "content": judge_prompt}], operation="judge")
                verdict = judge_label(judge)
                evaluation = {"status": "scored", "metric": "judge_accuracy", "score": float(verdict)}
            else:
                evaluation = score_local(dataset, case.category, case.gold, answer)
                if evaluation["status"] == "scored" and evaluation["metric"] in BINARY_METRICS:
                    verdict = bool(evaluation["score"])
        ports.cache.raise_if_missing()
        return {
            "id": case.id,
            "category": case.category,
            "status": "complete",
            "turns": len(case.turns),
            "admission_failures": admissions,
            "backend_state": state,
            "retrieval": retrieval,
            "packet": packet,
            "answer": answer,
            "judge_correct": verdict,
            "evaluation": evaluation,
            "answer_prompt_sha256": digest(answer_messages),
            "injected_token_units": len(context.encode()) // 4 + 1,
            "token_measure": "utf8_bytes_div4_estimate",
            "recall_ms": latency,
            "total_seconds": time.perf_counter() - started,
            "query_embedding_network_ms": None if ports.fake else "included_in_recall; separate probe required",
        }
    finally:
        if lab is not None:
            lab.close()


def parse_args() -> argparse.Namespace:
    """Read explicit routes, cache mode and bounded question-worker settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, help="reuse request cache across separate result directories")
    parser.add_argument("--dataset", choices=["longmemeval", "locomo", "factconsolidation"], default="longmemeval")
    parser.add_argument("--split", choices=["dev", "regression", "sealed"], default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--settle-seconds", type=float, default=900)
    parser.add_argument("--start-interval-seconds", type=float, default=0)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--approved-endpoint", action="append", default=[])
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--method", choices=["scope-recall", "full-context", "naive-rag"], default="scope-recall")
    args = parser.parse_args()
    if args.fake == bool(args.config) or (not args.fake and not (args.allow_model_calls or args.cache_only)):
        parser.error("choose --fake, or explicit --config with --allow-model-calls")
    if args.cache_only and not args.retrieval_only:
        parser.error("cache-only commit runs must be retrieval-only")
    if not 1 <= args.workers <= 64:
        parser.error("workers must be between 1 and 64")
    if not math.isfinite(args.settle_seconds) or args.settle_seconds <= 0:
        parser.error("settle-seconds must be finite and positive")
    if not math.isfinite(args.start_interval_seconds) or args.start_interval_seconds < 0:
        parser.error("start-interval-seconds must be finite and not negative")
    return args


def main() -> None:
    """Execute a fixed split, with four independent question stores by default."""
    args = parse_args()
    loaders = {
        "longmemeval": (longmemeval, "longmemeval_s_cleaned.json"),
        "locomo": (locomo, "locomo10.json"),
        "factconsolidation": (factconsolidation, "Conflict_Resolution-00000-of-00001.parquet"),
    }
    manifest = verify_inputs(args.data, args.dataset)
    loader, filename = loaders[args.dataset]
    cases = loader(args.data / filename)
    if args.dataset == "longmemeval":
        splits = fixed_splits(cases)
        split_file = args.data / "longmemeval-splits.json"
        text = json.dumps(splits, indent=2) + "\n"
        if split_file.exists() and split_file.read_text(encoding="utf-8") != text:
            raise ValueError("frozen split differs")
        split_file.write_text(text, encoding="utf-8")
        by_id = {case.id: case for case in cases}
        selected = [by_id[qid] for qid in splits[args.split]]
    else:
        # Local smoke covers every category rather than taking only the first
        # conversation or the first FactConsolidation hop class.
        selected = [next(case for case in cases if case.category == category) for category in category_counts(cases)]
    if args.limit:
        selected = selected[: args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    cache = RequestCache(args.cache or args.output / "requests.sqlite3", only=args.cache_only)
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    ports = Ports(cache, config, approved=args.approved_endpoint, ledger_root=args.output)
    prompts = official_prompts(args.data / "lme-evaluate-qa.py.txt") if args.dataset == "longmemeval" else {}
    started = time.perf_counter()
    stores = args.output / f"stores-{time.time_ns()}"
    stores.mkdir()

    def execute(indexed):
        index, case = indexed
        # The first wave starts one question at a time, so its captures and embeddings do not all reach the
        # providers in the same few seconds; later questions start whenever a worker frees up.
        if index < args.workers and index * args.start_interval_seconds > 0:
            time.sleep(index * args.start_interval_seconds)
        return isolated(
            lambda one: run_case(
                one,
                root=stores,
                ports=ports,
                prompts=prompts,
                method=args.method,
                retrieval_only=args.retrieval_only,
                dataset=args.dataset,
                settle_seconds=args.settle_seconds,
            ),
            case,
            cache_only=cache.only,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(execute, enumerate(selected)))
    cache.raise_if_missing()
    incomplete = [row for row in results if row["status"] != "complete"]
    results = [row for row in results if row["status"] == "complete"]
    scores = summarize(results, incomplete, fake=args.fake, retrieval_only=args.retrieval_only)
    report = {
        "schema": "public-bench/1",
        "mode": "fake-smoke" if args.fake else "live",
        "dataset": args.dataset,
        "dataset_manifest": manifest,
        "split": args.split,
        "split_manifest_sha256": hashlib.sha256(split_file.read_bytes()).hexdigest()
        if args.dataset == "longmemeval"
        else None,
        "method": args.method,
        "models": ports.config,
        "official_judge_prompts_sha256": digest(prompts) if prompts else None,
        "corpus_categories": category_counts(cases),
        "selected_categories": category_counts(selected),
        "questions": len(selected),
        "completed_questions": len(results),
        "settled_with_failures": sum(
            1 for row in results if (row.get("backend_state") or {}).get("status") == "failed"
        ),
        "results": results,
        "incomplete_results": incomplete,
        "workers": args.workers,
        "settle_seconds": args.settle_seconds,
        "start_interval_seconds": args.start_interval_seconds,
        "total_seconds": time.perf_counter() - started,
        "cache": {"hits": cache.hits, "misses": cache.misses, "operations": dict(cache.operations)},
        "scoring": scores,
        "accuracy": scores["accuracy"],
        "accuracy_ci95": wilson(round(scores["accuracy"] * len(results)), len(results))
        if scores["accuracy"] is not None
        else None,
        "quality_claim": "none"
        if args.fake
        else "within-run comparison only; configured answer model and dataset-specific evaluator",
    }
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"results", "dataset_manifest"}}))
    if incomplete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
