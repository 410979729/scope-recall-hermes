"""Canonical secret scanning primitives shared by every trust boundary.

This module owns Unicode shadow normalization, credential-value patterns,
assignment classification, and sensitive mapping-key classification.  Callers
may still apply context policy (for example release-fixture exemptions), but
must not maintain independent secret regex or Unicode normalization paths.

Match offsets refer to the normalized scan shadow.  They are suitable for line
reporting and classification, not for slicing or redacting the original text.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import unicodedata
from typing import Any


#: The label's words are bounded: ``(?:[A-Z0-9-]+[ ]+)*`` backtracked through every way to split a run of
#: ``-----BEGIN `` repeats, 14 s for 100 kB.  Real labels have a few short words ("OPENSSH", "ENCRYPTED").
#: The spacing after BEGIN is bounded the same way; with exactly one space, ``-----BEGIN  PRIVATE KEY-----``
#: passed, which 3.3.0 refused.
PEM_PRIVATE_KEY_BEGIN_RE = re.compile(
    r"-----BEGIN[ ]{1,4}(?P<label>(?:[A-Z0-9-]{1,32}[ ]{1,4}){0,8}PRIVATE KEY(?:[ ]{1,4}BLOCK)?)-----",
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
    # Spacing within the line, and one line break after the colon: ``\s*`` ran on across every following blank
    # line from each line start, 4.3 s for 20 kB of blank lines, and with no break at all ``Cookie:`` with the
    # cookie on the next line passed, which 3.3.0 refused.
    "cookie_header": re.compile(
        r"^[ \t]*(?:cookie|set-cookie)[ \t]*:[ \t]*(?:\r?\n[ \t]*)?[^=\n;,\s]+=[^\n]+$",
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

COMMON_SECRET_PATTERN_VALUES: tuple[re.Pattern[str], ...] = tuple(
    COMMON_SECRET_PATTERNS.values()
)

#: A credential word given a value on its line: the word, the closing quote or bracket of a key (``"password":``,
#: ``["password"] =``, ``'password' =>``, and ``\"password\":`` in JSON inside a message), a separator (``:``,
#: ``=``, ``:=``, ``==``, ``!=``, ``是`` or "is"; a colon may close Markdown bold, ``**Password:** hunter2``; ``=>``
#: after a key without quotes only before a quoted value, as ``token => save(token)`` is a function) and the first
#: character of what follows.  Whether what follows is a credential is decided in Python, on the whole value
#: (``_credential_end``).  A lookahead here decided it before: it looked at how the value began and not at the rest,
#: so ``Mr.Smith(1985)``, ``str = "Xk9#mP2q"`` and "the password is this: Xk9#mP2q" passed, and in its last form its
#: end repeated a class of punctuation that its mask matched too, so dots after a key took quadratic time
#: (``password: `` and 20,000 dots, 2.9 s).  Every repetition here is bounded or possessive.
_SEPARATOR = (
    r"(?P<q>\\?[\"'`])?\]?"
    r"(?P<sep>[ \t]*+(?::=|={1,3}+(?![=>~])|!==?|:(?:(?:\*\*|__)(?=[ \t]))?|是|=>(?(q)|(?=[ \t]*+\\?[\"'`])))"
    r"|[ \t]++is(?:[ \t]*+:)?(?=[ \t]))"
    r"[ \t]*+(?=\S)"
)
#: ``credential``'s suffix is bounded: unbounded, a run of "credential" repeats was tried at every length from
#: every repeat, 1 s for 20 kB.
_SECRET_WORDS = (
    r"api[_ \t-]?key|secret(?:[_ \t-]?key)?|password|passwd|credential(?:[_ \t-]?[a-z0-9_]{1,64})?|"
    r"private[_ \t-]?key"
)
#: The name before ``token`` is at most 128 characters: unbounded, a long hyphenated line (a generated id, a
#: kebab-case slug) was tried from every hyphen to its end, 18 s for 60,000 characters.  A camel-case name counts
#: when a credential's word comes before ``Token`` (``accessToken``, ``authToken``); ``nextToken`` and
#: ``cancellationToken`` are not credentials.
_TOKEN_NAMES = (
    r"(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_-]{0,126}[_-])?token"
    r"|(?:(?<![A-Za-z0-9_])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
    r"(?:access|refresh|auth|api|id|bearer|session|secret|client|bot|user|app|service|oauth|jwt|github|gitlab|"
    r"slack|discord|telegram|npm|pypi|personal)(?-i:Token)"
)
#: Each family of keys with its separator.  A match is a key given something, not yet a credential: whether the value
#: is one is ``scan_secret_like_text``'s to say.
SECRET_ASSIGNMENT_RE = re.compile(r"(?P<key>" + _SECRET_WORDS + r")" + _SEPARATOR, re.IGNORECASE)
TOKEN_ASSIGNMENT_RE = re.compile(r"(?P<key>" + _TOKEN_NAMES + r")" + _SEPARATOR, re.IGNORECASE)
#: Both in one search, so that a value one of them exempts is not searched again by the other: the prompt in
#: ``token = getpass.getpass("Password: ")`` is no key.
_ASSIGNMENT_RE = re.compile(
    r"(?:(?P<word>" + _SECRET_WORDS + r")|(?P<key>" + _TOKEN_NAMES + r"))" + _SEPARATOR, re.IGNORECASE
)

#: How far a value is examined, from where it starts and again from where an exemption ends.  Every shape that is
#: not a credential is short, so a longer value is none of them, and each key costs bounded work: the scan runs on
#: every captured message and model request, up to a million characters, and Python's ``re`` holds the GIL.
_WINDOW = 256
_QUOTES = "\"'`"
_LINE_BREAK_RE = re.compile(r"[\r\n]")
_NON_SPACE_RE = re.compile(r"\S*+")
_SPACES_RE = re.compile(r"[ \t]*+")
#: Punctuation that may close a value before the space that ends it (``None,``, ``password)``, ``"x";``).  After a
#: word of prose "!" and "?" close it too ("required!"); after anything else they go on with it (``null!``).
_CLOSERS_RE = re.compile(r"[,;:.)\]}\"'`\u3001\u3002]*+")
_PROSE_CLOSERS_RE = re.compile(r"[,;:.!?)\]}\"'`\u3001\u3002]*+")
_QUOTED_RE = {quote: re.compile(quote + r"((?:[^" + quote + r"\\\r\n]|\\.)*+)(" + quote + r"?)") for quote in _QUOTES}
#: A string in JSON held by a message (a tool's output): ``\"Xk9#mP2q\"``.
_ESCAPED_QUOTED_RE = re.compile(r"\\([\"'`])((?:[^\\\r\n]|\\(?!\1).)*+)((?:\\\1)?)")

#: A value that stands for a credential rather than being one, matched against the whole value with the closing
#: punctuation after it: a placeholder with no digit in its name (``<your-api-key>``, ``${API_KEY}``, ``$API_KEY``,
#: ``%API_KEY%``, ``{api_key}``; ``<hunter2>``, ``{hunter2}`` and ``$unshine2024`` count), ``[REDACTED...]``, a mask
#: of one character (``********``, ``xxxx``, ``xxxx-xxxx``; ``xXxXxXxX`` and ``-_-_-_-`` count), a SQL parameter
#: (``$1``, ``?``, ``%s``, ``:name``), a dash, YAML's null (``~``), "n/a".
_PLACEHOLDER_RE = re.compile(
    r"(?:<[A-Za-z][A-Za-z_.-]{0,79}+>"
    r"|\$\{(?:env:)?[A-Za-z_][A-Za-z0-9_.]{0,79}+\}"
    r"|\$[A-Z][A-Z0-9_]{0,79}+"
    r"|%[A-Za-z_][A-Za-z0-9_]{0,79}+%"
    r"|\{\{?[A-Za-z_][A-Za-z_.]{0,79}+\}\}?"
    r"|\[(?i:redacted|hidden|masked|omitted|removed)[A-Za-z_ -]{0,40}+\]"
    r"|\*{3,}+|x{3,}+|X{3,}+|\u2022{3,}+|\u00b7{3,}+|\u25cf{3,}+|\.{3,}+|-{3,}+|_{3,}+|#{3,}+"
    r"|(?:x{2,}+[-_.]){1,8}+x{2,}+|(?:X{2,}+[-_.]){1,8}+X{2,}+"
    r"|\$\d{1,2}+|\?{1,3}+|%s|:[a-z_]{1,32}+|@[a-z_]{1,32}+|[-~\u2013\u2014]|(?i:n/a))"
    r"[,;:.)\]}\"'`]*+"
)
#: A placeholder of words, as documentation writes one: ``<your api key>``.  ``<correct horse battery staple>`` is a
#: passphrase.
_SPACED_PLACEHOLDER_RE = re.compile(
    r"<(?=[^<>\r\n]{0,80}?\b(?i:your|my|the|insert|enter|put|paste|here|key|token|password|secret|value|name)\b)"
    r"[A-Za-z][A-Za-z _.-]{0,79}+>"
)
#: A template's expression (``{{ vault_password }}``, ``${{ secrets.PASSWORD }}``): names only, no quoted literal.
_TEMPLATE_RE = re.compile(r"\$?\{\{[ \t]*+[A-Za-z_.][A-Za-z0-9_. \t|()-]{0,120}+\}\}")
#: A shell command whose output is the value (``$(cat token.txt)``); one that prints a literal is not.
_COMMAND_RE = re.compile(
    r"\$\((?!(?:echo|printf|print)\b)[a-z][A-Za-z0-9_.-]{0,40}+(?:[ \t]++[A-Za-z0-9_./:@%+=,-]{1,120}+){0,8}+\)"
)
#: A YAML block scalar (``password: |``, ``!vault |``): the value is the indented line below.
_BLOCK_SCALAR_RE = re.compile(r"(?:![A-Za-z_]{1,32}+[ \t]++)?[|>][-+]?[0-9]?[ \t]*+")
_INDENTED_LINE_RE = re.compile(r"(?:\r\n?|\n)(?:[ \t]*+(?:\r\n?|\n)){0,4}+[ \t]++(?=\S)")

#: A type or a null in code: ``str``, ``Optional[str]``, ``dict[str, str]``, ``&str``, ``string | null``, ``None``.
_TYPE_NAME = (
    r"(?:(?:typing|t)\.)?(?i:str|string|bytes|bytearray|int|integer|bool|boolean|float|double|number|any|unknown|"
    r"object|secretstr|secretbytes|dict|list|char|byte|u8|void|none|null|nil|undefined|nullptr|true|false|"
    r"optional)(?![\w$])"
)
_CONTAINER = (
    r"(?:(?:typing|t)\.)?(?i:optional|union|dict|list|tuple|set|frozenset|sequence|mapping|iterable|map|hashmap|"
    r"vec|array|record|option|box)"
)
_SIMPLE_TYPE = r"(?:&(?:'[a-z]{1,16}[ \t])?(?:mut[ \t])?|\*|\[\])?" + _TYPE_NAME + r"(?:\[\])?\??"


def _generic(inner: str) -> str:
    return _CONTAINER + r"[\[<]" + inner + r"(?:,[ \t]?" + inner + r"){0,4}[\]>]\??"


_TYPE_1 = r"(?:" + _generic(_SIMPLE_TYPE) + r"|" + _SIMPLE_TYPE + r")"
_TYPE_2 = r"(?:" + _generic(_TYPE_1) + r"|" + _TYPE_1 + r")"
_TYPE_RE = re.compile(_TYPE_2 + r"(?:[ \t]?\|[ \t]?" + _TYPE_2 + r"){0,3}")
_NULLS = frozenset("none null nil undefined nullptr true false".split())

#: Words that say what a value is rather than being it, in any case: "password: reset it from the login page".
_STATUS_WORDS = frozenset(
    "required missing empty unset invalid incorrect wrong expired reset changed hidden masked redacted omitted "
    "removed see tbd todo default".split()
)
#: After "is", the lower-case words that describe a value rather than give it.  One must end its clause: at a
#: punctuation mark, the line's end or a word that starts a phrase ("the token is sent in the header").  A word after
#: it that does not is the value going on ("the password is correct horse battery staple"), and so is what follows
#: "is this:", "is here:" or "is set to".
_DESCRIPTIVE_WORDS = frozenset(
    "required optional missing empty set unset invalid incorrect wrong right correct valid expired revoked changed "
    "reset stored saved hashed encrypted encoded hidden masked redacted needed sent used generated created issued "
    "refreshed rotated returned passed included attached shown printed logged signed verified checked validated "
    "accepted rejected denied blocked disabled enabled out ok fine weak strong long short same "
    "different what where that this it here there in on at for from with by secure insecure unique random "
    "mandatory sensitive private public important necessary unknown unavailable available provided given supplied "
    "configured defined undefined specified present absent blank leaked compromised exposed safe visible persisted "
    "cached cleared deleted removed updated renewed regenerated limited scoped bound tied linked".split()
)
#: Skipped between "is" and its word: "the password is not required", "the password is now Sunflower2024".
_SKIPPED_WORDS = frozenset(
    "not also only just still now always never usually often currently already then too very really probably "
    "automatically typically generally normally simply actually obviously definitely clearly likely rather quite "
    "being either neither no longer so much more less most least the a an your my our their its his her".split()
)
#: Words that start a phrase, so the clause before them has ended.
_PHRASE_WORDS = frozenset(
    "in on at by for from with within without into onto via per of and or but nor so yet when whenever if unless "
    "after before until while because since once again only here there then too either than during through across "
    "over under between against like anymore already now later soon first enough anyway instead also even still "
    "just every each both all any the a an this that these those your my our their its his her it you we they i he "
    "she them us me him upon inside outside behind below above along around unlike except whether though "
    "although".split()
)
#: Words after which the next word is a value: "reset to Xk9#mP2q", "stored as hunter2".
_INTRODUCERS = frozenset("to as is are was were be equals becomes".split())

#: Roots of a dotted name that is code, not a word: ``settings.DB_PASSWORD``, ``self.api_key``.  Lower-case only:
#: ``This.Is.Sparta`` is a password.
_CODE_ROOTS = frozenset(
    "os sys env environ settings config conf cfg process request req self this cls app ctx context options opts "
    "args params props secrets vault keyring form body data values state user payload creds credentials auth "
    "result res response resp import window".split()
)
#: A call that looks a value up by name, so that its first argument, quoted, is the name or a prompt:
#: ``os.getenv("TOKEN")``, ``input("Password: ")``.  A later argument, or ``default=``, is a value.
_LOOKUP_CALL_RE = re.compile(
    r"(?i:get\w*|\w*env\w*|var|input|getpass|prompt|ask|config|read\w*|load\w*|fetch\w*|lookup|require\w*|param\w*|"
    r"arg\w*|option\w*|secret\w*|str|value|field|header\w*|cookie\w*|pop|setdefault)"
)
_BARE_LOOKUPS = frozenset("input raw_input getpass prompt env config getenv ask secret require".split())
_EXAMINED_ARGUMENTS = frozenset("default fallback value initial defaultvalue default_value initial_value".split())

#: After ``是``: a Chinese question or description, in Chinese only (``token是什么意思``, ``token是用来认证的``,
#: ``token是否有效``).  ``token是什么Xk9#mP2q``, ``password是不是-_-_-_-`` and ``password是天王盖地虎`` are values.
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]")
_CJK_ONLY_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]{1,64}")
_CJK_RUN_RE = re.compile(r"[^\s,.;:!?\u3001\u3002\"'`)\]}\\]{1,64}+")
#: ``否`` is ``是否`` after ``是`` was read as the separator.
_CJK_QUESTION_RE = re.compile(r"什么|多少|哪个|哪些|啥|怎么|怎样|如何|不是|是否|否|必须|必需|必填|可选|过期|无效|有效")
_CJK_DETERMINER_RE = re.compile(r"一个|一种|一串|一段|这个|那个|每个|某个")
_CJK_YES_NO_RE = re.compile(r"不是|是否|否|必须|必需|必填|可选|过期|无效|有效")
_CJK_PARTICLES = ("了", "吗", "呢", "的", "吧", "啊")

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
    swallowed as its value and was refused as ``sensitive_request``: 369
    candidate evaluations on one instance, none holding a secret.  Text that
    was serialised more than once (a tool output that is itself JSON holding
    JSON) writes the same break as ``\\\\n``; treating only the last two
    characters as the break left a backslash after the slot, which then read
    as its value, so the whole run of backslashes belongs to the break.  The
    substitution keeps the length, so every match position stays valid.
    """

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = _ESCAPED_BREAK_RE.sub(lambda match: _ESCAPED_BREAKS[match.group(1)] * len(match.group(0)), normalized)
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


