"""Tests for secret-reference index governance."""

from __future__ import annotations

import pytest

from scope_recall.secret_index import build_secret_index


def _synthetic_secret_value() -> str:
    return "sk-" + "test-" + "1234567890abcdef"


def test_secret_index_requires_vault_ref_for_secret_reference():
    with pytest.raises(ValueError, match="vault_ref is required"):
        build_secret_index(
            {
                "label": "Prod API",
                "service": "api",
                "secret_value": _synthetic_secret_value(),
            }
        )


def test_secret_index_stores_reference_metadata_without_plaintext_secret():
    synthetic_secret = _synthetic_secret_value()
    content, metadata = build_secret_index(
        {
            "label": "Prod API",
            "service": "payments",
            "account": "ops",
            "vault_ref": "vault://scope/payments/prod-api",
            "secret_type": "api_key",
            "secret_value": synthetic_secret,
        }
    )

    assert synthetic_secret not in content
    assert "Plaintext secret value: [not stored" in content
    assert metadata["sensitivity"] == "secret_reference"
    assert metadata["vault_ref"] == "vault://scope/payments/prod-api"
    assert metadata["secret_value_stored"] is False
    assert metadata["secret_value_sha256_prefix"]
