"""Conservative temporal semantics for durable fact evidence.

Lexical predicate alignment is not enough to establish a *current durable state*.
This module rejects clauses that are explicitly past, future, conditional, finite,
or temporary.  Unsupported constructions remain review-only; callers must never
interpret ``unknown`` as durable authority.
"""

from __future__ import annotations

import re
import unicodedata


_FUTURE_RE = re.compile(
    r"(?:\b(?:will|shall|next\s+(?:day|week|month|year|summer|winter|spring|fall|autumn)|"
    r"tomorrow|later|plans?\s+to|intends?\s+to|going\s+to)\b|"
    r"(?:将会|将要|明天|下周|下个月|明年|以后|计划))",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(
    r"(?:\b(?:used\s+to|formerly|previously|"
    r"last\s+(?:day|week|month|year|spring|summer|fall|autumn|winter|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"yesterday|ago|in\s+(?:19|20)\d{2}|"
    r"(?:was|were)\s+[a-z][a-z'-]*ing|"
    r"(?:spring|summer|fall|autumn|winter)\s+(?:19|20)\d{2})\b|"
    r"(?:曾经|曾|以前|过去|原来|去年|上个月|上周|昨天|\d{4}\s*年|的时候|"
    r"当时|那时候|那时|彼时|起初|早年|一度|之前|此前|先前|前阵子|"
    r"前些日子|小时候|童年时|从前|昔日|往日)|"
    r"(?<=[\u3400-\u9fff])过(?:了|\s|[,，。！？]|$))",
    re.IGNORECASE,
)
_CURRENT_STATE_MARKER_RE = re.compile(
    r"(?:\b(?:currently|presently|now|still|today|at\s+present)\b|"
    r"(?:现在|目前|如今|当前|现时|仍然|一直))",
    re.IGNORECASE,
)
_TEMPORARY_RE = re.compile(
    r"(?:\b(?:temporar(?:y|ily)|"
    r"(?:for|during|over|throughout)\s+(?:the\s+)?(?:summer|winter|spring|fall|autumn)|"
    r"this\s+(?:summer|winter|spring|fall|autumn)|"
    r"(?:in|during)\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"for\s+(?:an?\s+|one\s+|two\s+|three\s+|\d+[-\s]?)?"
    r"(?:day|week|month|year)s?(?:[-\s](?:long|contract|conference|project))?|"
    r"two[-\s]week|short[-\s]term|fixed[-\s]term|on\s+(?:a\s+)?vacation|"
    r"on\s+(?:a\s+)?trip|for\s+now|for\s+the\s+time\s+being|probation(?:ary)?|"
    r"conference|seasonal(?:ly)?|until)\b|"
    r"(?:暂时|临时|短期|度假|出差|会议期间|暑假|寒假|直到))",
    re.IGNORECASE,
)
_PAST_STATE_PREDICATE_RE = re.compile(
    r"\b(?:worked|lived|resided|stayed|preferred|liked|used|had|was|were)\b|"
    r"\bdid\s+(?:work|live|reside|stay|prefer|like|use|have)\b|"
    r"\b(?:has|have|had)\s+(?:been\s+)?(?:working|living|residing|staying|preferring|using|worked|lived|preferred|used)\b",
    re.IGNORECASE,
)
_TIME_ENDPOINT = (
    r"(?:jan(?:uary)?\.?|feb(?:ruary)?\.?|mar(?:ch)?\.?|apr(?:il)?\.?|may|"
    r"jun(?:e)?\.?|jul(?:y)?\.?|aug(?:ust)?\.?|sep(?:t(?:ember)?)?\.?|"
    r"oct(?:ober)?\.?|nov(?:ember)?\.?|dec(?:ember)?\.?|"
    r"spring|summer|fall|autumn|winter|(?:19|20)\d{2})"
)
_FINITE_RANGE_RE = re.compile(
    rf"(?:\b(?:from\s+{_TIME_ENDPOINT}\s+(?:to|through|until|[-–—])\s*{_TIME_ENDPOINT}|"
    rf"between\s+{_TIME_ENDPOINT}\s+and\s+{_TIME_ENDPOINT}|"
    rf"{_TIME_ENDPOINT}\s+(?:to|through|until)\s+{_TIME_ENDPOINT}|"
    rf"{_TIME_ENDPOINT}\s*[-–—]\s*{_TIME_ENDPOINT}|"
    rf"(?:until|through)\s+(?:the\s+end\s+of\s+)?{_TIME_ENDPOINT})\b|"
    r"(?:从\s*(?:\d{1,4}\s*年\s*)?(?:\d{1,2}|[一二三四五六七八九十]+)\s*月?\s*"
    r"(?:到|至|[-–—])\s*(?:\d{1,4}\s*年\s*)?(?:\d{1,2}|[一二三四五六七八九十]+)\s*月?))",
    re.IGNORECASE,
)
_DURATION_NUMBER = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"few|several|some|half|another|couple(?:\s+of)?|\d+)"
)
_FINITE_DURATION_RE = re.compile(
    rf"(?:\bfor\s+(?:(?:the[-\s]+(?:next|past)|another|about|around|approximately|roughly|"
    rf"up[-\s]*to|at[-\s]+(?:most|least)|no[-\s]+more[-\s]+than|"
    rf"less[-\s]+than|more[-\s]+than)\s+)?(?:"
    rf"(?:an?\s+(?:{_DURATION_NUMBER}[-\s]+)?|{_DURATION_NUMBER}[-\s]+|half\s+(?:an?\s+)?)"
    r"(?:day|week|month|year|season|semester|quarter)s?"
    r"(?:[-\s]+(?:long|contract|assignment|engagement|placement|project|role|internship))?"
    r")\b|"
    rf"\b(?:an?\s+)?{_DURATION_NUMBER}[-\s]+"
    r"(?:day|week|month|year|season|semester|quarter)s?[-\s]+"
    r"(?:contract|assignment|engagement|placement|project|role|internship)\b|"
    r"(?:为期\s*(?:\d+|[一二三四五六七八九十百]+)\s*(?:天|周|个月|月|年|季度|学期)|"
    r"(?:工作|任职|合同期?)\s*(?:\d+|[一二三四五六七八九十百]+)\s*(?:天|周|个月|月|年)))",
    re.IGNORECASE,
)
_CONTRACT_TERM_RE = re.compile(
    r"(?:\b(?:on|under)\s+(?:an?\s+)?(?:fixed[-\s]term\s+)?"
    r"(?:contract|assignment|engagement|placement|project|internship)\b|"
    r"\b(?:contract|assignment|engagement|placement|project|internship)\s+"
    r"(?:through|until|for)\b|(?:合同工|合同期|项目期间|实习期间))",
    re.IGNORECASE,
)
_DURATION_UNIT = r"(?:day|week|month|year|season|semester|quarter)"
_FINITE_DURATION_ADJACENT_RE = re.compile(
    rf"(?:\bfor\s+(?:"
    rf"(?:only|just|(?:the[-\s]+)?coming|under|over|fewer[-\s]+than)\s+"
    rf"(?:(?:an?|{_DURATION_NUMBER})[-\s]+)?{_DURATION_UNIT}s?|"
    rf"(?:an?[-\s]+)?(?:maximum|minimum)[-\s]+of[-\s]+"
    rf"(?:an?[-\s]+|{_DURATION_NUMBER}[-\s]+)?{_DURATION_UNIT}s?|"
    rf"(?:a|the)[-\s]+(?:period|term)[-\s]+of[-\s]+"
    rf"(?:an?[-\s]+|{_DURATION_NUMBER}[-\s]+)?{_DURATION_UNIT}s?|"
    rf"{_DURATION_NUMBER}[-\s]+more[-\s]+{_DURATION_UNIT}s?|"
    rf"{_DURATION_NUMBER}\s*(?:to|through|[-–—])\s*"
    rf"{_DURATION_NUMBER}[-\s]+{_DURATION_UNIT}s?"
    rf")\b|"
    rf"\bfor\s+the[-\s]+(?:rest|remainder)[-\s]+of[-\s]+"
    rf"(?:the[-\s]+)?{_DURATION_UNIT}\b)",
    re.IGNORECASE,
)
_DAY_TOKEN = r"(?:\d{1,2}(?:st|nd|rd|th)?)"
_YEAR_TOKEN = r"(?:(?:19|20)\d{2})"
_MONTH_DAY_ENDPOINT = rf"(?:{_TIME_ENDPOINT}\s+{_DAY_TOKEN}(?:,?\s+{_YEAR_TOKEN})?)"
_DAY_MONTH_ENDPOINT = rf"(?:{_DAY_TOKEN}\s+{_TIME_ENDPOINT}(?:\s+{_YEAR_TOKEN})?)"
_YEAR_MONTH_DAY_ENDPOINT = rf"(?:{_YEAR_TOKEN}\s+{_TIME_ENDPOINT}\s+{_DAY_TOKEN})"
_MONTH_YEAR_ENDPOINT = rf"(?:{_TIME_ENDPOINT}\s+{_YEAR_TOKEN}|{_YEAR_TOKEN}\s+{_TIME_ENDPOINT})"
_NUMERIC_DATE_ENDPOINT = (
    r"(?:\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|"
    r"\d{1,2}[-/.]\d{1,2}(?:[-/.](?:\d{2}|\d{4}))?)"
)
_DATE_ENDPOINT = rf"(?:{_YEAR_MONTH_DAY_ENDPOINT}|{_MONTH_DAY_ENDPOINT}|{_DAY_MONTH_ENDPOINT}|{_MONTH_YEAR_ENDPOINT}|{_NUMERIC_DATE_ENDPOINT})"
_FINITE_DATE_RANGE_RE = re.compile(
    rf"(?:\b(?:from\s+)?{_DATE_ENDPOINT}\s*(?:to|through|until|[-–—])\s*"
    rf"{_DATE_ENDPOINT}\b|"
    rf"\bbetween\s+{_DATE_ENDPOINT}\s+and\s+{_DATE_ENDPOINT}\b)",
    re.IGNORECASE,
)
_GENERIC_BOUNDED_RANGE_RE = re.compile(
    r"(?:\bfrom\b[^.;!?。！？\n]{1,72}?\b(?:to|through|until)\b"
    r"[^.;!?。！？\n]{1,72}|"
    r"\bbetween\b[^.;!?。！？\n]{1,72}?\band\b[^.;!?。！？\n]{1,72})",
    re.IGNORECASE,
)
_DURATION_WINDOW_RE = re.compile(
    r"\bfor\b(?P<body>.{0,96}?)\b(?:"
    r"days?|weeks?|months?|years?|quarters?|semesters?|seasons?|"
    r"dys?|wks?|mos?|mons?|mths?|mnths?|yrs?|qtrs?|sems?"
    r")\b",
    re.IGNORECASE,
)
_DURATION_QUANTIFIER_RE = re.compile(
    r"\b(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|few|several|some|half|another|couple|next|past|coming|only|just|"
    r"under|over|less|fewer|more|up|at|no|not|max|maximum|min|minimum|rest|"
    r"remainder|period|term|\d+)\b",
    re.IGNORECASE,
)
_DURATION_HYPHEN_RE = re.compile(r"[-‐‑‒–—]+")
_CONDITIONAL_RE = re.compile(
    r"(?:\b(?:if|unless|assuming|provided\s+that|would|could)\b|"
    r"(?:如果|除非|假如|若|可能会))",
    re.IGNORECASE,
)
_TRANSITION_EVENT_RE = re.compile(
    r"(?:\b(?:"
    r"start(?:ed|ing)?|begin(?:ning)?|began|begun|"
    r"commenc(?:e|ed|ing)|initiat(?:e|ed|ing)|"
    r"stop(?:ped|ping)?|finish(?:ed|ing)?|end(?:ed|ing)?|"
    r"ceas(?:e|ed|ing)|discontinu(?:e|ed|ing)|complet(?:e|ed|ing)|"
    r"tr(?:y|ied|ying)|attempt(?:ed|ing)?|"
    r"consider(?:ed|ing)?|contemplat(?:e|ed|ing)|"
    r"return(?:ed|ing)?|resum(?:e|ed|ing)|paus(?:e|ed|ing)|"
    r"leave|left|leaving|quit(?:ting)?|"
    r"abandon(?:ed|ing)?|terminat(?:e|ed|ing)|depart(?:ed|ing)?|"
    r"join(?:ed|ing)?|move(?:d|ing)?|relocat(?:e|ed|ing)|"
    r"switch(?:ed|ing)?|shift(?:ed|ing)?|visit(?:ed|ing)?"
    r")\b|(?:开始|着手|停止|结束|尝试|考虑|恢复|暂停|离开|离职|加入|搬到|迁往|访问))",
    re.IGNORECASE,
)


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _has_bounded_duration(normalized_clause: str) -> bool:
    """Detect quantified ``for ... duration-unit`` windows conservatively."""

    for match in _DURATION_WINDOW_RE.finditer(normalized_clause):
        body = _DURATION_HYPHEN_RE.sub(" ", match.group("body"))
        if _DURATION_QUANTIFIER_RE.search(body):
            return True
    return False