#: Tail words: a letter or digit with a symbol, letters with digits, or a long number could be a credential
#: (``Xk9#mP2q``, ``Sunflower2024``, ``12345678``).  URLs and paths are not.
_VALUE_LIKE_RE = re.compile(
    r"\d{6}|(?=\S{6})(?=\S*?[^\W\d_])(?=\S*?\d)|(?=\S{5})(?=\S*?[!#$%^&*+=~|\\])(?=\S*?[^\W_])"
)
_URL_OR_PATH_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,15}://|[~.]{0,2}/")
_WORD_EDGES = "()[]{}<>\"'`\\,.;:!?*#\u3001\u3002"
_PLAIN_WORD_RE = re.compile(r"[(\[{\"'`]*+(?:[^\W\d_]++(?:['\u2019_-][^\W\d_]++)*+|\d{1,3}+)[)\]}\"'`.,;:!?]*+")
_LETTERS_RE = re.compile(r"[A-Za-z]{1,32}+")
_PROSE_WORD_RE = re.compile(r"(?:[A-Za-z0-9]{1,16}+-)?([a-z]{1,32}+)")
_FALLBACK_RE = re.compile(r"(?:\|\||\?\?|\?:|\+(?![+=])|(?:or|else)(?![\w$]))[ \t]*+")
_SYMBOL_FALLBACK_RE = re.compile(r"(?:\|\||\?\?|\?:|\+(?![+=]))[ \t]*+")
_CONDITION_RE = re.compile(r"if(?![\w$])")
_ELSE_RE = re.compile(r"[ \t]else[ \t]++")
_PATH_RE = re.compile(
    r"(?:(?:await|new|yield)[ \t]++)?"
    r"([A-Za-z_][A-Za-z0-9_]{0,63}+(?:(?:\.|\?\.|::|->)[A-Za-z_][A-Za-z0-9_]{0,63}+){0,8}+)"
)
_PATH_SEPARATOR_RE = re.compile(r"\.|\?\.|::|->")
_MEMBER_RE = re.compile(r"(?:\.|\?\.|::|->)([A-Za-z_][A-Za-z0-9_]{0,63}+)")
_INDEX_RE = re.compile(
    r"\[[ \t]*+(?:(?P<q>\\?[\"'`])(?P<name>[A-Za-z_][A-Za-z0-9_.:/-]{0,79}+)(?P=q)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]{0,63}+)|(?P<number>-?\d{1,6}+(?::-?\d{0,6}+){0,2}+|:-?\d{1,6}+))[ \t]*+\]"
)
_KEYWORD_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63}+)[ \t]*+(?:=(?![=>])|:(?![:=]))[ \t]*+")
_NUMBER_RE = re.compile(r"-?\d{1,20}+(?:\.\d{1,20}+)?+(?![\w$])")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}+")
_IDENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,79}+")
_VARIABLE_RE = re.compile(r"\*{0,2}[A-Za-z_]{1,64}+(?:\.[A-Za-z_]{1,64}+){0,8}+(?![\w$])")
_OBJECT_KEY_RE = re.compile(
    r"(?:(?P<name>[A-Za-z_$][A-Za-z0-9_$]{0,63}+)|\"(?P<dq>[^\"\\\r\n]{0,64}+)\"|'(?P<sq>[^'\\\r\n]{0,64}+)')"
    r"[ \t]*+:[ \t]*+"
)
_CREDENTIAL_FIELD_RE = re.compile(r"(?i:api[_ -]?key|secret|password|passwd|token|credential|private[_ -]?key)")
_TAIL_KEY_RE = re.compile(r"\\?[\"'`]?[A-Za-z_$][\w.$-]{0,63}+\\?[\"'`]?\]?[ \t]*+(?::|=(?![=>]))")
_GAP_RE = re.compile(r"\s*+")
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_FOLD = str.maketrans("", "", "_- \t")


