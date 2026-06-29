from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .experience_preflight import experience_preflight
from .response_schemas import EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION


class ReplayCaseValidationError(ValueError):
    """Raised when a replay case file is empty or structurally invalid."""


def _clean_term(term: Any) -> str:
    return " ".join(str(term or "").strip().lower().split())


def _contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    normalized_text = " ".join(str(text or "").lower().split())
    if any(ord(char) > 127 for char in term):
        return term in normalized_text
    if re.fullmatch(r"[a-z0-9][a-z0-9_. -]*", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized_text) is not None
    return term in normalized_text


def coverage_hits(text: str, required_terms: Sequence[Any]) -> list[str]:
    normalized_terms = [_clean_term(term) for term in required_terms]
    return [term for term in normalized_terms if _contains_term(text, term)]


def _coverage_ratio(hits: Sequence[str], required_terms: Sequence[Any]) -> float:
    terms = [_clean_term(term) for term in required_terms if _clean_term(term)]
    if not terms:
        return 1.0
    return round(len(set(hits)) / len(set(terms)), 4)


def _case_id(case: Mapping[str, Any], index: int) -> str:
    return str(case.get("id") or case.get("name") or f"case-{index + 1}")


def load_replay_cases(path: str | Path) -> list[dict[str, Any]]:
    case_path = Path(path)
    text = case_path.read_text(encoding="utf-8")
    if case_path.suffix.lower() == ".jsonl":
        cases = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        raw = json.loads(text)
        if isinstance(raw, dict) and isinstance(raw.get("cases"), list):
            cases = raw["cases"]
        elif isinstance(raw, list):
            cases = raw
        else:
            raise ReplayCaseValidationError("case file must be JSON array, object with cases[], or JSONL")
    if not cases:
        raise ReplayCaseValidationError("case file contains no replay cases")
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ReplayCaseValidationError(f"case {index + 1} must be an object")
        normalized.append(dict(case))
    return normalized


def _average(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return round(sum(items) / len(items), 4)


def _required_terms_from_case(case: Mapping[str, Any]) -> list[Any]:
    raw_terms = case.get("required_terms") if "required_terms" in case else case.get("required_checks")
    if raw_terms is None:
        return []
    if isinstance(raw_terms, str):
        return [raw_terms]
    if isinstance(raw_terms, Mapping):
        raise ReplayCaseValidationError("required_terms must be a string or list, not an object")
    if isinstance(raw_terms, list | tuple):
        return list(raw_terms)
    raise ReplayCaseValidationError("required_terms must be a string or list")


def _min_coverage_gain(case: Mapping[str, Any]) -> float:
    raw_value = case.get("min_coverage_gain", 0.0)
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ReplayCaseValidationError("min_coverage_gain must be a finite non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise ReplayCaseValidationError("min_coverage_gain must be a finite non-negative number")
    return value


def evaluate_replay_case(
    conn: sqlite3.Connection,
    case: Mapping[str, Any],
    *,
    accessible_scope_ids: Sequence[str],
    config: Mapping[str, Any] | None = None,
    index: int = 0,
) -> dict[str, Any]:
    query = str(case.get("query") or case.get("task") or "")
    baseline_text = str(case.get("baseline_text") or case.get("baseline") or "")
    required_terms = _required_terms_from_case(case)
    min_coverage_gain = _min_coverage_gain(case)
    negative_control = bool(case.get("expect_no_reuse")) or str(case.get("case_type") or "positive").lower() in {"negative", "negative_no_reuse", "no_reuse"}
    preflight = experience_preflight(conn, query=query, accessible_scope_ids=accessible_scope_ids, config=config or {})
    packet = str(preflight.get("packet") or "")
    with_experience_text = f"{baseline_text}\n{packet}"
    baseline_hits = coverage_hits(baseline_text, required_terms)
    with_experience_hits = coverage_hits(with_experience_text, required_terms)
    expected_decision = str(case.get("expected_decision") or "")
    expected_playbook_id = str(case.get("expected_playbook_id") or "")
    playbook = preflight.get("playbook") if isinstance(preflight.get("playbook"), Mapping) else {}
    playbook_id = str(playbook.get("id") or "") if isinstance(playbook, Mapping) else ""
    decision = str(preflight.get("decision") or "")
    missing_after = [_clean_term(term) for term in required_terms if _clean_term(term) not in set(with_experience_hits)]
    baseline_coverage = _coverage_ratio(baseline_hits, required_terms)
    with_experience_coverage = _coverage_ratio(with_experience_hits, required_terms)
    coverage_gain = round(with_experience_coverage - baseline_coverage, 4)
    failures: list[str] = []
    if expected_decision and decision != expected_decision:
        failures.append("decision_mismatch")
    if expected_playbook_id and playbook_id != expected_playbook_id:
        failures.append("playbook_mismatch")
    if negative_control:
        if decision != "no_reuse":
            failures.append("negative_control_reused_playbook")
        if packet:
            failures.append("negative_control_packet_rendered")
    else:
        if missing_after:
            failures.append("missing_required_terms")
        if not [_clean_term(term) for term in required_terms if _clean_term(term)]:
            failures.append("empty_required_terms")
        if not packet:
            failures.append("missing_experience_packet")
        if coverage_gain <= min_coverage_gain:
            failures.append("no_coverage_gain")
    passed = not failures
    return {
        "id": _case_id(case, index),
        "query": query,
        "decision": decision,
        "reasons": list(preflight.get("reasons") or []),
        "playbook_id": playbook_id,
        "baseline_hits": baseline_hits,
        "with_experience_hits": with_experience_hits,
        "missing_after_experience": missing_after,
        "baseline_coverage": baseline_coverage,
        "with_experience_coverage": with_experience_coverage,
        "coverage_gain": coverage_gain,
        "packet_chars": len(packet),
        "failures": failures,
        "passed": passed,
    }


def build_replay_report(
    conn: sqlite3.Connection,
    *,
    cases: Sequence[Mapping[str, Any]],
    accessible_scope_ids: Sequence[str],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not cases:
        raise ReplayCaseValidationError("replay report requires at least one case")
    evaluated = [
        evaluate_replay_case(conn, case, accessible_scope_ids=accessible_scope_ids, config=config, index=index)
        for index, case in enumerate(cases)
    ]
    baseline_avg = _average(float(case["baseline_coverage"]) for case in evaluated)
    experience_avg = _average(float(case["with_experience_coverage"]) for case in evaluated)
    return {
        "schema_version": EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION,
        "case_count": len(evaluated),
        "pass_count": sum(1 for case in evaluated if case.get("passed")),
        "average_baseline_coverage": baseline_avg,
        "average_with_experience_coverage": experience_avg,
        "average_coverage_gain": round(experience_avg - baseline_avg, 4),
        "cases": evaluated,
    }
