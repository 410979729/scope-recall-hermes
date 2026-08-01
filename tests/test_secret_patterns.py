"""Cross-surface secret-pattern contracts for capture and release scanning.

Secret families must be rejected before persistence and before publication. The
fixtures assemble token shapes at runtime so push-protection scanners do not
mistake test source for live credentials.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from scope_recall.capture_filters import (
    contains_secret_like_text,
    redact_secret_like_text,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_check_release_secret_patterns",
        CHECK_RELEASE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _secret_samples() -> dict[str, str]:
    pem_begin = "-----BEGIN " + "PRIVATE KEY-----"
    pem_end = "-----END " + "PRIVATE KEY-----"
    return {
        "database_uri": "postgresql://alice:" + ("p" * 24) + "@db.example/app",
        "github_fine_grained_pat": "github_" + "pat_" + ("A" * 40),
        "gitlab_pat": "gl" + "pat-" + ("B" * 24),
        "npm_token": "npm_" + ("C" * 36),
        "pypi_token": "pypi-" + ("D" * 40),
        "aws_secret_assignment": "aws_secret_access_key=" + ("E" * 40),
        "cookie_header": "Cookie: sessionid=" + ("f" * 32),
        "telegram_bot_token": str(123456789) + ":" + ("G" * 35),
        "discord_token": ("H" * 24) + "." + ("I" * 6) + "." + ("J" * 30),
        "openai_project_key": "sk-" + "proj-" + ("K" * 40),
        "google_api_key": "AIza" + ("L" * 35),
        "pem_private_key": "\n".join(
            [pem_begin, "notreallybase64butsecretbody", pem_end]
        ),
    }


@pytest.mark.parametrize("family", sorted(_secret_samples()))
def test_secret_family_is_blocked_by_capture_and_release_scanner(family):
    secret = _secret_samples()[family]

    assert contains_secret_like_text(secret) is True
    redacted = redact_secret_like_text(secret)
    assert "[REDACTED_SECRET]" in redacted
    assert secret not in redacted

    release_check = _load_release_check_module()
    findings = release_check._scan_sensitive_text(Path("probe.txt"), secret)
    assert findings["secrets"], family
    assert secret not in "\n".join(findings["secrets"])


@pytest.mark.parametrize(
    "text",
    [
        "postgresql://db.example/app",
        "Set the Cookie header after authentication.",
        "npm install uses the frozen lockfile.",
        "The Discord integration accepts messages.",
        "token_count = 4096 is a public model limit.",
    ],
)
def test_secret_patterns_do_not_reject_adjacent_public_text(text):
    assert contains_secret_like_text(text) is False
    assert redact_secret_like_text(text) == text

    release_check = _load_release_check_module()
    assert release_check._scan_sensitive_text(Path("probe.txt"), text)["secrets"] == []