@dataclass(frozen=True)
class _Key:
    """What judging a value needs to know of its key."""

    name: str  # the whole name the credential word ends, folded: ``dbpassword`` for ``DB_PASSWORD``
    attribute: bool  # ``self.password``, ``this.token``, ``$this->password``, ``@password``
    argument: bool  # after ``(`` or ``,``: a keyword argument
    compare: bool  # ``token == ...``, ``password != ...``
    colon: bool  # ``password: ...``, ``password是...``, "the password is ...": the value may be the line's words


_NO_KEY = _Key("", False, False, False, False)
#: What a value is, for what may follow it: code (``str``, ``os.environ["X"]``), a word of prose (``required``,
#: ``什么``, a comment), or a stand-in for the value (``None``, ``***``, ``<your-key>``, ``""``).
_CODE, _PROSE, _STAND_IN = "code", "prose", "stand-in"


class _Lines:
    """The end of the line that holds a position, searched for once per line however many keys the line holds."""

    __slots__ = ("_text", "_start", "_end")

    def __init__(self, text: str) -> None:
        self._text = text
        self._start = 0
        self._end = -1

    def end(self, index: int) -> int:
        if not self._start <= index <= self._end:
            found = _LINE_BREAK_RE.search(self._text, index)
            self._start, self._end = index, found.start() if found else len(self._text)
        return self._end


