"""Cross-boundary contracts for the canonical secret-scanning API."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import scope_recall.secret_patterns as secret_patterns
from scope_recall.capture_filters import contains_secret_like_text, sanitize_mapping_key


ROOT = Path(__file__).resolve().parents[1]


def _release_module():
    path = ROOT / "scripts" / "check.release.py"
    spec = importlib.util.spec_from_file_location("scope_recall_release_secret_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OBFUSCATED_ASSIGNMENTS = (
    "api\u200b_key = Alpha1234567890Beta",
    "to\u2060ken: Bravo1234567890Zulu",
    "ｐａｓｓｗｏｒｄ = Charlie1234567890Zulu",  # fixture
    "api\u00a0key: Delta1234567890Zulu",
)


@pytest.mark.parametrize("text", OBFUSCATED_ASSIGNMENTS)
def test_public_api_and_runtime_detect_unicode_shadow_assignments(text):
    matches = secret_patterns.scan_secret_like_text(text)

    assert matches
    assert any(match.name in {"api_key_assignment", "token_assignment"} for match in matches)
    assert contains_secret_like_text(text) is True


@pytest.mark.parametrize(
    "key",
    ("api\u200b_key", "client\u2060secret", "ｐａｓｓｗｏｒｄ", "access\u00a0token"),
)
def test_public_mapping_key_classifier_drives_structured_redaction(key):
    assert secret_patterns.is_sensitive_mapping_key(key) is True
    assert sanitize_mapping_key(key) == ("[REDACTED_KEY]", True)


def test_unicode_shadow_preserves_benign_token_metric_exemption():
    text = "KV_bytes_per_to\u200bken = 32"

    assert secret_patterns.scan_secret_like_text(text) == ()
    assert secret_patterns.is_sensitive_mapping_key("KV_bytes_per_to\u200bken") is False
    assert contains_secret_like_text(text) is False


@pytest.mark.parametrize(
    "text",
    (
        "if claim_token is not None:",
        "assert l4_claim_token is None",
        "token is true",
        "password is false",
    ),
)
def test_python_sentinel_comparisons_are_not_secret_assignments(text):
    assert secret_patterns.scan_secret_like_text(text) == ()
    assert contains_secret_like_text(text) is False


def test_human_readable_is_assignment_still_matches():
    matches = secret_patterns.scan_secret_like_text(
        "access_token is " + "Alpha1234567890Zulu"
    )

    assert any(match.name == "token_assignment" for match in matches)


def test_release_scanner_uses_same_unicode_shadow_api_without_echoing_values(tmp_path):
    module = _release_module()
    for index, text in enumerate(OBFUSCATED_ASSIGNMENTS, 1):
        (tmp_path / f"obfuscated-{index}.yaml").write_text(text + "\n", encoding="utf-8")

    original_root = module.ROOT
    module.ROOT = tmp_path
    try:
        findings = module.scan_tree()
    finally:
        module.ROOT = original_root

    joined = "\n".join(findings["secrets"])
    for index in range(1, len(OBFUSCATED_ASSIGNMENTS) + 1):
        assert f"obfuscated-{index}.yaml" in joined
    for text in OBFUSCATED_ASSIGNMENTS:
        assert text.split()[-1] not in joined
    assert "[REDACTED_SECRET]" in joined


@pytest.mark.parametrize(
    "source",
    (
        'api_key = "sk-" + marker',
        'password = "prefix" + suffix',
        'token = "tok-" + ("A" * 24)',
    ),
)
def test_release_scanner_does_not_misreport_dynamic_secret_fixtures_as_literals(source):
    module = _release_module()

    assert contains_secret_like_text(source) is True
    assert module._scan_sensitive_text(Path("dynamic.py"), source)["secrets"] == []


def test_capture_and_release_delegate_scanning_to_public_secret_api():
    capture_source = (ROOT / "capture_filters.py").read_text(encoding="utf-8")
    release_source = (ROOT / "scripts" / "check.release.py").read_text(encoding="utf-8")

    assert "def _secret_scan_shadow" not in capture_source
    assert "def _is_sensitive_mapping_key" not in capture_source
    assert "contains_secret_like_text" in capture_source
    assert "scan_secret_like_text" in release_source
    assert "SECRET_PATTERNS = {" not in release_source
