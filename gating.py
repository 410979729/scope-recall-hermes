"""General gating, normalization, and compact-text helpers used across capture, recall, and reporting.

Keep these helpers deterministic and side-effect free because many safety checks depend on their exact behavior."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, List, Set

TRIVIAL_RE = re.compile(
    r"^(?:"
    r"ok|okay|kk|k|yes|no|yep|nope|sure|thanks|thank you|thx|ty|got it|roger|"
    r"understood|noted|acknowledged|done|"
    r"hi|hello|hey|yo|早|早安|你好|嗨|在吗|在嗎|谢谢|謝謝|收到|明白|明白了|了解|了解了|好的|好"
    r")(?:[!！,.。?？~\s]*)$",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]{2,}")
_CJK_TOKEN_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_DIRECT_CJK_LOCATION_QUERY_RE = re.compile(
    r"^(?:请问)?(?P<subject>[\u4e00-\u9fff]{2,12}?)(?:现在|目前|当前)?"
    r"(?P<predicate>住|居住|位于|运行|部署|工作|生活|存放)?"
    r"(?:在哪里|在哪儿|在哪|身处何地|位于何处|在何处)[?？!！。]?$"
)
_CJK_QUERY_STOPWORDS = {
    "一个",
    "什么",
    "哪个",
    "哪里",
    "哪",
    "哪儿",
    "何处",
    "为什么",
    "以及",
    "当前",
    "告诉",
    "告诉我",
    "多少",
    "如今",
    "怎么",
    "怎样",
    "是否",
    "是不是",
    "是",
    "有没有",
    "最近",
    "核验",
    "现在",
    "的",
    "请",
    "目前",
    "还是",
    "这个",
    "那个",
    "或者",
}
_SEMANTIC_QUERY_STOPWORDS = _CJK_QUERY_STOPWORDS | {
    "a",
    "an",
    "are",
    "at",
    "be",
    "current",
    "currently",
    "do",
    "does",
    "for",
    "how",
    "in",
    "is",
    "my",
    "now",
    "of",
    "on",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
}
_LOCATION_QUERY_RE = re.compile(r"(?:在哪里|在哪|哪儿|何处|什么位置)")
_OPERATING_SYSTEM_QUERY_RE = re.compile(
    r"(?:(?:跑|用|使用|运行).{0,4}(?:什么|哪个|哪种)(?:操作)?系统|"
    r"(?:什么|哪个|哪种)(?:操作)?系统)"
)
_TIMEZONE_QUERY_RE = re.compile(r"(?:\btime\s*zone\b|\btimezone\b|时区)", re.IGNORECASE)
_ATTRIBUTE_QUERY_RE = re.compile(
    r"(?:^|的)(?P<subject>[\u4e00-\u9fffA-Za-z0-9_.-]{2,16})"
    r"(?P<attribute>型号|版本|名称|名字)(?:是|为)?(?:什么|哪个|哪一个|哪种)?"
)
_LOCATION_INTENT_TERMS = ("位置", "地址", "路径", "目录", "主机", "本机", "运行环境")
_OPERATING_SYSTEM_INTENT_TERMS = (
    "操作系统",
    "运行环境",
    "主机",
    "本机",
    "windows",
    "linux",
    "macos",
)
_TIMEZONE_INTENT_TERMS = ("时区", "timezone", "time zone", "utc", "gmt")
_TIMEZONE_SUBJECT_TERMS = ("工作电脑", "workstation", "电脑", "本机", "主机", "server")
_TIMEZONE_VALUE_RE = re.compile(
    r"(?:[a-z]+/[a-z0-9_+\-]+|\b(?:utc|gmt)(?:[+\-]\d{1,2}(?::?\d{2})?)?\b|"
    r"美国东部时区|美国西部时区|东部时区|西部时区|北京时间)",
    re.IGNORECASE,
)
_TIMEZONE_ANSWER_RELATION_RE = re.compile(
    r"(?:时区(?:偏好)?\s*[:：]?|使用|采用|设置(?:为|成)?|"
    r"timezone\s*(?:is|:)|uses?)"
    r"[\s\S]{0,48}"
    r"(?:[a-z]+/[a-z0-9_+\-]+|\b(?:utc|gmt)(?:[+\-]\d{1,2}(?::?\d{2})?)?\b|"
    r"美国东部时区|美国西部时区|东部时区|西部时区|北京时间)",
    re.IGNORECASE,
)
_LOCATION_INTENT_EVIDENCE_TERMS = ("位置", "地址", "路径", "目录", "主机", "本机", "运行环境", "live root", "live根")
_OPERATING_SYSTEM_INTENT_EVIDENCE_TERMS = (
    "操作系统",
    "运行环境",
    "windows",
    "linux",
    "macos",
    "debian",
    "ubuntu",
    "rocky linux",
    "rocky",
    "red hat",
    "rhel",
    "centos",
    "fedora",
    "alpine",
    "arch linux",
    "nixos",
    "freebsd",
    "wsl2",
    "wsl",
)
_OPERATING_SYSTEM_ANSWER_RELATION_RE = re.compile(
    r"(?:"
    r"(?:运行(?:在|于|的是)?|使用(?:的是)?|采用|基于|"
    r"操作系统\s*(?:是|为|:|：)|系统\s*(?:是|为|:|：)|"
    r"runs?\s+on|is\s+running|operating\s+system\s+is)"
    r"[\s\S]{0,24}"
    r")"
    r"(?:windows|linux|macos|debian|ubuntu|rocky(?:\s+linux)?|"
    r"red\s+hat|rhel|centos|fedora|alpine|arch\s+linux|nixos|freebsd|wsl2?|"
    r"[\u4e00-\u9fff]{2,8}(?:操作)?系统)\b",
    re.IGNORECASE,
)
_CURRENT_STATE_QUERY_RE = re.compile(
    r"(?:\b(?:current|currently|latest|newest|now|today)\b|当前|目前|现在|如今|最新)"
)
_HISTORICAL_STATE_QUERY_RE = re.compile(
    r"(?:\b(?:previously|before|history|historical|formerly|used\s+to|as\s+of)\b|"
    r"之前|以前|过去|当时|曾经|历史|原来)"
)
MEMORY_CONTEXT_RE = re.compile(
    r"<memory-context>[\s\S]*?</memory-context>\s*", re.IGNORECASE
)
SUPERMEMORY_CONTEXT_RE = re.compile(
    r"<supermemory-context>[\s\S]*?</supermemory-context>\s*", re.IGNORECASE
)


def stringify_content(value: Any) -> str:
    """Normalize Hermes/OpenAI structured message content into plain text.

    Hermes may pass message content as OpenAI-style structured parts, for
    example [{"type": "text", "text": "hi"}] or multimodal blocks. Capture and
    recall filters are regex based, so they must receive text rather than raw
    lists/dicts.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        text = value.get("text")
        if text is not None:
            return stringify_content(text)
        content = value.get("content")
        if content is not None:
            return stringify_content(content)
        return " ".join(
            stringify_content(item)
            for key, item in value.items()
            if key not in {"type", "mime_type", "media_type"}
        ).strip()
    if isinstance(value, Iterable):
        return "\n".join(
            part for part in (stringify_content(item).strip() for item in value) if part
        )
    return str(value)