def _key(text: str, match: re.Match[str]) -> _Key:
    group = "word" if match.group("word") is not None else "key"
    start = match.start(group)
    floor = max(0, start - 64)
    begin = start
    while begin > floor and text[begin - 1] in _NAME_CHARS:
        begin -= 1
    edge = begin
    while edge > floor and text[edge - 1] in " \t":
        edge -= 1
    sep = match.group("sep").strip().rstrip("*_")
    return _Key(
        name=text[begin : match.end(group)].translate(_FOLD).lower(),
        attribute=text[begin - 1 : begin] in (".", "@", "$") or text[max(0, begin - 2) : begin] == "->",
        argument=edge > 0 and text[edge - 1] in "(,",
        compare=sep in ("==", "===", "!=", "!=="),
        colon=sep in (":", "是") or sep.startswith("is"),
    )


def _next_char(text: str, index: int, limit: int) -> str:
    index = _SPACES_RE.match(text, index, limit).end()
    return text[index] if index < limit else ""


def _line_end_at(text: str, index: int, limit: int) -> int:
    found = _LINE_BREAK_RE.search(text, index, limit)
    return found.start() if found else limit


def _has_digit(text: str) -> bool:
    return any(char.isdigit() for char in text)


def _quoted_at(text: str, index: int, limit: int) -> tuple[int, str, bool] | None:
    """The quoted string at ``text[index]``, plain or in JSON held by a message: its end, its content and whether it
    closes before ``limit``; None when no string starts there."""
    char = text[index]
    if char in _QUOTES:
        found = _QUOTED_RE[char].match(text, index, limit)
        return found.end(), found.group(1), bool(found.group(2))
    if char == "\\" and index + 1 < limit and text[index + 1] in _QUOTES:
        found = _ESCAPED_QUOTED_RE.match(text, index, limit)
        return found.end(), found.group(2), bool(found.group(3))
    return None


def _plain_words(text: str) -> bool:
    """Words of prose: letters, short numbers and punctuation, nothing like ``Xk9#mP2q``."""
    return all(_PLAIN_WORD_RE.fullmatch(word) or not any(char.isalnum() for char in word) for word in text.split())


def _looks_like_a_value(word: str) -> bool:
    return _VALUE_LIKE_RE.match(word) is not None and _URL_OR_PATH_RE.match(word) is None


