"""Paired language acceptance over identical corpus, route and execution protocol."""

from __future__ import annotations

import math
from collections import Counter

from subject import digest

METRICS = ("validation_failure_rate", "false_acceptance_rate", "owner_promotion_rate", "candidate_reason_coverage")


ROW_FIELDS = {
    "id": str,
    "gold_active": bool,
    "active": bool,
    "promoted_within_1200": bool,
    "validation_failed": bool,
    "discarded_proposals": int,
    "delay_seconds": (int, float, type(None)),
    "states": list,
    "receipts": list,
    "candidate_count": int,
    "candidates_with_reasons": int,
    "model_calls": dict,
    "prompt_sha256": dict,
    "operational_errors": list,
    "requests_with_clause_removed": int,
}


def validation_failed(receipts, discarded):
    """Count per-item rejections even when their surrounding work completed."""
    return bool(discarded) or any(
        r["error"] in {"derivation_invalid", "DERIVATION_INVALID", "INPUT_INVALID"} for r in receipts
    )


def summarize(rows):
    """Count validation failures, false acceptance, timely promotion and every candidate reason."""
    positives = [r for r in rows if r["gold_active"]]
    negatives = [r for r in rows if not r["gold_active"]]
    candidates = sum(r.get("candidate_count", 0) for r in rows)
    reasoned = sum(r.get("candidates_with_reasons", 0) for r in rows)
    return dict(
        cases=len(rows),
        discarded_proposals=sum(r["discarded_proposals"] for r in rows),
        cases_with_discarded_proposals=sum(r["discarded_proposals"] > 0 for r in rows),
        validation_failure_rate=sum(r["validation_failed"] for r in rows) / len(rows),
        false_acceptance_rate=sum(r["active"] for r in negatives) / max(1, len(negatives)),
        owner_promotion_rate=sum(r["promoted_within_1200"] for r in positives) / max(1, len(positives)),
        candidate_reasons=dict(Counter(s["reason"] for r in rows for s in r["states"] if s["state"] == "proposed")),
        candidate_count=candidates,
        candidates_with_reasons=reasoned,
        candidate_reason_coverage=reasoned / candidates if candidates else 1.0,
    )


def _fields(value, required):
    """Require each declared field and its JSON type without accepting bool as int."""
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    for name, kind in required.items():
        types = kind if isinstance(kind, tuple) else (kind,)
        if name not in value or type(value[name]) not in types:
            raise ValueError("missing or invalid field: " + name)


def _count(value):
    return type(value) is int and value >= 0


def _validate_traces(row):
    for state in row["states"]:
        _fields(state, dict(state=str, reason=str, at=str, current=bool))
    for receipt in row["receipts"]:
        _fields(receipt, dict(type=str, disposition=str, error=(str, type(None))))
    if row["operational_errors"]:
        raise ValueError("case has operational errors")
    if any(not isinstance(key, str) or not key or not _count(value) for key, value in row["model_calls"].items()):
        raise ValueError("invalid model call counts")
    if row["model_calls"].keys() != row["prompt_sha256"].keys():
        raise ValueError("missing prompt trace")
    for values in row["prompt_sha256"].values():
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ValueError("invalid prompt trace")


def _validate_row(row):
    _fields(row, ROW_FIELDS)
    _validate_traces(row)
    counts = ("candidate_count", "candidates_with_reasons", "discarded_proposals", "requests_with_clause_removed")
    if not row["id"] or any(not _count(row[key]) for key in counts):
        raise ValueError("invalid case identity or counts")
    active = any(state["state"] == "active" for state in row["states"])
    candidates = [s for s in row["states"] if s["current"] and s["state"] in {"proposed", "disputed"}]
    expected = (len(candidates), sum(bool(s["reason"].strip()) for s in candidates))
    if (row["candidate_count"], row["candidates_with_reasons"]) != expected:
        raise ValueError("candidate counts disagree with states")
    delay = row["delay_seconds"]
    if delay is not None and (not math.isfinite(delay) or delay < 0):
        raise ValueError("invalid publication delay")
    if row["active"] != active or active != (delay is not None):
        raise ValueError("publication result disagrees with states")
    if row["promoted_within_1200"] != (active and delay <= 1200):
        raise ValueError("promotion result disagrees with delay")
    if row["validation_failed"] != validation_failed(row["receipts"], row["discarded_proposals"]):
        raise ValueError("validation result disagrees with rejections")


def _checked_variant(report):
    _fields(report, dict(complete=bool, subject=dict, prompt_sha256=dict, variants=list, case_ids_sha256=str))
    if not report["complete"] or not report["subject"] or not report["prompt_sha256"] or not report["variants"]:
        raise ValueError("incomplete report")
    variant = report["variants"][0]
    _fields(variant, dict(rows=list, metrics=dict, clause=type(None)))
    rows = variant["rows"]
    if not rows:
        raise ValueError("missing per-case results")
    for row in rows:
        _validate_row(row)
    identities = [row["id"] for row in rows]
    if len(set(identities)) != len(rows) or digest(identities) != report["case_ids_sha256"]:
        raise ValueError("case identities disagree with manifest")
    calculated = summarize(rows)
    if variant["metrics"] != calculated or any(
        type(variant["metrics"][k]) is not type(v) for k, v in calculated.items()
    ):
        raise ValueError("summary disagrees with per-case results")
    return variant


def compare(before, after):
    """Reject incomparable or incomplete runs before applying the four acceptance gates."""
    left, right = _checked_variant(before), _checked_variant(after)
    for key in (
        "schema",
        "mode",
        "corpus_sha256",
        "case_ids_sha256",
        "model_config_sha256",
        "protocol_sha256",
        "evaluator_sha256",
    ):
        if key not in before or before[key] != after.get(key):
            raise ValueError("incomparable reports: " + key)
    if [(r["id"], r["gold_active"]) for r in left["rows"]] != [(r["id"], r["gold_active"]) for r in right["rows"]]:
        raise ValueError("case identities or labels differ")
    old, new = left["metrics"], right["metrics"]
    acceptance = dict(
        validation_improves=new["validation_failure_rate"] < old["validation_failure_rate"],
        false_acceptance_not_worse=new["false_acceptance_rate"] <= old["false_acceptance_rate"],
        owner_promotion_at_least_90_percent=new["owner_promotion_rate"] >= 0.9,
        all_candidates_have_reasons=new["candidate_reason_coverage"] == 1.0,
    )
    return dict(
        before={key: old[key] for key in METRICS},
        after={key: new[key] for key in METRICS},
        acceptance=acceptance,
        discarded_proposals=dict(before=old["discarded_proposals"], after=new["discarded_proposals"]),
        cases_with_discarded_proposals=dict(
            before=old["cases_with_discarded_proposals"], after=new["cases_with_discarded_proposals"]
        ),
        accepted=all(acceptance.values()),
        quality_claim="workflow only" if after["mode"] == "fake" else "synthetic corpus only",
    )
