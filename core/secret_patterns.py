"""Canonical secret scanning primitives shared by every trust boundary.

This module owns Unicode shadow normalization, credential-value patterns,
assignment classification, and sensitive mapping-key classification.  Callers
may still apply context policy (for example release-fixture exemptions), but
must not maintain independent secret regex or Unicode normalization paths.

Match offsets refer to the normalized scan shadow.  They are suitable for line
reporting and classification, not for slicing or redacting the original text.
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from collections.abc import Iterator
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
    # Spacing within the line only: ``\s*`` would run on across every following blank line from each line start,
    # 4.3 s for 20 kB of blank lines.
    "cookie_header": re.compile(
        r"^[ \t]*(?:cookie|set-cookie)[ \t]*:[ \t]*[^=\n;,\s]+=[^\n]+$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # A bot's id stands alone, or follows ``bot`` in an API URL (``/bot<id>:<secret>/getMe``).  A digit
    # run glued to other letters is part of something else: a Codex source key ends its hex installation
    # id in eight digits about once in 45 installations and runs on into a session UUID
    # (``...00de02721985:92051813-c57b-...``), which would read as a token and refuse every capture of that
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
#: word in any script, so only what cannot be one is let through.  Without these exemptions every message that
#: says ``def login(user: str, password: str)``, ``api_key: <your-api-key>`` or "the password is required" would
#: be refused as a secret and never stored, and a model request carrying one refused as ``sensitive_request``.
#: A value runs to its first space or the end of the text.  Not a value when it is all of that, in matching quotes
#: or none, with closing punctuation only after it (an exemption that stops earlier would let through what
#: follows it: ``<b>Xk9#mP2q</b>``, ``Changed!2024``, ``My.Secret.Pass#99``):
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
#: - a Chinese question or description (``token是什么意思``, ``密码已重置``).
#: Chinese, camelCase and Markdown labels share the ASCII value rule: at least four characters with a digit or inner
#: symbol, or eight letters after a password label. Trailing punctuation, structural punctuation right after the
#: label and identifier references do not count. Unquoted values end at whitespace or non-ASCII text; quoted values
#: end at their escape-aware closing quote. Plain English keeps word values; after 是 or 为 no label takes Chinese text.
#: Plain English labels count ``$unshine2024``, ``[hunter2]``, ``letmein(2024)``, "correct horse battery staple" and
#: values in any script. Known gaps, credentials that pass: what follows a value's first space
#: (``password: str Xk9#mP2q``,
#: ``DB_PASSWORD: str = "..."``, ``settings.DB_PASSWORD or "..."``, ``password: { value: ... }``), what follows an
#: exemption judged by how it begins (``password: Mr.Smith(1985)``, ``love()you``, a YAML ``|`` block), and a
#: value of punctuation alone (``!@#$%^&*``), and passwords written in Chinese characters after Chinese labels.
#: The quantifiers that meet another run of the same characters are possessive (``*+``, ``++``, ``{3,}+``): greedy,
#: a run of dots after a key word would be given back one dot at a time and the end tried again at each, 2.9 s for
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
    r"(?P<vq>[\"'`])(?:"
    + _EXEMPT_IN_QUOTES
    + r")(?P=vq)"
    + r"|(?:"
    + _EXEMPT_IN_QUOTES
    + r")"
    + _END
    + r"|[\"'`]?\[(?:redacted|hidden|masked|omitted|removed)[^\]\s]*\]"
    + "(?:"
    + _END
    + r"|(?=[^ -~]))"
    + r"|(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*[(\[]"
    r"|(?-i:[a-z_][a-z0-9_]*)\((?:\)|[\"'])"
    r"|[^\s\w]++(?:\s|$)"
    r"|(?:什么|多少|哪个|哪些|啥|怎么|怎样|如何|不是|是否|必须|必需|必填|可选|过期|无效|有效)"
    r"|(?:错误(?:的)?|已重置|为空|空)" + _END + r")"
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

#: Bounded Markdown runs cannot rescan the rest of a long emphasis line at each credential word.
#: Spacing is possessive and horizontal only: an empty slot never takes the following line as its value.
_MARKS = r"[\"'`*_]{0,8}+"
_VALUE = (
    r"(?P<quote>[\"'`])?+"
    r"(?P<value>(?(quote)(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])*+|[^\s]++))"
)
_FORMATTED_SEPARATOR = (
    r"(?P<separator>"
    + _MARKS
    + r"[ \t]*+"
    + _MARKS
    + r"(?:[:=是为]"
    + r"[*_]{0,8}+"
    + r"[ \t]*+|is[ \t]++"
    + _IS_NOT_A_WORD
    + r"))"
)
_CAMEL_CREDENTIAL = (
    r"(?<![A-Za-z0-9_])(?:accessToken|refreshToken|idToken|authToken|sessionToken|apiToken|botToken|"
    r"bearerToken|oauthToken|clientSecret|apiSecret|appSecret|secretKey|accessKey)(?![A-Za-z0-9_])"
)

#: ``credential``'s suffix is bounded: unbounded, a run of "credential" repeats would be tried at every length from
#: every repeat, 1 s for 20 kB.
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:api[_ \t-]?key|secret|password|passwd|"
    r"credential(?:[_ \t-]?[a-z0-9_]{1,64})?|private[_ \t-]?key)" + _FORMATTED_SEPARATOR + _NOT_A_VALUE + _VALUE,
    re.IGNORECASE,
)

CHINESE_ASSIGNMENT_RE = re.compile(
    r"(?:密码|口令|密钥|私钥|令牌|授权码)"
    # A label closed by a bracket keeps its separator: "（开发者密钥）：9a8b7c6d5e".
    + r"(?>[)\]]?"
    + _MARKS
    + r"[ \t]*+"
    + _MARKS
    + r"[:=是为]"
    + r"[ \t]*+|"
    + _MARKS
    + r"[ \t]++|(?=[!-~]))[*_]{0,8}+[ \t]*+"
    + _NOT_A_VALUE
    + _VALUE.replace(r"[^\s]++", r"[!-~]++"),
    re.IGNORECASE,
)

CAMEL_ASSIGNMENT_RE = re.compile(
    _CAMEL_CREDENTIAL + _FORMATTED_SEPARATOR + _NOT_A_VALUE + _VALUE.replace(r"[^\s]++", r"[!-~]++"),
    re.IGNORECASE,
)

#: The name before ``token`` is at most 128 characters: unbounded, a long hyphenated line (a generated id, a
#: kebab-case slug) would be tried from every hyphen to its end, 18 s for 60,000 characters.
TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?P<key>(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_-]{0,126}[_-])?token)"
    + _FORMATTED_SEPARATOR
    + _NOT_A_VALUE
    + _VALUE,
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
    value_start: int | None = None


#: From the first backslash of a run only: from every one, a long run with no break after it would be tried to its
#: end again and again, 1.4 s for 20 kB of backslashes.
_ESCAPED_BREAK_RE = re.compile(r"(?<!\\)\\+([nrt])")
_ESCAPED_BREAKS = {"n": "\n", "r": "\r", "t": "\t"}


def secret_scan_shadow(value: Any) -> str:
    """Return an NFKC scan view with invisible format controls removed.

    A serialised line break (the two characters ``\\n``) counts as the break it
    stands for.  Serialised into a model request, a document template with an
    empty credential slot ("AppSecret:" and nothing after it) would have the next
    line swallowed as its value and be refused as ``sensitive_request`` (369
    candidate evaluations in one store, none holding a secret).  Text that
    was serialised more than once (a tool output that is itself JSON holding
    JSON) writes the same break as ``\\\\n``; treating only the last two
    characters as the break would leave a backslash after the slot to be read
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
    if re.fullmatch(_CAMEL_CREDENTIAL + r"|密码|口令|密钥|私钥|令牌|授权码", shadow, re.IGNORECASE):
        return True
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
    patterns = (
        ("api_key_assignment", SECRET_ASSIGNMENT_RE),
        ("chinese_credential_assignment", CHINESE_ASSIGNMENT_RE),
        ("camel_credential_assignment", CAMEL_ASSIGNMENT_RE),
        *COMMON_SECRET_PATTERNS.items(),
    )
    for name, pattern in patterns:
        candidates.extend(
            SecretTextMatch(
                name,
                match.start(),
                match.end(),
                match.group(0),
                match.start("value") if "value" in pattern.groupindex else None,
            )
            for match in _accepted_matches(pattern, shadow)
        )
    candidates.extend(
        SecretTextMatch(
            "token_assignment",
            match.start(),
            match.end(),
            match.group(0),
            match.start("value"),
        )
        for match in _accepted_matches(TOKEN_ASSIGNMENT_RE, shadow)
    )
    # Keep ordering deterministic and collapse exact duplicate classifier output.
    unique: dict[tuple[str, int, int], SecretTextMatch] = {}
    for match in candidates:
        unique.setdefault((match.name, match.start, match.end), match)
    return tuple(sorted(unique.values(), key=lambda item: (item.start, item.end, item.name)))


