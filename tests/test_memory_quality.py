"""Tests for sensitivity governance helpers."""

from __future__ import annotations

import json

from scope_recall.capture_filters import should_capture_text
from scope_recall.memory_quality import lint_memory_row, normalize_sensitivity, sensitivity_metadata


def _row(*, content: str = "Durable fact", metadata: dict | None = None, target: str = "memory") -> dict:
    return {
        "id": "memory-1",
        "scope_id": "scope-a",
        "source": "tool-store",
        "target": target,
        "content": content,
        "summary": content,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": json.dumps(metadata or {"memory_type": "factual", "lifecycle": "promoted"}, ensure_ascii=False),
    }


def test_normalize_sensitivity_supports_v1_levels_and_aliases():
    assert normalize_sensitivity("public") == ("public", "")
    assert normalize_sensitivity("normal") == ("internal", "")
    assert normalize_sensitivity("sensitive") == ("restricted", "")
    assert normalize_sensitivity("secret-index", vault_ref="vault://scope/item") == ("secret_reference", "")


def test_normalize_sensitivity_rejects_plaintext_secret_and_missing_vault_ref():
    assert normalize_sensitivity("internal", content="token=abcdefghijklmnopqrstuvwxyz") == (
        "restricted",
        "plaintext_secret_rejected",
    )
    assert normalize_sensitivity("secret_reference") == ("restricted", "secret_reference_missing_vault_ref")
    assert sensitivity_metadata({"sensitivity": "secret_reference", "vault_ref": "vault://scope/item"}) == {
        "sensitivity": "secret_reference",
        "sensitivity_reason": "",
        "vault_ref_required": True,
        "vault_ref_present": True,
    }


def test_lint_memory_row_flags_secret_reference_without_vault_ref():
    rules = lint_memory_row(_row(metadata={"memory_type": "resource", "lifecycle": "promoted", "sensitivity": "secret_reference"}))

    assert "secret_reference_missing_vault_ref" in rules


def test_lint_memory_row_flags_plaintext_secret_content():
    rules = lint_memory_row(_row(content="api_key=abcdefghijklmnopqrstuvwxyz", metadata={"memory_type": "resource", "lifecycle": "promoted"}))

    assert "secret_like_content" in rules
    decision = should_capture_text("api_key=abcdefghijklmnopqrstuvwxyz")
    assert decision.allowed is False
    assert decision.reason == "plaintext_secret_rejected"
