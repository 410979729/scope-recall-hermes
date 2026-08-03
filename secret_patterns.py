"""Canonical secret scanning primitives shared by every trust boundary.

This module owns Unicode shadow normalization, credential-value patterns,
assignment classification, and sensitive mapping-key classification.  Callers
may still apply context policy (for example release-fixture exemptions), but
must not maintain independent secret regex or Unicode normalization paths.

Match offsets refer to the normalized scan shadow.  They are suitable for line
reporting and classification, not for slicing or redacting the original text.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any


PEM_PRIVATE_KEY_BEGIN_RE = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9-]+[ ]+)*PRIVATE KEY(?:[ ]+BLOCK)?)-----",
    re.IGNORECASE,
)

COMMON_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "pem_private_key_block": re.compile(
        r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?"
        r"-----END [A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?-----",
        re.IGNORECASE,
    ),
    # A dangling BEGIN marker is sensitive too: truncated key blocks must fail
    # closed even when the corresponding END marker is missing.
    "pem_private_key_begin": PEM_PRIVATE_KEY_BEGIN_RE,
    "database_uri_with_password": re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis(?:s)?|"
        r"amqp(?:s)?|mssql)://[^/\s:@]*:[^@\s/]+@[^\s]+",
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
        r"\bbearer(?:\s+|\s*[:=]\s*)[A-Za-z0-9._\-~+/=*]{16,}",
        re.IGNORECASE,
    ),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9.*_-]{16}\b"),
    "aws_secret_access_key": re.compile(
        r"\baws_secret_access_key\s*(?:=|:)\s*[\"']?[A-Za-z0-9/+=]{32,}",
        re.IGNORECASE,
    ),
    "cookie_header": re.compile(
        r"^\s*(?:cookie|set-cookie)\s*:\s*[^=\n;,\s]+=[^\n]+$",
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

SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:api[_ \t-]?key|secret|password|passwd|"
    r"credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)[\"']?"
    r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
    re.IGNORECASE,
)

TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?P<key>(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_-]*[_-])?token)[\"']?"
    r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
    re.IGNORECASE,
)

SENSITIVE_MAPPING_KEY_RE = re.compile(
    r"(?:"
    r"(?:^|[_\-\s])(?:authorization|api[_\-\s]?key|access[_\-\s]?token|"
    r"refresh[_\-\s]?token|password|passwd|private[_\-\s]?key|"
    r"client[_\-\s]?secret|cookie)(?:$|[_\-\s:=])"
    r"|(?:^|[_\-\s])token(?:$|[\s:=])"
    r")",
    re.IGNORECASE,
)

SENSITIVE_KEY_COMPONENT_RE = re.compile(
    r"(?:^|_)(?:authorization|auth|bearer|cookie|credential|credentials|"
    r"password|passwd|secret|token|api_key|private_key|client_secret)(?:_|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SecretTextMatch:
    """One named match located in the normalized secret-scan shadow."""

    name: str
    start: int
    end: int
    text: str


def secret_scan_shadow(value: Any) -> str:
    """Return an NFKC scan view with invisible format controls removed."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )


def normalize_secret_mapping_key(value: Any) -> str:
    """Normalize case and separators for secret mapping-key classification."""

    return re.sub(
        r"[-\s]+",
        "_",
        secret_scan_shadow(value).strip().casefold(),
    )


def is_safe_token_metric_key(value: Any) -> bool:
    """Return whether a token-suffixed key is benign telemetry, not a credential."""

    normalized = normalize_secret_mapping_key(value)
    if normalized == "per_token":
        return True
    suffix = "_per_token"
    if not normalized.endswith(suffix):
        return False
    metric_prefix = normalized[: -len(suffix)]
    return not bool(
        SENSITIVE_MAPPING_KEY_RE.search(metric_prefix)
        or SENSITIVE_KEY_COMPONENT_RE.search(metric_prefix)
    )


def is_sensitive_mapping_key(value: Any) -> bool:
    """Classify credential keys while preserving benign token metrics."""

    if is_safe_token_metric_key(value):
        return False
    shadow = secret_scan_shadow(value)
    normalized = normalize_secret_mapping_key(shadow)
    if SENSITIVE_MAPPING_KEY_RE.search(shadow) or SENSITIVE_MAPPING_KEY_RE.search(
        normalized
    ):
        return True
    if normalized == "token" or normalized.endswith("_token"):
        return True
    suffix = "_per_token"
    if normalized.endswith(suffix):
        metric_prefix = normalized[: -len(suffix)]
        return bool(SENSITIVE_KEY_COMPONENT_RE.search(metric_prefix))
    return False


def scan_secret_like_text(value: Any) -> tuple[SecretTextMatch, ...]:
    """Return all canonical secret-like matches in one normalized scan view."""

    shadow = secret_scan_shadow(value)
    candidates: list[SecretTextMatch] = []
    patterns = (("api_key_assignment", SECRET_ASSIGNMENT_RE), *COMMON_SECRET_PATTERNS.items())
    for name, pattern in patterns:
        candidates.extend(
            SecretTextMatch(name, match.start(), match.end(), match.group(0))
            for match in pattern.finditer(shadow)
        )
    candidates.extend(
        SecretTextMatch(
            "token_assignment",
            match.start(),
            match.end(),
            match.group(0),
        )
        for match in TOKEN_ASSIGNMENT_RE.finditer(shadow)
        if not is_safe_token_metric_key(match.group("key"))
    )
    # Keep ordering deterministic and collapse exact duplicate classifier output.
    unique: dict[tuple[str, int, int], SecretTextMatch] = {}
    for match in candidates:
        unique.setdefault((match.name, match.start, match.end), match)
    return tuple(sorted(unique.values(), key=lambda item: (item.start, item.end, item.name)))


def contains_secret_like_text(value: Any) -> bool:
    """Return whether the canonical scan API found any secret-like material."""

    return bool(scan_secret_like_text(value))