#: Symbols a password may begin with; any other punctuation right after a label is the surrounding text's.
_LEADING_VALUE_SYMBOLS = frozenset("!@#$%^&*+=~?")
#: Labels whose values may be words: an e-mail authorization code (授权码) is often sixteen lower-case letters.
_PASSWORD_LABEL = re.compile(r"pass(?:word|wd)|密码|口令|授权码", re.IGNORECASE)
_CJK_START = re.compile(r"[㐀-鿿豈-﫿]")
#: What a non-empty container stands for after its credential key (``_after_key``): four digits meet the ASCII value
#: length rule and cannot name a placeholder.
_CONTAINER_WITNESSES = {"dict": "{0000}", "list": "[0000]"}


def _assignment_value_allowed(match: re.Match[str]) -> bool:
    """Judge ASCII values consistently across Chinese, camelCase and Markdown labels."""
    if "value" not in match.re.groupindex:
        return True
    # Camel labels must never also inherit the less restrictive English suffix rule.
    if (
        match.re is SECRET_ASSIGNMENT_RE
        and match.start()
        and re.search(
            _CAMEL_CREDENTIAL + "$", match.string[max(0, match.start() - 16) : match.start("separator")], re.IGNORECASE
        )
    ):
        return False
    separator = match.groupdict().get("separator") or ""
    label = match.string[match.start() : match.start("value")]
    password_label = _PASSWORD_LABEL.search(label) is not None
    # After 是 or 为, Chinese text describes a key or token rather than giving it ("TOKEN 为保存凭据名的字段"); a
    # password may still be a Chinese passphrase.
    if ("是" in separator or "为" in separator) and not password_label and _CJK_START.match(match["value"]):
        return False
    strict = match.re in (CHINESE_ASSIGNMENT_RE, CAMEL_ASSIGNMENT_RE) or any(mark in separator for mark in "`*_")
    return not strict or _ascii_value_allowed(match, password_label=password_label)


