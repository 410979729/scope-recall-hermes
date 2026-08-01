"""Canonical secret-value patterns shared by capture and release boundaries.

The patterns intentionally target credential *values* and transport formats.
Context-sensitive mapping keys and token-metric exemptions remain in their
callers because those policies need parsed key information rather than regexes.
"""

from __future__ import annotations

import re


COMMON_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "pem_private_key_block": re.compile(
        r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?"
        r"-----END [A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?-----",
        re.IGNORECASE,
    ),
    # A dangling BEGIN marker is sensitive too: truncated key blocks must fail
    # closed even when the corresponding END marker is missing.
    "pem_private_key_begin": re.compile(
        r"-----BEGIN (?:[A-Z0-9-]+[ ]+)*PRIVATE KEY(?:[ ]+BLOCK)?-----",
        re.IGNORECASE,
    ),
    "database_uri_with_password": re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis(?:s)?|"
        r"amqp(?:s)?|mssql)://[^/\s:@]+:[^@\s/]+@[^\s]+",
        re.IGNORECASE,
    ),
    "openai_key": re.compile(
        r"(?<![A-Za-z0-9_-])sk-(?:(?:proj|ant-api\d{2})-)?"
        r"[A-Za-z0-9_*.-]{16,}(?![A-Za-z0-9_-])"
    ),
    "github_token": re.compile(
        r"(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]{20,}|"
        r"gh[pousr]_[A-Za-z0-9_*_]{20,})(?![A-Za-z0-9_])"
    ),
    "gitlab_token": re.compile(
        r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
    ),
    "npm_token": re.compile(
        r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{24,}(?![A-Za-z0-9_])"
    ),
    "pypi_token": re.compile(
        r"(?<![A-Za-z0-9_-])pypi-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    ),
    "bearer_token": re.compile(
        r"\bbearer\s+[A-Za-z0-9._\-~+/=*]{16,}",
        re.IGNORECASE,
    ),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9.*_-]{16}\b"),
    "aws_secret_access_key": re.compile(
        r"\baws_secret_access_key\s*(?:=|:)\s*[\"']?[A-Za-z0-9/+=]{32,}",
        re.IGNORECASE,
    ),
    "cookie_header": re.compile(
        r"^\s*(?:cookie|set-cookie)\s*:\s*[^\r\n]{8,}$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "telegram_bot_token": re.compile(
        r"(?<!\d)\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"
    ),
    "discord_token": re.compile(
        r"(?<![A-Za-z0-9_-])(?:mfa\.[A-Za-z0-9_-]{60,}|"
        r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\."
        r"[A-Za-z0-9_-]{25,40})(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    ),
    "slack_token": re.compile(
        r"\bxox[abprs]-[A-Za-z0-9.*_-]{8,}\b",
        re.IGNORECASE,
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9._-]{8,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "stripe_live_key": re.compile(
        r"\b(?:sk|rk)_live_[A-Za-z0-9_*.-]{16,}\b",
        re.IGNORECASE,
    ),
}


COMMON_SECRET_PATTERN_VALUES: tuple[re.Pattern[str], ...] = tuple(
    COMMON_SECRET_PATTERNS.values()
)
