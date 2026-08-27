#!/usr/bin/env python3
"""Frozen, API-free paired benchmark for the Program 4 Context Compiler."""

from __future__ import annotations

import importlib
import importlib.util
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_VERSION = "scope-recall-compiler-synthetic.v1"
HARNESS_VERSION = "scope-recall-recall-compiler-benchmark.v1"
RANDOM_SEED = 0


def _bootstrap_source_package() -> None:
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    try:
        importlib.import_module("scope_recall._internal.recall.compiler")
        return
    except ImportError:
        for name in list(sys.modules):
            if name == "scope_recall" or name.startswith("scope_recall."):
                sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "scope_recall", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to bootstrap scope_recall source package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)


_bootstrap_source_package()

from scope_recall._internal.recall.compiler import (  # noqa: E402
    CandidateSet,
    CompilerPolicy,
    RecallPacket,
    compile_recall_packet,
    paired_packet_diff,
)
from scope_recall.models import RecallItem  # noqa: E402


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    candidates: tuple[RecallItem, ...]
    expected_ids: frozenset[str]
    stale_ids: frozenset[str]


def _item(
    memory_id: str,
    *,
    score: float,
    signals: int = 1,
    fact_key: str = "",
    truth: str = "untracked",
    topic: str = "",
    long_summary: bool = False,
) -> RecallItem:
    metadata: dict[str, object] = {
        "scope_id": "benchmark-scope",
        "lexical_score": min(1.0, score),
    }
    if signals >= 2:
        metadata["vector_score"] = min(1.0, score)
    if signals >= 3:
        metadata["rrf_score"] = min(1.0, score)
    if fact_key:
        metadata.update(
            {
                "fact_claim_id": f"claim:{memory_id}",
                "fact_claim_key": fact_key,
                "fact_freshness_status": truth,
            }
        )
    if topic:
        metadata["topic"] = topic
    summary = (
        f"{memory_id} verified evidence " * 40
        if long_summary
        else f"{memory_id} verified evidence"
    )
    return RecallItem(
        id=memory_id,
        content=summary,
        summary=summary,
        source="benchmark-fixture",
        target="project",
        score=score,
        updated_at="2026-08-27T00:00:00+00:00",
        metadata=metadata,
    )


def _dataset() -> tuple[BenchmarkCase, ...]:
    truth_candidates = (
        _item("stale-city", score=0.99, fact_key="fact:city", truth="stale"),
        _item("noise-a", score=0.98, topic="noise"),
        _item("noise-b", score=0.97, topic="noise"),
        _item("noise-c", score=0.96, topic="noise"),
        _item("noise-d", score=0.95, topic="noise"),
        _item("current-city", score=0.80, signals=3, fact_key="fact:city", truth="current"),
        _item("evidence-a", score=0.79, signals=3, topic="a"),
        _item("evidence-b", score=0.78, signals=3, topic="b"),
        _item("evidence-c", score=0.77, signals=3, topic="c"),
        _item("evidence-d", score=0.76, signals=3, topic="d"),
    )
    budget_candidates = tuple(
        _item(
            f"budget-{index}",
            score=0.90 - index / 100,
            signals=3 if index < 5 else 1,
            topic=f"topic-{index}",
            long_summary=True,
        )
        for index in range(8)
    )
    return (
        BenchmarkCase(
            name="stale-current-and-evidence",
            candidates=truth_candidates,
            expected_ids=frozenset(
                {"current-city", "evidence-a", "evidence-b", "evidence-c", "evidence-d"}
            ),
            stale_ids=frozenset({"stale-city"}),
        ),
        BenchmarkCase(
            name="token-budget",
            candidates=budget_candidates,
            expected_ids=frozenset({f"budget-{index}" for index in range(3)}),
            stale_ids=frozenset(),
        ),
    )


def _legacy_policy() -> CompilerPolicy:
    return CompilerPolicy(
        limit=5,
        token_budget=320,
        per_item_token_budget=64,
        current_truth_enabled=False,
        evidence_order_enabled=False,
        diversity_enabled=False,
        budgeter_enabled=False,
    )