def clean_text(text: Any) -> str:
    text = stringify_content(text)
    text = MEMORY_CONTEXT_RE.sub("", text or "")
    text = SUPERMEMORY_CONTEXT_RE.sub("", text)
    return text.strip()


def compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def is_trivial(text: str) -> bool:
    return bool(TRIVIAL_RE.match((text or "").strip()))


def normalize_query(text: str, char_limit: int) -> str:
    return clean_text(text)[:char_limit].strip()


def should_skip_retrieval(query: str, min_length: int) -> bool:
    if not query:
        return True
    if is_trivial(query):
        return True
    if len(query) < min_length:
        return True
    return False


def _deterministic_cjk_segments(token: str) -> List[str]:
    reduced = token
    for stopword in sorted(_CJK_QUERY_STOPWORDS, key=len, reverse=True):
        reduced = reduced.replace(stopword, " ")
    segments: list[str] = []
    for piece in reduced.split():
        if len(piece) >= 2:
            segments.append(piece)
        for width in (2, 3):
            if len(piece) <= width:
                continue
            segments.extend(
                piece[index : index + width]
                for index in range(0, len(piece) - width + 1)
            )
    return segments


def _cjk_query_segments(token: str) -> List[str]:
    """Return bounded search terms for one long CJK run.

    SQLite's unicode61 tokenizer treats an unspaced Chinese sentence as one
    token.  Keeping that token alone makes a natural question miss memories
    that contain the same concepts inside a different sentence.  Jieba is an
    existing optional dependency of Scope Recall's entity extractor, so use
    its search segmentation here while preserving the original token and the
    previous no-jieba fallback behavior.
    """

    if len(token) < 4 or not _CJK_TOKEN_RE.fullmatch(token):
        return []
    raw_terms: list[str] = []
    try:
        import jieba  # type: ignore[import-not-found]

        jieba.setLogLevel(logging.WARNING)
        raw_terms.extend(str(term) for term in jieba.cut_for_search(token, HMM=True))
    except Exception:
        pass
    raw_terms.extend(_deterministic_cjk_segments(token))

    positioned: dict[str, int] = {}
    for raw_term in raw_terms:
        term = str(raw_term or "").strip()
        if (
            len(term) < 2
            or term == token
            or term in _CJK_QUERY_STOPWORDS
            or not _CJK_TOKEN_RE.fullmatch(term)
        ):
            continue
        positioned.setdefault(term, token.find(term))
    terms = [
        term
        for term in positioned
        if len(term) > 2
        or not any(len(other) > len(term) and term in other for other in positioned)
    ]
    return sorted(terms, key=lambda term: (-len(term), positioned[term], term))[:11]


