"""Shared finite-range coercion for the ranking weights this call path reads."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .tuning import (
    DEFAULT_ENTITY_DISTANCE_WEIGHT,
    DEFAULT_ENTITY_WEIGHT,
    DEFAULT_METADATA_WEIGHT,
)

WEIGHT_RANGE = (0.0, 1.0)
WEIGHT_SPECS: dict[str, tuple[float, float, float]] = {
    "metadata_weight": (DEFAULT_METADATA_WEIGHT, *WEIGHT_RANGE),
    "entity_weight": (DEFAULT_ENTITY_WEIGHT, *WEIGHT_RANGE),
    "entity_distance_weight": (DEFAULT_ENTITY_DISTANCE_WEIGHT, *WEIGHT_RANGE),
}


def coerce_retrieval_weight(
    value: Any,
    *,
    default: float,
    name: str,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    """Treat missing/None as ``default`` and keep an explicit finite zero.

    Bool, NaN, Inf, negatives, non-numeric values, and out-of-range numbers
    are rejected so doctor and ranking cannot silently disagree.
    """

    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        ) from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(
            f"{name} must be a finite number between {minimum:g} and {maximum:g}"
        )
    return parsed


def effective_retrieval_weights(
    retrieval_cfg: Mapping[str, Any] | None,
) -> dict[str, float]:
    """Return the ranking weights the orchestrator actually applies."""

    cfg = retrieval_cfg if isinstance(retrieval_cfg, Mapping) else {}
    return {
        name: coerce_retrieval_weight(
            cfg.get(name),
            default=default,
            name=name,
            minimum=minimum,
            maximum=maximum,
        )
        for name, (default, minimum, maximum) in WEIGHT_SPECS.items()
    }