def _candidate_policy() -> CompilerPolicy:
    return CompilerPolicy(
        limit=5,
        token_budget=320,
        per_item_token_budget=64,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _quality(cases: tuple[BenchmarkCase, ...], packets: list[RecallPacket]) -> dict[str, float]:
    stale_hits = 0
    any_hits = 0
    all_hits = 0
    token_counts: list[int] = []
    for case, packet in zip(cases, packets, strict=True):
        returned = {item.item.id for item in packet.items[:5]}
        stale_hits += int(bool(returned & case.stale_ids))
        any_hits += int(bool(returned & case.expected_ids))
        all_hits += int(case.expected_ids <= returned)
        token_counts.append(packet.estimated_tokens)
    denominator = max(1, len(cases))
    return {
        "stale_injection_rate": stale_hits / denominator,
        "top5_any_hit": any_hits / denominator,
        "top5_all_hit": all_hits / denominator,
        "token_p50": statistics.median(token_counts),
        "token_p95": float(_percentile([float(value) for value in token_counts], 0.95)),
    }


def run_benchmark(*, iterations: int = 500) -> dict[str, object]:
    cases = _dataset()
    candidate_sets = [CandidateSet.from_items(case.candidates) for case in cases]
    legacy_packets = [
        compile_recall_packet(candidate_set, _legacy_policy())
        for candidate_set in candidate_sets
    ]
    candidate_packets = [
        compile_recall_packet(candidate_set, _candidate_policy())
        for candidate_set in candidate_sets
    ]
    # Full item diffs are intentionally confined to this isolated fixture.
    paired = [
        paired_packet_diff(legacy, candidate, isolated=True)
        for legacy, candidate in zip(legacy_packets, candidate_packets, strict=True)
    ]

    legacy_timings: list[float] = []
    candidate_timings: list[float] = []
    for _ in range(max(1, iterations)):
        for candidate_set in candidate_sets:
            started = time.perf_counter()
            compile_recall_packet(candidate_set, _legacy_policy())
            legacy_timings.append((time.perf_counter() - started) * 1000.0)
            started = time.perf_counter()
            compile_recall_packet(candidate_set, _candidate_policy())
            candidate_timings.append((time.perf_counter() - started) * 1000.0)

    legacy_quality = _quality(cases, legacy_packets)
    candidate_quality = _quality(cases, candidate_packets)
    legacy_p95 = _percentile(legacy_timings, 0.95)
    candidate_p95 = _percentile(candidate_timings, 0.95)
    gates = {
        "same_candidate_set": all(bool(diff["same_candidate_set"]) for diff in paired),
        "stale_injection_improved": (
            candidate_quality["stale_injection_rate"]
            < legacy_quality["stale_injection_rate"]
        ),
        "top5_any_not_regressed": (
            candidate_quality["top5_any_hit"] >= legacy_quality["top5_any_hit"]
        ),
        "top5_all_improved": (
            candidate_quality["top5_all_hit"] > legacy_quality["top5_all_hit"]
        ),
        "token_p95_improved": (
            candidate_quality["token_p95"] < legacy_quality["token_p95"]
        ),
        "latency_within_gate": candidate_p95 <= max(legacy_p95 * 5.0, legacy_p95 + 1.0),
    }
    return {
        "schema": HARNESS_VERSION,
        "frozen_inputs": {
            "dataset": DATASET_VERSION,
            "answerer": "none",
            "judge": "exact-id-set.v1",
            "embedder": "none",
            "prompt": "none",
            "harness": HARNESS_VERSION,
            "random_seed": RANDOM_SEED,
        },
        "case_count": len(cases),
        "iterations": max(1, iterations),
        "legacy": {
            **legacy_quality,
            "latency_p50_ms": statistics.median(legacy_timings),
            "latency_p95_ms": legacy_p95,
        },
        "candidate": {
            **candidate_quality,
            "latency_p50_ms": statistics.median(candidate_timings),
            "latency_p95_ms": candidate_p95,
        },
        "gates": gates,
        "ok": all(gates.values()),
    }


def main() -> int:
    result = run_benchmark()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(result["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
