"""Compatibility shims must be explicit, owned, tested, and removable."""

from __future__ import annotations

from pathlib import Path

from scope_recall._internal.compatibility.registry import (
    ALL_COMPATIBILITY_IDS,
    COMPATIBILITY_REGISTRY,
    PROGRAM_1A_COMPATIBILITY_IDS,
    PROGRAM_1B_COMPATIBILITY_IDS,
    validate_compatibility_registry,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _split_reference(reference: str) -> tuple[str, str]:
    path, symbol = reference.split(":", 1)
    return path, symbol


def test_compatibility_registry_is_complete() -> None:
    assert validate_compatibility_registry() == ()
    assert {item.shim_id for item in COMPATIBILITY_REGISTRY} == set(
        ALL_COMPATIBILITY_IDS
    )
    assert len(PROGRAM_1A_COMPATIBILITY_IDS) == 12
    assert PROGRAM_1B_COMPATIBILITY_IDS == {"legacy-lifecycle-receipt-adapter"}
    assert {item.remove_after for item in COMPATIBILITY_REGISTRY} == {"2.1.0"}
    assert all(item.removal_condition for item in COMPATIBILITY_REGISTRY)


def test_registered_sources_usage_and_tests_exist() -> None:
    for item in COMPATIBILITY_REGISTRY:
        source_path, source_symbol = _split_reference(item.source)
        source = (PLUGIN_ROOT / source_path).read_text(encoding="utf-8")
        assert source_symbol in source, item.shim_id
        for reference in (*item.usage_evidence, *item.tests):
            path, symbol = _split_reference(reference)
            referenced_source = (PLUGIN_ROOT / path).read_text(encoding="utf-8")
            assert symbol in referenced_source, f"{item.shim_id}: {reference}"
