"""Model-specific calibration contract for vector-only recall filtering."""

from __future__ import annotations

import json
from pathlib import Path

from scope_recall.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks" / "vector_only_threshold_calibration_v1.json"


def _metrics(records: list[dict[str, object]], threshold: float) -> dict[str, float | int]:
    true_positive = sum(
        int(record["label"]) == 1 and float(record["score"]) >= threshold
        for record in records
    )
    false_negative = sum(
        int(record["label"]) == 1 and float(record["score"]) < threshold
        for record in records
    )
    false_positive = sum(
        int(record["label"]) == 0 and float(record["score"]) >= threshold
        for record in records
    )
    true_negative = sum(
        int(record["label"]) == 0 and float(record["score"]) < threshold
        for record in records
    )
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    return {
        "tp": true_positive,
        "fp": false_positive,
        "tn": true_negative,
        "fn": false_negative,
        "precision": precision,
        "recall": recall,
        "weighted_error": 2 * false_positive + false_negative,
    }


def test_default_vector_only_threshold_matches_gemini_calibration_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = list(fixture["records"])
    default = float(DEFAULT_CONFIG["retrieval"]["vector_only_min_score"])

    assert fixture["text_policy"].startswith("Public synthetic")
    assert fixture["model"] == DEFAULT_CONFIG["vector"]["embedder"]["model"]
    assert fixture["dimensions"] == DEFAULT_CONFIG["vector"]["embedder"][
        "dimensions"
    ]
    assert len(records) == 72
    assert sum(int(record["label"]) == 1 for record in records) == 24
    assert sum(int(record["label"]) == 0 for record in records) == 48
    assert default == float(fixture["recommended_default"]) == 0.70

    calibrated = _metrics(records, default)
    baseline = _metrics(records, 0.65)
    assert calibrated["precision"] >= 0.75
    assert calibrated["recall"] >= 0.80
    assert calibrated["weighted_error"] <= baseline["weighted_error"] * 0.60
    assert calibrated["tp"] == fixture["recommended_metrics"]["tp"]
    assert calibrated["fp"] == fixture["recommended_metrics"]["fp"]
    assert calibrated["fn"] == fixture["recommended_metrics"]["fn"]