def clause_has_explicit_current_marker(clause: str) -> bool:
    """Return whether a clause explicitly anchors itself to the present."""

    return _CURRENT_STATE_MARKER_RE.search(_normalized(clause)) is not None


def clause_has_transition_event(clause: str) -> bool:
    """Return whether ``clause`` describes a transition/event, not a state.

    Callers use this as an additional gate for *adding* current durable truth.
    Retraction evidence deliberately remains free to use transition cues such as
    ``stopped`` and ``left``.
    """

    return _TRANSITION_EVENT_RE.search(_normalized(clause)) is not None


def classify_durable_state_clause(clause: str) -> str:
    """Classify a clause's temporal suitability for current durable authority.

    The result is one of ``current_durable``, ``past``, ``future``,
    ``temporary``, ``conditional``, or ``unknown``.  The classifier only grants
    the current state only for an explicit present-time marker. Claim-aware
    predicate-frame validation may additionally authorize grammatical present
    state forms; an otherwise unclassified clause remains ``unknown``.
    """

    normalized = _normalized(clause)
    if not normalized:
        return "unknown"
    if _CONDITIONAL_RE.search(normalized):
        return "conditional"
    if _FUTURE_RE.search(normalized):
        return "future"
    if (
        _TEMPORARY_RE.search(normalized)
        or _FINITE_RANGE_RE.search(normalized)
        or _FINITE_DURATION_RE.search(normalized)
        or _FINITE_DURATION_ADJACENT_RE.search(normalized)
        or _has_bounded_duration(normalized)
        or _FINITE_DATE_RANGE_RE.search(normalized)
        or _GENERIC_BOUNDED_RANGE_RE.search(normalized)
        or _CONTRACT_TERM_RE.search(normalized)
    ):
        return "temporary"
    if _HISTORICAL_RE.search(normalized) or _PAST_STATE_PREDICATE_RE.search(normalized):
        return "past"
    if _CURRENT_STATE_MARKER_RE.search(normalized):
        return "current_durable"
    return "unknown"


__all__ = [
    "clause_has_explicit_current_marker",
    "clause_has_transition_event",
    "classify_durable_state_clause",
]
