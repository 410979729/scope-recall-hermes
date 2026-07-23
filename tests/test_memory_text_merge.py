"""Segment-aware memory text merge contracts.

Reviewed/manual merges may combine independent assertions, while automatic
pipelines must never grow slash-delimited duplicate chains.
"""
from __future__ import annotations

from scope_recall.memory_text_merge import (
    combine_reviewed_memory_text,
    deduplicate_memory_text,
)


def test_deduplicate_memory_text_removes_exact_and_contained_slash_segments() -> None:
    text = (
        "B1：中段token漏召回。 / B2：配置缺失。 / B3：stale snapshot。 / "
        "B1：中段token漏召回。 / B2：配置缺失。"
    )

    cleaned = deduplicate_memory_text(text)

    assert cleaned.count("B1：中段token漏召回") == 1
    assert cleaned.count("B2：配置缺失") == 1
    assert cleaned.count("B3：stale snapshot") == 1
    assert " / " not in cleaned
    assert "\n§\n" in cleaned


def test_combine_reviewed_memory_text_uses_explicit_boundaries_without_duplication() -> None:
    merged = combine_reviewed_memory_text(
        "Joy prefers concise replies.",
        "Joy likes brief responses.",
    )

    assert "Joy prefers concise replies" in merged
    assert "Joy likes brief responses" in merged
    assert " / " not in merged
    assert "\n§\n" in merged


def test_combine_reviewed_memory_text_keeps_richer_containing_assertion_only() -> None:
    merged = combine_reviewed_memory_text(
        "Scope Recall uses SQLite.",
        "Scope Recall uses SQLite as truth and LanceDB as a rebuildable companion.",
    )

    assert merged == "Scope Recall uses SQLite as truth and LanceDB as a rebuildable companion."
