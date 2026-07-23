"""Entity-quality boundary tests for durable graph indexing."""
from __future__ import annotations

import pytest

from scope_recall.entity_quality import entity_is_indexable
from scope_recall.graph import normalize_entity


@pytest.mark.parametrize(
    "value",
    [
        "helper function",
        "current status",
        "operation",
        "alerts",
        "天玑 operation",
        "天玑 alerts",
    ],
)
def test_generic_or_hybrid_entity_noise_is_rejected(value: str) -> None:
    assert entity_is_indexable(value) is False
    assert normalize_entity(value) == ""


@pytest.mark.parametrize(
    "value",
    ["Scope Recall", "OpenClaw", "Hermes Agent", "home-yu-0001", "天玑"],
)
def test_stable_names_and_identifiers_remain_indexable(value: str) -> None:
    assert entity_is_indexable(value) is True
    assert normalize_entity(value)