def query_tokens(text: str) -> List[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def append(token: str) -> None:
        if token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for token in WORD_RE.findall(text.lower()):
        append(token)
        for segment in _cjk_query_segments(token):
            append(segment)
    return tokens


def semantic_query_tokens(text: str) -> List[str]:
    """Return relevance-bearing tokens shared by recall candidate and scoring paths."""

    direct_location = _DIRECT_CJK_LOCATION_QUERY_RE.fullmatch(clean_text(text))
    if direct_location is not None:
        tokens = [direct_location.group("subject")]
        predicate = str(direct_location.group("predicate") or "")
        if predicate:
            normalized_predicate = "住在" if predicate in {"住", "居住"} else predicate
            if normalized_predicate not in tokens:
                tokens.append(normalized_predicate)
        return tokens

    raw_tokens = query_tokens(text)
    output: list[str] = []
    for token in raw_tokens:
        normalized = token.casefold().strip()
        if not normalized or normalized in _SEMANTIC_QUERY_STOPWORDS:
            continue
        if (
            _CJK_TOKEN_RE.fullmatch(normalized)
            and len(normalized) >= 4
            and any(stopword in normalized for stopword in _CJK_QUERY_STOPWORDS)
            and any(
                other != normalized
                and len(other) >= 2
                and other in normalized
                for other in raw_tokens
            )
        ):
            continue
        if normalized not in output:
            output.append(normalized)
    return output


def query_intent_terms(text: str) -> List[str]:
    """Return bounded answer-shape terms for explicit Chinese question intents.

    These terms are optional retrieval hints, not hard entity or scope signals.
    Keeping the mapping narrow prevents generic Chinese nouns from becoming
    cross-project isolation keys while still connecting location and operating-
    system questions to factual profile wording.
    """

    normalized = clean_text(text).casefold()
    output: list[str] = []
    if _LOCATION_QUERY_RE.search(normalized):
        output.extend(_LOCATION_INTENT_TERMS)
    if _OPERATING_SYSTEM_QUERY_RE.search(normalized):
        output.extend(_OPERATING_SYSTEM_INTENT_TERMS)
    if _TIMEZONE_QUERY_RE.search(normalized):
        output.extend(_TIMEZONE_INTENT_TERMS)
    return list(dict.fromkeys(output))


def matched_query_intent_terms(query: str, document: str) -> List[str]:
    """Return strong answer-shape evidence found in one candidate document.

    Retrieval expansions intentionally include weak context words such as
    ``本机``.  Ranking must not treat those words as proof that a document
    answers an operating-system question, otherwise generic local-task or
    timezone preferences outrank actual runtime-state facts.
    """

    normalized_query = clean_text(query).casefold()
    normalized_document = clean_text(document).casefold()
    terms: list[str] = []
    if _LOCATION_QUERY_RE.search(normalized_query):
        terms.extend(_LOCATION_INTENT_EVIDENCE_TERMS)
    if _OPERATING_SYSTEM_QUERY_RE.search(normalized_query):
        operating_system_terms = [
            term
            for term in _OPERATING_SYSTEM_INTENT_EVIDENCE_TERMS
            if term in normalized_document
        ]
        if operating_system_terms and (
            _CURRENT_STATE_QUERY_RE.search(normalized_document)
            or _OPERATING_SYSTEM_ANSWER_RELATION_RE.search(normalized_document)
        ):
            terms.extend(operating_system_terms)
    if _TIMEZONE_QUERY_RE.search(normalized_query):
        subject_terms = [
            term for term in _TIMEZONE_SUBJECT_TERMS if term in normalized_query
        ]
        subject_matches = not subject_terms or any(
            term in normalized_document for term in subject_terms
        )
        timezone_value = _TIMEZONE_VALUE_RE.search(normalized_document)
        if (
            subject_matches
            and timezone_value is not None
            and _TIMEZONE_ANSWER_RELATION_RE.search(normalized_document)
        ):
            terms.extend(_TIMEZONE_INTENT_TERMS)
            terms.append(timezone_value.group(0).casefold())
    attribute_match = _ATTRIBUTE_QUERY_RE.search(normalized_query)
    if attribute_match is not None:
        subject = attribute_match.group("subject").casefold()
        attribute = attribute_match.group("attribute").casefold()
        answer_relation = re.compile(
            rf"{re.escape(subject)}(?:的)?(?:型号|版本|名称|名字)?\s*(?:是|为|:|：)"
        )
        if subject in normalized_document and answer_relation.search(normalized_document):
            terms.extend((subject, attribute))
    return [term for term in dict.fromkeys(terms) if term in normalized_document]


def query_requests_current_state(text: str) -> bool:
    """Return whether a question asks for present state rather than history."""

    normalized = clean_text(text).casefold()
    if not normalized or _HISTORICAL_STATE_QUERY_RE.search(normalized):
        return False
    return bool(
        _CURRENT_STATE_QUERY_RE.search(normalized)
        or _LOCATION_QUERY_RE.search(normalized)
        or _OPERATING_SYSTEM_QUERY_RE.search(normalized)
        or _TIMEZONE_QUERY_RE.search(normalized)
    )


def retrieval_query_tokens(text: str) -> List[str]:
    """Return semantic query tokens plus bounded optional intent expansions."""

    return list(dict.fromkeys([*semantic_query_tokens(text), *query_intent_terms(text)]))


def stem_token(token: str) -> str:
    if not token.isascii() or not token.isalpha():
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("ing"):
        stem = token[:-3]
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if len(token) > 3 and token.endswith("ed"):
        stem = token[:-2]
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if (
        len(token) > 4
        and token.endswith("es")
        and not token.endswith(("ses", "xes", "zes", "ches", "shes"))
    ):
        return token[:-1]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalized_token_set(tokens: List[str]) -> Set[str]:
    normalized: set[str] = set()
    for token in tokens:
        token = token.lower().strip()
        if not token:
            continue
        normalized.add(token)
        normalized.add(stem_token(token))
    return normalized


def lexical_overlap_details(query: str, *documents: str) -> dict[str, Any]:
    """Explain deterministic lexical coverage using the shared semantic tokenizer."""

    normalized_query = clean_text(query).casefold()
    haystack = " ".join(clean_text(document) for document in documents).casefold()
    if not normalized_query:
        return {
            "score": 1.0,
            "exact_phrase": False,
            "query_tokens": [],
            "matched_tokens": [],
        }
    tokens = semantic_query_tokens(normalized_query)
    if normalized_query in haystack:
        return {
            "score": 1.0,
            "exact_phrase": True,
            "query_tokens": tokens,
            "matched_tokens": list(tokens),
        }
    if not tokens:
        return {
            "score": 0.0,
            "exact_phrase": False,
            "query_tokens": [],
            "matched_tokens": [],
        }

    document_tokens = normalized_token_set(query_tokens(haystack))
    matched_tokens: list[str] = []
    matched_weight = 0
    total_weight = 0
    for token in tokens:
        weight = min(8, max(2, len(token)))
        total_weight += weight
        if token in haystack or stem_token(token) in document_tokens:
            matched_tokens.append(token)
            matched_weight += weight
    score = matched_weight / total_weight if total_weight else 0.0
    return {
        "score": score,
        "exact_phrase": False,
        "query_tokens": tokens,
        "matched_tokens": matched_tokens,
    }


def lexical_overlap_score(query: str, *documents: str) -> float:
    return float(lexical_overlap_details(query, *documents)["score"])


def build_fts_query(tokens: List[str]) -> str:
    safe = [fts_escape(token) for token in tokens if token]
    if not safe:
        return ""
    return " OR ".join(safe[:12])


def like_terms(query: str, tokens: List[str]) -> List[str]:
    terms = tokens[:6]
    if not terms and query:
        terms = [query[:30]]
    return [term for term in terms if term]


def fts_escape(token: str) -> str:
    return '"' + token.replace('"', " ") + '"'


def dedup_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


CAPTURE_SKIP_PATTERNS = [
    re.compile(
        r"review the conversation above and update the skill library", re.IGNORECASE
    ),
    re.compile(r"call the memory tool .*output only the raw json", re.IGNORECASE),
    re.compile(r"reply with ok and nothing else", re.IGNORECASE),
    re.compile(r"^\s*you are an ai assistant", re.IGNORECASE),
    re.compile(r"<available_skills>[\s\S]*?</available_skills>", re.IGNORECASE),
]
SECRET_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|private[_-]?key)\s*[:=]\s*[^\s]+",
    re.IGNORECASE,
)


def should_skip_capture(text: str, config: dict[str, Any] | None = None) -> bool:
    config = config or {}
    text = clean_text(text or "")
    if not text or is_trivial(text):
        return True
    max_chars = int(config.get("capture_hard_max_chars") or 4000)
    if max_chars > 0 and len(text) > max_chars:
        return True
    if SECRET_RE.search(text):
        return True
    for pattern in CAPTURE_SKIP_PATTERNS:
        if pattern.search(text):
            return True
    return False


def config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
