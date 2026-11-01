"""Paired language acceptance over identical corpus, route and execution protocol."""

from __future__ import annotations

METRICS = ("validation_failure_rate", "false_acceptance_rate", "owner_promotion_rate", "candidate_reason_coverage")


def compare(before, after):
    """Reject incomparable or incomplete runs before applying the four acceptance gates."""
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
    for report in (before, after):
        if not report.get("complete") or not report.get("subject") or not report.get("prompt_sha256"):
            raise ValueError("incomplete report")
    left, right = before["variants"][0], after["variants"][0]
    if left["clause"] is not None or right["clause"] is not None:
        raise ValueError("comparison requires the unmodified prompt variant")
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
        accepted=all(acceptance.values()),
        quality_claim="workflow only" if after["mode"] == "fake" else "synthetic corpus only",
    )