def _ascii_value_allowed(match: re.Match[str], *, password_label: bool) -> bool:
    """The value rule Chinese, camelCase and Markdown labels share: an ASCII run judged by its shape."""
    prefix = re.match(r"[ -~]*" if match["quote"] else r"[!-~]*", match["value"])
    assert prefix is not None  # Both ASCII prefix patterns accept an empty run.
    if prefix[0] in _CONTAINER_WITNESSES.values():
        return True
    value = prefix[0].rstrip(string.punctuation)
    # Punctuation that structures the text right after a label (",", ")", "]", "/", "|", a quote opening a link, a
    # path or JSON) belongs to that text.  A password may still begin with one of the symbols people put in them.
    if value[:1] and not value[0].isalnum() and value[0] not in _LEADING_VALUE_SYMBOLS:
        return False
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value, re.ASCII):
        return False
    if not any(character.isdigit() for character in value) and (
        re.fullmatch(r"[A-Za-z_]*_[A-Za-z_]*", value)
        or re.fullmatch(r"(?:[A-Za-z]*[a-z][A-Z]|[A-Z]{2,}[a-z])[A-Za-z]*", value)
    ):
        return False
    if value.isascii() and value.isalpha():
        # A word alone is a password only where a password is asked for: after "Credentials:" or "token is" a word
        # describes something ("Encrypted", "obtained").
        return len(value) >= 8 and password_label
    return len(value) >= 4 and (
        any(character.isdigit() for character in value) or any(not character.isalnum() for character in value[1:-1])
    )


