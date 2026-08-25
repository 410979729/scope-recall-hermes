"""Shared fail-closed validation for embedding adapter batches.

Hosted providers, local model adapters, and full-sync writers must agree on
one contract: exact batch count, configured dimension, finite values, and
nonzero vectors. A mismatch is never a partial success.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable


def _raw_vector(row: Any) -> Any:
    embedding = getattr(row, "embedding", None)
    return row if embedding is None else embedding


def validate_embedding_batch(
    rows: Any,
    *,
    expected_count: int,
    expected_dimensions: int,
    provider: str,
    extract_vector: Callable[[Any], Any] = _raw_vector,
) -> list[list[float]]:
    """Return validated float vectors or raise before any caller can write.

    ``zip(..., strict=True)`` is used so an N-1 or N+1 response cannot be
    silently truncated or padded. NaN, Inf, wrong width, and all-zero rows
    fail closed with a batch offset for diagnosis.
    """

    if expected_count < 0:
        raise RuntimeError(f"{provider} embedding expected_count is negative")
    if expected_dimensions <= 0:
        raise RuntimeError(f"{provider} embedding expected dimension is invalid")
    try:
        items = list(rows)
    except TypeError as exc:
        raise RuntimeError(f"{provider} embedding response is not a sequence") from exc
    if len(items) != expected_count:
        raise RuntimeError(
            f"{provider} embedding response count {len(items)} does not match "
            f"input count {expected_count}"
        )
    vectors: list[list[float]] = []
    for offset, row in zip(range(expected_count), items, strict=True):
        try:
            raw = extract_vector(row)
            vector = [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{provider} embedding contains a non-numeric value at batch offset {offset}"
            ) from exc
        if len(vector) != expected_dimensions:
            raise RuntimeError(
                f"{provider} embedding dimensions {len(vector)} do not match configured "
                f"{expected_dimensions} at batch offset {offset}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(
                f"{provider} embedding contains non-finite values at batch offset {offset}"
            )
        if not any(value != 0.0 for value in vector):
            raise RuntimeError(
                f"{provider} embedding contains a zero vector at batch offset {offset}"
            )
        vectors.append(vector)
    return vectors


def zip_embedding_rows(
    rows: Iterable[Any],
    vectors: Iterable[Any],
    *,
    provider: str,
) -> list[tuple[Any, Any]]:
    """Pair source rows with vectors using strict zip so count drift cannot write."""

    try:
        return list(zip(rows, vectors, strict=True))
    except ValueError as exc:
        raise RuntimeError(
            f"{provider} embedding count does not match the source batch"
        ) from exc
