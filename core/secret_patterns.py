"""Canonical secret scanning primitives shared by every trust boundary.

This module owns Unicode shadow normalization, credential-value patterns,
assignment classification, and sensitive mapping-key classification.  Callers
may still apply context policy (for example release-fixture exemptions), but
must not maintain independent secret regex or Unicode normalization paths.

Match offsets refer to the normalized scan shadow.  They are suitable for line
reporting and classification, not for slicing or redacting the original text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: The label's words are bounded: ``(?:[A-Z0-9-]+[ ]+)*`` backtracked through every way to split a run of
#: ``-----BEGIN `` repeats, 14 s for 100 kB.  Real labels have a few short words ("OPENSSH", "ENCRYPTED").
PEM_PRIVATE_KEY_BEGIN_RE = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9-]{1,32}[ ]{1,4}){0,8}PRIVATE KEY(?:[ ]{1,4}BLOCK)?)-----",
    re.IGNORECASE,
)

COMMON_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    # The BEGIN marker alone decides, dangling or not: truncated key blocks must fail closed even when the END
    # marker is missing.  A whole-block pattern added nothing to that and scanned from every BEGIN to the end of
    # the text looking for its END (quadratic in repeated markers); ``capture_filters`` redacts the block itself.
    "pem_private_key_begin": PEM_PRIVATE_KEY_BEGIN_RE,
    "database_uri_with_password": re.compile(
        r"(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis(?:s)?|"
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
    "gitlab_token": re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    "npm_token": re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{24,}(?![A-Za-z0-9_])"),
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
    # Spacing within the line only: ``\s*`` ran on across every following blank line from each line start,
    # 4.3 s for 20 kB of blank lines.
    "cookie_header": re.compile(
        r"^[ \t]*(?:cookie|set-cookie)[ \t]*:[ \t]*[^=\n;,\s]+=[^\n]+$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # A bot's id stands alone, or follows ``bot`` in an API URL (``/bot<id>:<secret>/getMe``).  A digit
    # run glued to other letters is part of something else: a Codex source key ends its hex installation
    # id in eight digits about once in 45 installations and runs on into a session UUID
    # (``...00de02721985:92051813-c57b-...``), which read as a token and refused every capture of that
    # installation as a secret.
    "telegram_bot_token": re.compile(
        r"(?:(?<![A-Za-z0-9_-])|(?<=[Bb][Oo][Tt]))\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"
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

#: What follows a credential word without being a credential, kept narrow on purpose: a password can be any
#: word in any script, so only what cannot be one is let through.  Every message that said
#: ``def login(user: str, password: str)``, ``api_key: <your-api-key>`` or "the password is required" was refused
#: as a secret and never stored, and a model request carrying one was refused as ``sensitive_request``.
#: A value runs to its first space or the end of the text.  Not a value when it is all of that, in matching quotes
#: or none, with closing punctuation only after it (an exemption that stopped earlier let through what followed
#: it: ``<b>Xk9#mP2q</b>``, ``Changed!2024``, ``My.Secret.Pass#99``):
#: - a placeholder: ``<your-key>``, ``${...}``, ``$UPPER_NAME``, ``%NAME%``, ``{name}``, ``[REDACTED...]``, the
#:   bracketed names without digits (``<hunter2>`` and ``{hunter2}`` count);
#: - a mask (``***``, ``xxxx``), or an opening brace or bracket alone (``"credentials": {``);
#: - a type or a null in code (``str``, ``Optional[str]``, ``None``), or a word that says what the value is
#:   (``required``, ``missing``, ``reset``, ``see``);
#: - a dotted name from a code root such as ``os``, ``settings``, ``self`` or ``process`` (``settings.DB_PASSWORD``).
#: Not a value by how it begins, whatever follows:
#: - a call or subscript on a dotted name (``os.environ["KEY"]``, ``Yup.string()``), or a call on a lower-case
#:   name with no argument or a quoted one (``getpass()``, ``input("Password: ")``);
#: - punctuation alone up to a space or a line's end (``""``, ``")``, ``=`` in ``token == nil``);
#: - a Chinese question or description (``token是什么意思``).
#: ``$unshine2024``, ``[hunter2]``, ``letmein(2024)``, "correct horse battery staple" and a value in any script
#: still count.  Known gaps, credentials that pass: what follows a value's first space (``password: str Xk9#mP2q``,
#: ``DB_PASSWORD: str = "..."``, ``settings.DB_PASSWORD or "..."``, ``password: { value: ... }``), what follows an
#: exemption judged by how it begins (``password: Mr.Smith(1985)``, ``love()you``, a YAML ``|`` block), and a
#: value of punctuation alone (``!@#$%^&*``).
#: The quantifiers that meet another run of the same characters are possessive (``*+``, ``++``, ``{3,}+``): greedy,
#: a run of dots after a key word was given back one dot at a time and the end tried again at each, 2.9 s for
#: 20 kB and 7.5 s with 64 key starts.  Taking a run whole never changes a verdict: a shorter run is followed by
#: another of its characters, where a space or the end of the text is needed.
_END = r"""(?=[,;:.)\]}>"'`|]*+(?:\s|$))"""
_EXEMPT_IN_QUOTES = (
    r"<[A-Za-z][A-Za-z _.-]{0,79}>"
    r"|\$\{[^{}\s]{1,80}\}"
    r"|(?-i:\$[A-Z][A-Z0-9_]*)"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\{\{?[A-Za-z_][A-Za-z_.]*\}\}?"
    r"|\[(?:redacted|hidden|masked|omitted|removed)[^\]\s]*\]"
    r"|[*\u2022\u00b7xX._-]{3,}+"
    r"|[{\[(]"
    r"|(?:str|string|bytes|int|bool|float|none|null|nil|undefined|optional|any|secretstr|dict|list|object|true|"
    r"false)(?:\[[^\]\s]{0,80}\])?"
    r"|(?:required|missing|empty|unset|invalid|incorrect|wrong|expired|reset|changed|hidden|masked|redacted|"
    r"omitted|removed|see|tbd|todo|n/a)"
    r"|(?:os|sys|env|settings|config|conf|cfg|process|request|req|self|this|app|ctx|context|options|opts|args|"
    r"params|props|secrets|vault|keyring|form|body|data|values|state)(?:\.[A-Za-z_]+)+"
)
_NOT_A_VALUE = (
    r"(?!"
    r"(?P<vq>[\"'`]?)(?:"
    + _EXEMPT_IN_QUOTES
    + r")(?P=vq)"
    + _END
    + r"|(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*[(\[]"
    r"|(?-i:[a-z_][a-z0-9_]*)\((?:\)|[\"'])"
    r"|[^\s\w]++(?:\s|$)"
    r"|(?:什么|多少|哪个|哪些|啥|怎么|怎样|如何|不是|是否|必须|必需|必填|可选|过期|无效|有效)"
    r")"
)
#: Prose: after "is", one of the lower-case words that describe a value rather than give it ("the password is
#: required", "the secret is out", "the token is sent in the header").  "my password is iloveyou", "the password
#: is now Sunflower2024" and "the password is Strong!2024" still read as one.
_IS_NOT_A_WORD = (
    r"(?!(?-i:(?:required|optional|missing|empty|set|unset|invalid|incorrect|wrong|right|correct|valid|expired|"
    r"revoked|changed|reset|stored|saved|hashed|encrypted|encoded|hidden|masked|redacted|needed|sent|used|"
    r"generated|created|issued|refreshed|rotated|returned|passed|included|attached|shown|printed|logged|signed|"
    r"verified|checked|validated|accepted|rejected|denied|blocked|disabled|enabled|not|none|null|true|false|the|"
    r"a|an|in|on|at|for|out|ok|fine|weak|strong|long|short|same|different|what|where|that|this|it|here|there))"
    + _END
    + r")"
)
_SEPARATOR = r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+" + _IS_NOT_A_WORD + r")"