def _is_code_name(word: str) -> bool:
    """A variable's name rather than a word: ``new_password``, ``confirmPassword``, ``pw``."""
    if not word.isascii() or _has_digit(word):
        return False
    return "_" in word or (word[:1].islower() and not word.islower()) or len(word) <= 3


def _quoted_is_exempt(content: str) -> bool:
    """A quoted value that is empty, a placeholder, a mask, a type, a null or a status word."""
    value = content.strip()
    if not value:
        return True
    if len(value) > _WINDOW:
        return False
    return bool(
        _PLACEHOLDER_RE.fullmatch(value)
        or _SPACED_PLACEHOLDER_RE.fullmatch(value)
        or _TEMPLATE_RE.fullmatch(value)
        or _TYPE_RE.fullmatch(value)
        or value.lower() in _STATUS_WORDS
    )


def _words_are_clean(text: str, index: int, line_end: int) -> bool:
    """Whether the next few words on the line give no value: none looks like a credential, and none follows a
    word that introduces one ("reset to Sunflower2024", "stored as hunter2") unless it starts a phrase."""
    limit = min(line_end, index + _WINDOW // 2)
    introduced = False
    for _ in range(4):
        index = _SPACES_RE.match(text, index, limit).end()
        if index >= limit:
            return True
        word = _NON_SPACE_RE.match(text, index, limit)
        core = word.group(0).strip(_WORD_EDGES)
        if core:
            lower = core.lower()
            if (introduced and lower not in _PHRASE_WORDS) or _looks_like_a_value(core):
                return False
            introduced = lower in _INTRODUCERS
        index = word.end()
    return True


def _starts_something_else(text: str, index: int, line_end: int) -> bool:
    """Whether the words after a stand-in start something other than the value: the next key, a comment, a phrase or
    a note ("from the dashboard", "(hidden)").  ``password: *** sunshine`` and ``password: none, sunshine`` give one."""
    if _TAIL_KEY_RE.match(text, index, line_end) or text.startswith(("#", "//"), index):
        return True
    word = _NON_SPACE_RE.match(text, index, min(line_end, index + 64)).group(0).strip(_WORD_EDGES).lower()
    return word in _PHRASE_WORDS or word in _STATUS_WORDS


def _tail_is_clean(text: str, end: int, line_end: int, key: _Key, depth: int, kind: str) -> bool:
    """Whether what follows an exempt value on its line leaves it exempt.  The value must end where its shape does:
    a character glued to it goes on with it (``Changed!2024``, ``love()you``, ``<b>Xk9#mP2q</b>``).  An assignment or
    a fallback after it is judged as the value (``str = "Xk9#mP2q"``, ``|| "Xk9#mP2q"``, ``or "Xk9#mP2q"``); after
    ``:``, ``是`` or "is", what follows a stand-in must start something else; and the next words may not look like a
    credential (``--- Xk9#mP2q``, "reset to Sunflower2024")."""
    prose = kind == _PROSE
    index = (_PROSE_CLOSERS_RE if prose else _CLOSERS_RE).match(text, end, line_end).end()
    if index < line_end and not text[index].isspace() and not (prose and index > end and _CJK_RE.match(text, index)):
        # (Chinese goes on after a clause mark without a space: its words are read below)
        if index == end and text[index] == "=" and not text.startswith(("==", "=>"), index):
            return _is_exempt(text, _SPACES_RE.match(text, index + 1, line_end).end(), line_end, key, depth + 1) >= 0
        return False
    index = _SPACES_RE.match(text, index, line_end).end()
    if index >= line_end:
        return True
    if text[index] == "=" and not text.startswith(("==", "=>"), index):
        return _is_exempt(text, _SPACES_RE.match(text, index + 1, line_end).end(), line_end, key, depth + 1) >= 0
    fallback = (_FALLBACK_RE if kind == _CODE else _SYMBOL_FALLBACK_RE).match(text, index, line_end)
    if fallback is not None:
        # a variable there is code, as in a comparison: token = token or default_token
        return _is_exempt(text, fallback.end(), line_end, replace(key, compare=True), depth + 1) >= 0
    if kind == _CODE and _CONDITION_RE.match(text, index, line_end):
        other = _ELSE_RE.search(text, index, min(line_end, index + _WINDOW))
        if other is not None:
            return _is_exempt(text, other.end(), line_end, key, depth + 1) >= 0
    if kind == _STAND_IN and depth == 0 and key.colon and not _starts_something_else(text, index, line_end):
        return False
    return _words_are_clean(text, index, line_end)


def _index_end(text: str, start: int, limit: int, parts: list[str]) -> tuple[int, bool]:
    """A subscript: a quoted name, a variable, or a number or slice on a lower-case name (``row[2]``, not
    ``J.Doe[2020]``).  Returns its end and whether it was a quoted name, or (-1, False)."""
    found = _INDEX_RE.match(text, start, limit)
    if found is None:
        return -1, False
    if found.group("name") is not None:
        return found.end(), True
    if found.group("ident") is not None:
        return (-1, False) if _has_digit(found.group("ident")) else (found.end(), False)
    root = parts[0]
    return (found.end(), False) if root[:1].islower() and not _has_digit(root) else (-1, False)


def _argument_end(text: str, index: int, limit: int, line_end: int, name: str | None, looked_up: bool,
                  chained: bool, numbers: bool, depth: int) -> int:
    """One argument of a call, or -1 when it gives a credential."""
    if index >= limit:
        return -1
    examined = name is not None and (name.lower() in _EXAMINED_ARGUMENTS or SENSITIVE_KEY_COMPONENT_RE.search(name))
    if text.startswith("...", index):
        return index + 3
    quoted = _quoted_at(text, index, limit)
    if quoted is not None:
        end, content, closed = quoted
        if not closed:
            return -1
        # A name or words, not a value in a string, and not a credential given in them: the search does not look
        # inside an exempt value again, so os.getenv("token: sunshine") is judged here.
        named = ((_IDENT_NAME_RE.fullmatch(content) is not None or _plain_words(content))
                 and not (_ASSIGNMENT_RE.search(content) and contains_secret_like_text(content)))
        if not examined and named and (name is not None or looked_up or chained):
            # algorithm="HS256"; the name looked up or a prompt, input("Password: "); after a first call,
            # .required("Password is required"), .removeprefix("Bearer ")
            return end
        return end if _quoted_is_exempt(content) else -1
    number = _NUMBER_RE.match(text, index, limit)
    if number is not None:
        # a number sets something in code (secrets.token_urlsafe(32), z.string().min(8), size=8), not in a word
        # (letmein(2024), john.doe(42), Mr.Smith(1985)) or as a default (default=123456)
        return number.end() if not examined and (numbers or name is not None) else -1
    variable = _VARIABLE_RE.match(text, index, limit)
    if variable is not None and _next_char(text, variable.end(), line_end) not in ("(", "[", "."):
        return variable.end()  # a variable: hash_password(raw), f(user.id); not letmein(2024) or hunter2(x)
    end, _ = _shape_end(text, index, limit, line_end, _NO_KEY, depth + 1)
    return end


def _arguments_end(text: str, start: int, parts: list[str], calls: int, depth: int) -> int:
    """Where the argument list opened at ``text[start]`` ends, or -1 when an argument gives a credential.  A lookup's
    first argument, quoted, is what it looks up (``os.getenv("TOKEN")``, ``input("Password: ")``); a later one, or a
    ``default=``, is a value (``os.environ.get("DB_PASSWORD", "Xk9#mP2q")``).  The list may go on over the lines
    below, as a formatter writes a long call."""
    callee = parts[-1]
    looked_up = (_LOOKUP_CALL_RE.fullmatch(callee) is not None) if len(parts) > 1 else callee in _BARE_LOOKUPS
    numbers = (calls > 0 or "_" in callee.strip("_") or (callee[:1].islower() and not callee.islower())
               or (len(parts) > 1 and parts[0] in _CODE_ROOTS))
    limit = min(len(text), start + 2 * _WINDOW)
    index = _GAP_RE.match(text, start + 1, limit).end()
    position = 0
    while index < limit:
        if text[index] == ")":
            return index + 1
        line_end = _line_end_at(text, index, limit)
        keyword = _KEYWORD_RE.match(text, index, line_end)
        name = keyword.group(1) if keyword is not None else None
        begin = keyword.end() if keyword is not None else index
        index = _argument_end(text, begin, min(line_end, begin + _WINDOW), line_end, name,
                              looked_up and position == 0 and name is None, calls > 0, numbers, depth)
        if index < 0:
            return -1
        index = _GAP_RE.match(text, index, limit).end()
        if index < limit and text[index] == ",":
            index = _GAP_RE.match(text, index + 1, limit).end()
            position += 1
        elif index >= limit or text[index] != ")":
            return -1
    return -1


def _code_path(parts: list[str], calls: int, named: bool) -> bool:
    """Whether a called or subscripted name is code: not ``Mr.Smith(...)``, ``J.Doe[...]`` or ``Hunter2(...)``."""
    root = parts[0]
    if _has_digit(root) and (len(parts) == 1 or not root.islower()):
        return False  # a module may have digits (base64.b64encode, boto3.client), a word may not (Hunter2(backup))
    if len(parts) == 1 or root in _CODE_ROOTS or root[0].islower() or root[0] == "_":
        return True
    # Yup.string(), System.getenv("X"), Environment.GetEnvironmentVariable("X"), Settings.Values["ApiKey"]
    return (any(part[:1].islower() for part in parts[1:]) or _LOOKUP_CALL_RE.fullmatch(parts[-1]) is not None
            or (named and not calls))


def _name_end(text: str, end: int, line_end: int, parts: list[str], key: _Key) -> tuple[int, str]:
    """A bare name: a dotted name from a code root (``settings.DB_PASSWORD``), a status word, the key's own name as
    an attribute or an argument (``self.password = password``, ``f(password=password)``), or a variable where one is
    compared or passed (``token == expected_token``, ``f(password=new_password)``)."""
    follows = _next_char(text, end, line_end)
    if len(parts) > 1:
        root = parts[0]
        code = root in _CODE_ROOTS or ("_" in root.strip("_") and root.islower() and not _has_digit(root))
        return (end, _CODE) if code or (key.compare and root[:1].islower()) else (-1, _CODE)
    word = parts[0]
    if word.lower() in _STATUS_WORDS:
        return end, _PROSE
    if word.translate(_FOLD).lower() == key.name and (
            key.attribute or follows in (",", ")", "]", "}")
            or _FALLBACK_RE.match(text, _SPACES_RE.match(text, end, line_end).end(), line_end) is not None):
        return end, _CODE  # the fallback after it is judged as the value: token = token or "Xk9#mP2q"
    if _is_code_name(word) and (key.compare or (key.argument and follows in (",", ")"))):
        return end, _CODE
    return -1, _CODE


def _code_end(text: str, start: int, limit: int, line_end: int, key: _Key, depth: int) -> tuple[int, str]:
    """A name, or code that calls or subscripts one and gives no credential in its arguments."""
    path = _PATH_RE.match(text, start, limit)
    if path is None:
        return -1, _CODE
    parts = _PATH_SEPARATOR_RE.split(path.group(1))
    end = path.end()
    calls = 0
    named = True
    trailers = False
    while end < limit:
        char = text[end]
        if char == "(":
            end = _arguments_end(text, end, parts, calls, depth)
            calls += 1
        elif char == "[":
            end, quoted_name = _index_end(text, end, limit, parts)
            named = named and quoted_name
        elif (member := _MEMBER_RE.match(text, end, limit)) is not None:
            parts.append(member.group(1))
            end = member.end()
            continue
        elif char in "?!" and (calls or len(parts) > 1):
            end += 1  # Rust's ?, TypeScript's !: process.env.TOKEN!
            continue
        else:
            break
        if end < 0:
            return -1, _CODE
        trailers = True
    if not trailers:
        return _name_end(text, end, line_end, parts, key)
    return (end, _CODE) if _code_path(parts, calls, named) else (-1, _CODE)


def _group_end(text: str, start: int, limit: int, line_end: int, depth: int) -> int:
    """A brace, bracket or parenthesis: an empty pair; a brace alone at its line's end, whose fields on the lines
    below are each searched as a key of their own; an inline object whose fields are exempt; a list or a parenthesis
    whose values are, which may go on over the lines below.  ``(Summer2024)``, ``[hunter2]`` and a ``(`` with
    ``"Xk9#mP2q"`` on the line below are values."""
    opener = text[start]
    closer = {"(": ")", "[": "]", "{": "}"}[opener]
    if depth >= 3:
        return -1
    after = _SPACES_RE.match(text, start + 1, line_end).end()
    closed = _CLOSERS_RE.match(text, after, line_end).end()
    if closed >= line_end and (opener == "{" or closed > after):
        return line_end  # alone at its line's end, or closing the string that holds it: "\"credentials\": {"
    if opener == "{":
        index = _SPACES_RE.match(text, start + 1, line_end).end()
        while index < limit:
            if text[index] == closer:
                return index + 1
            field = _OBJECT_KEY_RE.match(text, index, limit)
            if field is None:
                return -1
            index = field.end()
            name = field.group("name") or field.group("dq") or field.group("sq") or ""
            number = _NUMBER_RE.match(text, index, limit)
            variable = _VARIABLE_RE.match(text, index, limit)
            if _CREDENTIAL_FIELD_RE.search(name):
                # judged as its key would be, not as a setting: {password: checker} gives one
                index, _ = _shape_end(text, index, limit, line_end, _Key(name.translate(_FOLD).lower(), False, False,
                                                                        False, True), depth + 1)
            elif number is not None and number.end() - index <= 3:
                index = number.end()  # minlength: 8
            elif variable is not None and ("." in variable.group(0) or _is_code_name(variable.group(0))):
                index = variable.end()  # ref: Schema.Types.ObjectId, validate: checkEmail
            else:
                index, _ = _shape_end(text, index, limit, line_end, _NO_KEY, depth + 1)
            if index < 0:
                return -1
            index = _SPACES_RE.match(text, index, limit).end()
            if index < limit and text[index] == closer:
                return index + 1
            if index >= limit or text[index] != ",":
                return -1
            index = _SPACES_RE.match(text, index + 1, limit).end()
        return -1
    limit = min(len(text), start + 2 * _WINDOW)
    index = _GAP_RE.match(text, start + 1, limit).end()
    if index >= len(text):
        return index  # an opener at the end of the text gives nothing
    while index < limit:
        if text[index] == closer:
            return index + 1
        here = _line_end_at(text, index, limit)
        index, _ = _shape_end(text, index, min(here, index + _WINDOW), here, _NO_KEY, depth + 1)
        if index < 0:
            return -1
        index = _GAP_RE.match(text, index, limit).end()
        if index < limit and text[index] == closer:
            return index + 1
        if opener == "(" or index >= limit or text[index] != ",":
            return -1
        index = _GAP_RE.match(text, index + 1, limit).end()
    return -1


def _shape_end(text: str, start: int, limit: int, line_end: int, key: _Key, depth: int) -> tuple[int, str]:
    """Where the shape of a non-credential value at ``text[start]`` ends, and what kind of value it is (``_CODE``,
    ``_PROSE`` or ``_STAND_IN``); -1 when it has none."""
    if depth > 3:
        return -1, _CODE
    char = text[start]
    quoted = _quoted_at(text, start, line_end)
    if quoted is not None:
        end, content, _ = quoted
        return (end, _STAND_IN) if end <= limit and _quoted_is_exempt(content) else (-1, _CODE)
    token_end = _NON_SPACE_RE.match(text, start, limit).end()
    if _PLACEHOLDER_RE.fullmatch(text, start, token_end):
        return token_end, _STAND_IN
    for pattern in (_TEMPLATE_RE, _SPACED_PLACEHOLDER_RE):
        found = pattern.match(text, start, limit)
        if found is not None:
            return found.end(), _STAND_IN
    command = _COMMAND_RE.match(text, start, limit)
    if command is not None:
        # $(cat token.txt), not $(base64 -d c3Vuc2hpbmU=)
        arguments = text[command.start() + 2 : command.end() - 1].split()[1:]
        return (command.end(), _STAND_IN) if not any(map(_looks_like_a_value, arguments)) else (-1, _CODE)
    if text[start:token_end] in ("#", "//"):
        return token_end, _PROSE  # a comment where the value would be: its words are read as prose
    if char == "$":
        name = _IDENT_RE.match(text, start + 1, limit)
        if (name is not None and name.group(0).translate(_FOLD).lower() == key.name
                and (key.attribute or _next_char(text, name.end(), line_end) in (",", ")", "]", "}"))):
            return name.end(), _CODE  # PHP: $this->password = $password
        return -1, _CODE
    if _CJK_RE.match(text, start):
        run = _CJK_RUN_RE.match(text, start, limit)
        value = run.group(0)
        if _CJK_ONLY_RE.fullmatch(value) and (
                _CJK_QUESTION_RE.match(value) or _CJK_DETERMINER_RE.match(value) or value.endswith("的")):
            # "is it ...?" or "must be ..." names a value: password是不是天王盖地虎 gives one, token是否有效 and
            # 是不是太短了 do not; after "what", "how" or "which" the words are the question (token是什么意思)
            asked = _CJK_YES_NO_RE.match(value)
            rest = value[asked.end():] if asked else ""
            if not rest or _CJK_QUESTION_RE.match(rest) or rest.endswith(_CJK_PARTICLES):
                return run.end(), _PROSE
        return -1, _CODE
    if char in "&*[" or (char.isascii() and (char.isalpha() or char == "_")):
        typed = _TYPE_RE.match(text, start, limit)
        if typed is not None:
            after = _CLOSERS_RE.match(text, typed.end(), line_end).end()
            if after >= line_end or text[after] in " \t=":
                return typed.end(), _STAND_IN if typed.group(0).lower() in _NULLS else _CODE
    if char in "{[(":
        return _group_end(text, start, limit, line_end, depth), _CODE
    if char.isascii() and (char.isalpha() or char == "_"):
        return _code_end(text, start, limit, line_end, key, depth)
    return -1, _CODE


def _is_exempt(text: str, start: int, line_end: int, key: _Key, depth: int) -> int:
    """Where the value at ``text[start]`` ends when the whole of it, with what follows it on its line, is known not
    to be a credential; -1 when it may be one."""
    if start >= line_end:
        return line_end  # nothing given: ``password: str =`` at the line's end
    if depth > 3:
        return -1
    end, kind = _shape_end(text, start, min(line_end, start + _WINDOW), line_end, key, depth)
    if end < 0:
        return -1
    if end > line_end:
        line_end = _line_end_at(text, end, len(text))  # a call or a parenthesis that went on over lines
    return end if _tail_is_clean(text, end, line_end, key, depth, kind) else -1


def _prose_is_exempt(text: str, start: int, line_end: int, key: _Key) -> int:
    """After "is": a word that describes a value and ends its clause, or what is no credential after any separator
    (``None``, ``********``, ``<your-key>``).  Returns where it ends, or -1."""
    limit = min(line_end, start + _WINDOW)
    index = start
    for _ in range(4):
        word = _LETTERS_RE.match(text, index, limit)
        if word is None or word.group(0) not in _SKIPPED_WORDS:
            break
        after = _SPACES_RE.match(text, word.end(), limit).end()
        if after == word.end():
            break
        index = after
    if index >= line_end:
        return line_end
    word = _PROSE_WORD_RE.match(text, index, limit)
    if word is None or word.group(1) not in _DESCRIPTIVE_WORDS:
        return _is_exempt(text, index, line_end, key, 0)
    end = index = word.end()
    if index >= line_end:
        return end
    if text[index] in ":=":
        # "is this: Xk9#mP2q", "is here = Xk9#mP2q"
        return _is_exempt(text, _SPACES_RE.match(text, index + 1, line_end).end(), line_end, key, 1)
    if text[index] not in " \t":
        return end if _tail_is_clean(text, index, line_end, key, 1, _PROSE) else -1
    after = _SPACES_RE.match(text, index, line_end).end()
    if after >= line_end:
        return end
    following = _LETTERS_RE.match(text, after, line_end)
    if following is None:
        return end if _tail_is_clean(text, index, line_end, key, 1, _PROSE) else -1  # "(min 8)", "- see below"
    lower = following.group(0).lower()
    if lower in _PHRASE_WORDS or lower in ("to", "as"):
        return end if _words_are_clean(text, after, line_end) else -1
    return -1  # "correct horse battery staple": the words go on and give the value


def _credential_end(text: str, match: re.Match[str], lines: _Lines) -> tuple[bool, int]:
    """Whether what follows the key at ``match`` is a credential, and where it ends.  A credential's end covers the
    value: a quoted one to its closing quote; one whose line ends in an open bracket or a comma to the end of the
    line below (``decrypt(`` above the literal); one after ``:``, ``是`` or "is" (it may hold spaces) to the line's
    end; any other to the next space, or to the line's end when it opens a quote or a bracket.  What is not a
    credential ends where its shape does: the search goes on from there, not inside it."""
    start = match.end()
    line_end = lines.end(start)
    key = _key(text, match)
    sep = match.group("sep").strip().rstrip("*_")
    if _BLOCK_SCALAR_RE.fullmatch(text, start, line_end):
        below = _INDENTED_LINE_RE.match(text, line_end)
        if below is None or text.startswith("$ANSIBLE_VAULT;", below.end()):
            return False, line_end
        found = _LINE_BREAK_RE.search(text, below.end())
        body_end = found.start() if found else len(text)
        exempt = _is_exempt(text, below.end(), body_end, key, 1) >= 0
        return (False, line_end) if exempt else (True, body_end)
    if sep.startswith("is") and not sep.endswith(":"):
        end = _prose_is_exempt(text, start, line_end, key)
    else:
        end = _is_exempt(text, start, line_end, key, 0)
    if end >= 0:
        return False, end
    quoted = _quoted_at(text, start, line_end)
    if quoted is not None:
        return True, quoted[0] if quoted[2] else line_end
    if line_end - start <= _WINDOW and text[start:line_end].rstrip().endswith(("(", "[", ",")):
        # decrypt( with the literal on the line below
        return True, _line_end_at(text, _GAP_RE.match(text, line_end, min(len(text), line_end + _WINDOW)).end(),
                                  len(text))
    if sep in (":", "是") or sep.startswith("is"):
        return True, line_end
    end = _NON_SPACE_RE.match(text, start, line_end).end()
    opened = text[start : min(end, start + _WINDOW)]
    return True, line_end if any(char in opened for char in "\"'`([{") else end


def _assignment_matches(shadow: str) -> list[SecretTextMatch]:
    """Every credential word given a credential, from the word to where the value ends.  The search goes on after
    each value, a credential or not, so each part of the text is judged once: linear time."""
    lines = _Lines(shadow)
    found: list[SecretTextMatch] = []
    position = 0
    while (match := _ASSIGNMENT_RE.search(shadow, position)) is not None:
        position = match.end()
        token = match.group("word") is None
        if token and is_safe_token_metric_key(match.group("key")):
            continue
        secret, end = _credential_end(shadow, match, lines)
        if secret:
            name = "token_assignment" if token else "api_key_assignment"
            found.append(SecretTextMatch(name, match.start(), end, shadow[match.start() : end]))
        position = max(position, end)
    return found


def scan_secret_like_text(value: Any) -> tuple[SecretTextMatch, ...]:
    """Return all canonical secret-like matches in one normalized scan view."""

    shadow = secret_scan_shadow(value)
    candidates = _assignment_matches(shadow)
    for name, pattern in COMMON_SECRET_PATTERNS.items():
        candidates.extend(
            SecretTextMatch(name, match.start(), match.end(), match.group(0))
            for match in pattern.finditer(shadow)
        )
    # Keep ordering deterministic and collapse exact duplicate classifier output.
    unique: dict[tuple[str, int, int], SecretTextMatch] = {}
    for match in candidates:
        unique.setdefault((match.name, match.start, match.end), match)
    return tuple(sorted(unique.values(), key=lambda item: (item.start, item.end, item.name)))


def contains_secret_like_text(value: Any) -> bool:
    """Return whether the canonical scan API found any secret-like material."""

    return bool(scan_secret_like_text(value))