def _accepted_matches(pattern: re.Pattern[str], shadow: str) -> Iterator[re.Match[str]]:
    """Yield accepted runs while keeping a rejected assignment's value searchable."""
    position = 0
    while position <= len(shadow):
        match = pattern.search(shadow, position)
        if match is None:
            return
        accepted = _assignment_value_allowed(match)
        if pattern is TOKEN_ASSIGNMENT_RE:
            accepted = accepted and not is_safe_token_metric_key(match["key"])
        if accepted:
            yield match
        # Skip rejected heads so a telemetry key's suffix stays part of that key, but inspect their values.
        position = max(match.start() + 1, match.end() if accepted else match.start("value"))


def contains_secret_like_text(value: Any) -> bool:
    """Return whether the canonical scan API found any secret-like material."""

    return bool(scan_secret_like_text(value))


def secret_prefilter_sql(expression: str) -> tuple[str, tuple[str, ...]]:
    """Build a cheap SQL candidate test; the canonical scan alone decides matches.

    The expression is a caller-owned, lowercased SQL column, never user input.
    Text outside ASCII, ordinary CJK and their punctuation is also admitted for
    normalization; compatibility characters and invisible controls cannot hide
    a credential from this prefilter. Ordinary Chinese prose uses the word test.
    """
    words = (
        "password",
        "passwd",
        "secret",
        "credential",
        "key",
        "token",
        "bearer",
        "cookie",
        "://",
        "：//",
        "sk-",
        "gh",
        "glpat-",
        "npm_",
        "pypi-",
        "akia",
        "asia",
        "mfa.",
        "xox",
        "eyj",
        "aiza",
        "_live_",
        "密码",
        "口令",
        "密钥",
        "私钥",
        "令牌",
        "授权码",
        "\\u",
    )
    # A full-width colon can frame a URI or bot token without any credential word.
    return (
        "("
        + " OR ".join(f"instr({expression},?)>0" for _ in words)
        + f" OR {expression} GLOB '*[^ -~\t\n\r\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f，。！？：；、（）【】]*'"
        + f" OR {expression} GLOB '*{'[0-9]' * 8}[:：]*' OR {expression} GLOB '*.*.*')",
        words,
    )


def secret_redaction_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Map secret values back to original characters, retaining labels and formatting.

    Per-character NFKC covers width changes, ligatures and invisible controls. If composition across characters
    differs from the canonical shadow, the whole text is withheld rather than trusting approximate offsets.
    Escaped breaks preserve length and therefore preserve this map.
    """
    matches = scan_secret_like_text(text)
    if not matches:
        return ()
    # A normalized PEM marker can evade original-text block framing; its body must never remain readable.
    if any(match.name == "pem_private_key_begin" for match in matches):
        return ((0, len(text)),)
    if secret_scan_shadow(text) == text:
        return tuple((m.value_start if m.value_start is not None else m.start, m.end) for m in matches)
    characters, positions = [], []
    for index, character in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", character):
            if unicodedata.category(normalized) != "Cf":
                characters.append(normalized)
                positions.append(index)
    projected = _ESCAPED_BREAK_RE.sub(
        lambda match: _ESCAPED_BREAKS[match.group(1)] * len(match.group(0)), "".join(characters)
    )
    if projected != secret_scan_shadow(text):
        return ((0, len(text)),)
    return tuple(
        (positions[m.value_start if m.value_start is not None else m.start], positions[m.end - 1] + 1) for m in matches
    )


def secret_like_text_starts_before(value: Any, end: int) -> bool:
    """Whether a secret-like run of ``value`` starts in its first ``end`` characters, each run judged as written.

    For a caller that hands on only those characters: a run the cut falls in is judged with the rest of its line,
    not as the cut leaves it.  Cut alone, the start of an exempt value reads as a password (``credential_env: nu`` of
    ``null``), and the first characters of a key can be too few for its pattern.  No pattern's match, nor what
    decides it, reaches past a line break, except the spacing after ``bearer`` or an AWS secret key's name, and a
    value it carries onto a line after the cut's is not sent; so those lines are not scanned, which spares reading a
    long text whole.  Positions compare in the scan shadow: the shadow of the first ``end`` characters is as long as the
    part of the whole shadow they become when ``value`` is in NFKC form, since a prefix of NFKC text is NFKC;
    embedding input is cut in that form.
    """

    text = str(value or "")
    line_end = text.find("\n", end)
    if line_end >= 0:
        text = text[: line_end + 1]
    boundary = len(secret_scan_shadow(text[:end]))
    return any(match.start < boundary for match in scan_secret_like_text(text))


def _after_key(item: Any) -> str:
    """``item`` as JSON writes it after its key, a container as one holding something whose texts are read on their
    own (``_CONTAINER_WITNESSES``)."""
    if isinstance(item, dict):
        return _CONTAINER_WITNESSES["dict"] if item else "{}"
    if isinstance(item, (list, tuple)):
        return _CONTAINER_WITNESSES["list"] if item else "[]"
    if item is None or isinstance(item, (str, bool, int, float)):
        return json.dumps(item, ensure_ascii=False)
    return str(item)


def _names_a_credential(key: Any) -> bool:
    """Classify assignment keys with a witness of at least four ASCII characters including a digit."""
    return contains_secret_like_text(f"{key}: TEST9")


def structure_contains_secret_like_text(value: Any) -> bool:
    """Whether a JSON-like value carries secret-like text, each of its texts read as written.

    For a structure that crosses a boundary as JSON (a model request's sources, a model's result).  Read through that
    encoding, a text's end is followed by the next field where the scan expects a space or the end of the text, and
    its quotes are escaped: ``api_key: null`` ending a text and ``api_key: "<your-api-key>"`` anywhere in one read as
    credentials.  Each text and each key is read alone.  A member under a key that names a credential is also read as
    JSON writes it, ``key: value`` (``_after_key``), so a credential held there is still one whatever holds it
    (``{"password": 123456}``, ``{"password": ["hunter2"]}``, ``{"password": "null hunter2"}``).  The walk keeps its own
    stack, so nesting depth costs no recursion.
    """

    return any(contains_secret_like_text(text) for text in _structure_texts(value))


def secret_structure_pattern_names(value: Any) -> tuple[str, ...]:
    """Return canonical rule names for structured content, never values or matched text."""
    return tuple(sorted({match.name for text in _structure_texts(value) for match in scan_secret_like_text(text)}))


def _structure_texts(value: Any):
    """Yield each text as written and each credential member as its key assigns it."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            if "predicate" in item and "value_text" in item:
                pending.append({str(item["predicate"]): item["value_text"]})
            for key, member in item.items():
                yield key
                if _names_a_credential(key):
                    yield f"{key}: {_after_key(member)}"
                pending.append(member)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


