#!/usr/bin/env python3
"""Deterministic citation and read-only benchmark for reflection synthesis."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from threading import RLock
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "reflection_cases.json"


def _bootstrap_source_package() -> None:
    source_init = (ROOT / "__init__.py").resolve()
    loaded_package = sys.modules.get("scope_recall")
    loaded_file = getattr(loaded_package, "__file__", None)
    if loaded_file is not None:
        try:
            if Path(loaded_file).resolve() == source_init:
                return
        except OSError:
            pass
    for loaded_path in getattr(loaded_package, "__path__", ()):
        try:
            if Path(loaded_path).resolve() == ROOT.resolve():
                return
        except OSError:
            pass
    for name in list(sys.modules):
        if name == "scope_recall" or name.startswith("scope_recall."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "scope_recall",
        source_init,
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to bootstrap scope_recall source package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)


_bootstrap_source_package()

from scope_recall.models import RecallItem  # noqa: E402
from scope_recall.reflection import (  # noqa: E402
    ReflectionBudget,
    build_reflection_evidence_pack,
)
from scope_recall.reflection_llm import (  # noqa: E402
    ReflectionLLMError,
    parse_reflection_response,
    synthesize_reflection,
)
from scope_recall.reflection_grounding import (  # noqa: E402
    grounded_candidate_synthesis,
    text_grounding,
)
from scope_recall.sql_store import ensure_schema  # noqa: E402


_BENCHMARK_MEMORIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "one",
        "Project Aurora uses PostgreSQL.",
        "Aurora database",
        "benchmark-a",
    ),
    (
        "two",
        "Project Aurora deploys in eu-west-1.",
        "Aurora deployment",
        "benchmark-b",
    ),
    (
        "three",
        "Alice manages Bob.",
        "Management relationship",
        "benchmark-c",
    ),
    (
        "four",
        "Project Borealis does not use PostgreSQL.",
        "Borealis database exclusion",
        "benchmark-d",
    ),
    (
        "five",
        "Migration Alpha happens before Migration Beta.",
        "Migration order",
        "benchmark-e",
    ),
    (
        "six",
        "If canary verification passes, production rollout may begin.",
        "Conditional rollout",
        "benchmark-f",
    ),
    (
        "seven",
        "Every Aurora deployment uses signed artifacts.",
        "Deployment artifact policy",
        "benchmark-g",
    ),
    (
        "eight",
        "Project Borealis previously used MySQL.",
        "Historical Borealis database",
        "benchmark-h",
    ),
)


class BenchmarkProvider:
    def __init__(self) -> None:
        self._config = {"temporal_queries": {"enabled": True, "timezone": "UTC"}}
        self._retrieval_config = {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": False,
            "min_score": 0.0,
        }
        self._vector_config: dict[str, Any] = {}
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._lock = RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(self._conn)
        self._seed()

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _seed(self) -> None:
        for memory_id, content, summary, source in _BENCHMARK_MEMORIES:
            self._conn.execute(
                """
                INSERT INTO memories(
                    id, scope_id, source, target, content, summary,
                    created_at, updated_at, metadata
                ) VALUES (?, 'scope-a', ?, 'project', ?, ?,
                    '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00',
                    '{"lifecycle":"promoted","memory_type":"project"}')
                """,
                (memory_id, source, content, summary),
            )
        self._conn.commit()

    def _search_curated_memories(self, _query: str) -> list[RecallItem]:
        return []

    def _search_db_memories(self, _query: str, *, limit: int) -> list[RecallItem]:
        return [
            RecallItem(
                id=memory_id,
                content=content,
                summary=summary,
                source=source,
                target="project",
                score=0.99 - (index * 0.001),
                updated_at="2026-01-01T00:00:00+00:00",
                metadata={
                    "scope_id": "scope-a",
                    "lifecycle": "promoted",
                    "memory_type": "project",
                    "lexical_score": 0.99 - (index * 0.001),
                    "vector_score": 0.0,
                    "importance": 0.9,
                },
            )
            for index, (memory_id, content, summary, source) in enumerate(
                _BENCHMARK_MEMORIES
            )
        ][:limit]

    def _search_vector_memories(
        self,
        _query: str,
        *,
        limit: int,
    ) -> list[RecallItem]:
        del limit
        return []

    def _dedup_key(self, content: str) -> str:
        return str(content).strip().casefold()

    def _config_value(self, key: str, default: Any) -> Any:
        return self._config.get(key, default)


def _all_citations(result: Any) -> set[str]:
    output = set(result.citations)
    for group in (result.observations, result.inferences, result.uncertainties):
        for statement in group:
            output.update(statement.citations)
    return output


def _invalid_payload(base: dict[str, Any], mutation: str) -> str:
    payload = json.loads(json.dumps(base))
    if mutation == "unknown_top_level":
        payload["citations"].append("memory:forged")
        return json.dumps(payload)
    if mutation == "unknown_statement":
        payload["observations"][0]["citations"] = ["memory:forged"]
        payload["citations"] = ["memory:forged"]
        return json.dumps(payload)
    if mutation == "markdown_fence":
        return "```json\n" + json.dumps(payload) + "\n```"
    raise ValueError("unknown reflection benchmark mutation")


def run_benchmark(cases_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    valid_cases = payload.get("valid_responses")
    invalid_cases = payload.get("invalid_responses")
    thresholds = payload.get("thresholds")
    if not isinstance(valid_cases, list) or len(valid_cases) < 8:
        raise ValueError("reflection benchmark requires at least eight valid responses")
    if not isinstance(invalid_cases, list) or len(invalid_cases) < 3:
        raise ValueError("reflection benchmark requires at least three invalid responses")
    if not isinstance(thresholds, dict):
        raise ValueError("reflection benchmark thresholds are required")

    provider = BenchmarkProvider()
    before = provider._conn.total_changes
    pack = build_reflection_evidence_pack(
        provider,
        # This benchmark validates reflection grounding over all eight fixed
        # evidence shapes, not the relevance of a single recall query.  Name
        # every fixture concept so the production query-signal gate honestly
        # admits the complete benchmark evidence pack.
        query=(
            "Project Aurora Borealis PostgreSQL deployment eu-west-1 Alice Bob "
            "Migration Alpha Beta canary production rollout signed artifacts MySQL"
        ),
        budget=ReflectionBudget(max_evidence=8, max_chars=4_000),
    )
    allowed = {item.evidence_id for item in pack.evidence}
    valid_results: list[dict[str, Any]] = []
    citation_checks = 0
    separation_checks = 0
    grounded_candidate_checks = 0
    for case in valid_cases:
        raw = json.dumps(case["payload"], sort_keys=True)
        result = synthesize_reflection(
            pack,
            transport=lambda _prompt, response=raw: response,
        )
        citations_valid = _all_citations(result).issubset(allowed)
        observations = {statement.text for statement in result.observations}
        inferences = {statement.text for statement in result.inferences}
        separated = observations.isdisjoint(inferences)
        grounding = grounded_candidate_synthesis(result, pack.evidence)
        grounded_candidate = grounding.synthesis is not None and not grounding.reason
        citation_checks += int(citations_valid)
        separation_checks += int(separated)
        grounded_candidate_checks += int(grounded_candidate)
        valid_results.append(
            {
                "id": case["id"],
                "citations_valid": citations_valid,
                "observation_inference_separated": separated,
                "grounded_candidate": grounded_candidate,
            }
        )

    base = valid_cases[0]["payload"]
    invalid_results: list[dict[str, Any]] = []
    unknown_attempts = 0
    unknown_rejections = 0
    fenced_rejected = False
    for case in invalid_cases:
        mutation = str(case["mutation"])
        if mutation.startswith("unknown_"):
            unknown_attempts += 1
        try:
            parse_reflection_response(
                _invalid_payload(base, mutation),
                allowed_citations=frozenset(allowed),
            )
            rejected = False
        except ReflectionLLMError:
            rejected = True
        if mutation.startswith("unknown_") and rejected:
            unknown_rejections += 1
        if mutation == "markdown_fence":
            fenced_rejected = rejected
        invalid_results.append(
            {"id": case["id"], "mutation": mutation, "rejected": rejected}
        )

    unsupported_payload = json.loads(json.dumps(base))
    unsupported_payload["answer"] = (
        "Aurora uses PostgreSQL. Aurora uses PostgreSQL for durable project state. "
        "Aurora is deployed in eu-west-1. Aurora deploys in eu-west-1. Aurora uses redis."
    )
    unsupported_result = parse_reflection_response(
        json.dumps(unsupported_payload),
        allowed_citations=frozenset(allowed),
    )
    unsupported_grounding = grounded_candidate_synthesis(
        unsupported_result,
        pack.evidence,
    )
    unsupported_answer_rejected = (
        unsupported_grounding.synthesis is None
        and unsupported_grounding.reason == "unsupported_answer"
    )

    evidence_by_id = {item.evidence_id: item for item in pack.evidence}
    semantic_adversarial_cases = (
        ("Bob manages Alice.", "memory:three"),
        ("Project Borealis uses PostgreSQL.", "memory:four"),
        ("Migration Beta happens before Migration Alpha.", "memory:five"),
        ("Production rollout may begin.", "memory:six"),
        ("Aurora deployment uses signed artifacts.", "memory:seven"),
        ("Project Borealis used MySQL.", "memory:eight"),
    )
    semantic_positive_cases = (
        ("Alice manages Bob.", "memory:three"),
        ("Project Borealis does not use PostgreSQL.", "memory:four"),
        ("Migration Alpha happens before Migration Beta.", "memory:five"),
        (
            "If canary verification passes, production rollout may begin.",
            "memory:six",
        ),
        ("Every Aurora deployment uses signed artifacts.", "memory:seven"),
        ("Project Borealis previously used MySQL.", "memory:eight"),
    )
    semantic_adversarial_rejections = sum(
        not text_grounding(
            text,
            citations=(citation,),
            evidence_by_id=evidence_by_id,
        ).supported
        for text, citation in semantic_adversarial_cases
    )
    semantic_positive_acceptances = sum(
        text_grounding(
            text,
            citations=(citation,),
            evidence_by_id=evidence_by_id,
        ).supported
        for text, citation in semantic_positive_cases
    )
    semantic_adversarial_rejection_rate = (
        semantic_adversarial_rejections / len(semantic_adversarial_cases)
    )
    semantic_positive_acceptance_rate = (
        semantic_positive_acceptances / len(semantic_positive_cases)
    )

    write_delta = provider._conn.total_changes - before
    citation_rate = citation_checks / len(valid_cases)
    separation_rate = separation_checks / len(valid_cases)
    grounded_candidate_rate = grounded_candidate_checks / len(valid_cases)
    unknown_rate = unknown_rejections / unknown_attempts
    metrics = {
        "valid_response_count": len(valid_cases),
        "citation_validity_rate": citation_rate,
        "observation_inference_separation_rate": separation_rate,
        "grounded_candidate_rate": grounded_candidate_rate,
        "unsupported_answer_rejected": unsupported_answer_rejected,
        "semantic_adversarial_case_count": len(semantic_adversarial_cases),
        "semantic_adversarial_rejection_rate": semantic_adversarial_rejection_rate,
        "semantic_positive_acceptance_rate": semantic_positive_acceptance_rate,
        "write_delta": write_delta,
        "unknown_reference_rejection_rate": unknown_rate,
        "fenced_json_rejected": fenced_rejected,
    }
    passed = (
        citation_rate >= float(thresholds["citation_validity_rate"])
        and separation_rate
        >= float(thresholds["observation_inference_separation_rate"])
        and grounded_candidate_rate
        >= float(thresholds["grounded_candidate_rate"])
        and len(valid_cases) >= int(thresholds["minimum_valid_response_count"])
        and semantic_adversarial_rejection_rate
        >= float(thresholds["semantic_adversarial_rejection_rate"])
        and semantic_positive_acceptance_rate
        >= float(thresholds["semantic_positive_acceptance_rate"])
        and write_delta <= int(thresholds["max_write_delta"])
        and unknown_rate >= float(thresholds["unknown_reference_rejection_rate"])
        and (
            not thresholds.get("require_unsupported_answer_rejection")
            or unsupported_answer_rejected
        )
        and (
            not thresholds.get("require_fenced_json_rejection")
            or fenced_rejected
        )
    )
    provider._conn.close()
    return {
        "schema_version": payload.get("schema_version"),
        "passed": passed,
        "thresholds": thresholds,
        "metrics": metrics,
        "valid_cases": valid_results,
        "invalid_cases": invalid_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic Scope Recall reflection benchmark"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json", action="store_true", help="Output JSON (default)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_benchmark(args.cases)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