#: ``credential``'s suffix is bounded: unbounded, a run of "credential" repeats was tried at every length from
#: every repeat, 1 s for 20 kB.
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:api[_ \t-]?key|secret|password|passwd|"
    r"credential(?:[_ \t-]?[a-z0-9_]{1,64})?|private[_ \t-]?key)[\"']?" + _SEPARATOR + _NOT_A_VALUE + r"[^\s]+",
    re.IGNORECASE,
)

#: The name before ``token`` is at most 128 characters: unbounded, a long hyphenated line (a generated id, a
#: kebab-case slug) was tried from every hyphen to its end, 18 s for 60,000 characters.
TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?P<key>(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_-]{0,126}[_-])?token)[\"']?"
    + _SEPARATOR
    + _NOT_A_VALUE
    + r"[^\s]+",
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


#: From the first backslash of a run only: from every one, a long run with no break after it was tried to its end
#: again and again, 1.4 s for 20 kB of backslashes.
_ESCAPED_BREAK_RE = re.compile(r"(?<!\\)\\+([nrt])")
_ESCAPED_BREAKS = {"n": "\n", "r": "\r", "t": "\t"}


def secret_scan_shadow(value: Any) -> str:
    """Return an NFKC scan view with invisible format controls removed.

    A serialised line break (the two characters ``\\n``) counts as the break it
    stands for.  Serialised into a model request, a document template with an
    empty credential slot ("AppSecret:" and nothing after it) had the next line
    swallowed as its value and would be refused as ``sensitive_request`` (369
    candidate evaluations in one store, none holding a secret).  Text that
    was serialised more than once (a tool output that is itself JSON holding
    JSON) writes the same break as ``\\\\n``; treating only the last two
    characters as the break left a backslash after the slot, which then read
    as its value, so the whole run of backslashes belongs to the break.  The
    substitution keeps the length, so every match position stays valid.
    """

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = _ESCAPED_BREAK_RE.sub(lambda match: _ESCAPED_BREAKS[match.group(1)] * len(match.group(0)), normalized)
    return "".join(character for character in normalized if unicodedata.category(character) != "Cf")


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
    return not bool(SENSITIVE_MAPPING_KEY_RE.search(metric_prefix) or SENSITIVE_KEY_COMPONENT_RE.search(metric_prefix))


def is_sensitive_mapping_key(value: Any) -> bool:
    """Classify credential keys while preserving benign token metrics."""

    if is_safe_token_metric_key(value):
        return False
    shadow = secret_scan_shadow(value)
    normalized = normalize_secret_mapping_key(shadow)
    if SENSITIVE_MAPPING_KEY_RE.search(shadow) or SENSITIVE_MAPPING_KEY_RE.search(normalized):
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
            SecretTextMatch(name, match.start(), match.end(), match.group(0)) for match in pattern.finditer(shadow)
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