#: How far ``cut_between_runs`` moves a cut, and a quarter of how much text it reads on each side of one.  A key, a
#: placeholder or a word is shorter than this, and on a long line the reading stays bounded.
CUT_REACH = 200


def _runs(text: str, shift: int = 0) -> set[tuple[str, int, int]]:
    """The secret-like runs of ``text`` (``scan_secret_like_text``), their positions moved by ``shift``."""
    return {(match.name, match.start + shift, match.end + shift) for match in scan_secret_like_text(text)}


def cut_between_runs(text: str, cut: int, *, toward: int, reach: int = CUT_REACH) -> int:
    """Where to cut ``text`` near ``cut`` so that neither side holds a secret-like run the uncut text does not.

    The nearest position just after a whitespace character, from ``cut`` toward ``toward`` and at most ``reach``
    away, where every run either side holds is a run of the uncut text, at the same place.  Cut inside a value the
    scan exempts only whole, a side would carry its first letters (``credential_env: nu`` of ``null``,
    ``api_key: <your api`` of ``<your api key>``), or the end of a key's name (``token:`` of ``mytoken:``), and read as
    a credential.  The sides and the uncut text are read on the cut's line, at most ``4 * reach`` to each side of it;
    a run that bound cuts into lies on a side and in the uncut text alike, so it neither blocks a cut nor excuses one.
    ``cut`` itself when no such position is within reach, as on a run of characters with no space in it.
    """

    span = 4 * reach
    step = 1 if toward > cut else -1
    stop = (min(cut + reach, toward) if step > 0 else max(cut - reach, toward)) + step
    for position in range(cut, stop, step):
        if position <= 0 or position >= len(text):
            return position
        if not text[position - 1].isspace():
            continue
        low, high = max(0, position - span), min(len(text), position + span)
        newline = text.rfind("\n", low, position)
        start = newline + 1 if newline >= 0 else low
        newline = text.find("\n", position, high)
        end = newline if newline >= 0 else high
        left = _runs(text[start:position])
        right = _runs(text[position:end], len(secret_scan_shadow(text[start:position])))
        if left | right <= _runs(text[start:end]):
            return position
    return cut
