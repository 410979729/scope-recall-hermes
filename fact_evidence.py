"""Deterministic source-specific support checks for structured fact evidence.

Evidence IDs and source roles establish provenance, not entailment.  This module
adds a deliberately conservative lexical boundary: an authoritative quote must
itself anchor the claim value, the subject (or a direct first-person speaker),
and the asserted relation.  Ambiguous, negated, or speculative value clauses
remain review-only rather than borrowing support from unrelated batch text.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .fact_actions import ClaimDraft, EvidenceReference
from .fact_temporal_semantics import (
    clause_has_explicit_current_marker,
    clause_has_transition_event,
    classify_durable_state_clause,
)


DIRECT_EVIDENCE_SOURCE_TYPES = frozenset(
    {
        "direct_user",
        "direct_user_correction",
        "manual_correction",
        "user_message",
    }
)
CORROBORATING_EVIDENCE_SOURCE_TYPES = frozenset(
    {
        "document",
        "external_record",
        "tool_result",
        "verified_profile",
    }
)
INFERRED_EVIDENCE_SOURCE_TYPES = frozenset(
    {
        "model_inference",
        "summary_inference",
    }
)
AUTHORITATIVE_EVIDENCE_SOURCE_TYPES = (
    DIRECT_EVIDENCE_SOURCE_TYPES | CORROBORATING_EVIDENCE_SOURCE_TYPES
)
TRUSTED_EVIDENCE_SOURCE_TYPES = (
    AUTHORITATIVE_EVIDENCE_SOURCE_TYPES | INFERRED_EVIDENCE_SOURCE_TYPES
)

_WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[.;!?。！？；\n]+|\b(?:but|however|although|though)\b|(?:但是|不过|然而|可是))",
    re.IGNORECASE,
)
_MONTH_ABBREVIATION_PERIOD_RE = re.compile(
    r"\b(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.(?=\s|$|[-–—])",
    re.IGNORECASE,
)
_DOTTED_NUMERIC_DATE_RE = re.compile(
    r"\b(\d{1,4})\.(\d{1,2})\.(\d{1,4})\b"
)
_TWO_COMPONENT_DOTTED_DATE_RE = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})(?=\s|$|[-–—])"
)
_FIRST_PERSON_RE = re.compile(
    r"(?:\b(?:i|i'm|im|me|my|mine|myself)\b|我|我的|本人)",
    re.IGNORECASE,
)
_LATIN_NEGATION_RE = re.compile(
    r"(?:\b(?:not|never|no\s+longer|cannot|can['’]?t|couldn['’]?t|"
    r"didn['’]?t|doesn['’]?t|don['’]?t|hasn['’]?t|hadn['’]?t|haven['’]?t|"
    r"isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|won['’]?t|wouldn['’]?t|"
    r"shouldn['’]?t|without|neither|nor)\b)",
    re.IGNORECASE,
)
_CJK_NEGATION_RE = re.compile(
    r"(?:不再|并不|从不|绝不|从未|未曾|没有|没在|没住|"
    r"不住|不喜欢|不拥有|不支持|不需要|不属于|不是|并非|"
    r"无意|无法|不能|不会|不要|别再|莫要|否认)"
)
_UNCERTAINTY_RE = re.compile(
    r"(?:\b(?:may|might|perhaps|possibly|probably|guess|guessed|unverified|"
    r"uncertain|rumou?red|not\s+stated|apparently|seems?|maybe)\b|"
    r"(?:可能|也许|或许|猜测|未经验证|未说明|似乎|据称|据说))",
    re.IGNORECASE,
)
_HISTORICAL_OR_CONDITIONAL_RE = re.compile(
    r"(?:\b(?:used\s+to|formerly|previously|if|unless|would)\b|"
    r"(?:曾经|以前|原来|如果|除非|假如))",
    re.IGNORECASE,
)
_ATTRIBUTION_RE = re.compile(
    r"(?:\b(?:said|says|claimed|claims|reported|reports|told|according\s+to)\b|"
    r"(?:说|表示|声称|转述|据称|据说))",
    re.IGNORECASE,
)
_RETRACTION_CUE_RE = re.compile(
    r"(?:\b(?:no\s+longer|not|never|left|leave|leaving|quit|stopped?|ended?|"
    r"incorrect|wrong|false|retract(?:ed|ion)?|withdraw(?:n)?)\b|"
    r"(?:不再|已经离开|已离开|离职|停止|结束|错误|不正确|撤回|作废))",
    re.IGNORECASE,
)
_PRESENT_AUXILIARY_RE = re.compile(r"\b(?:am|is|are|has|have|does)\b", re.IGNORECASE)
_PROGRESSIVE_CURRENT_PREFIX_RE = re.compile(
    r"\b(?:am|is|are|continues?|keeps?)\s+$",
    re.IGNORECASE,
)
_FIRST_PERSON_CURRENT_PREFIX_RE = re.compile(
    r"\b(?:i|we)\s+(?:(?:currently|now|presently|today|still)\s+)?$",
    re.IGNORECASE,
)
_POST_RELATION_COPULA_RE = re.compile(r"^\s*(?:is|are)\b", re.IGNORECASE)
_FIRST_PERSON_SUBJECT_RE = re.compile(r"^(?:i|we)$", re.IGNORECASE)
_CJK_CURRENT_MODIFIER_RE = re.compile(r"(?:现在|目前|如今|当前|现时|仍然|一直)")
_CJK_PRE_RELATION_ASSERTIVE_MODIFIER_RE = re.compile(r"(?:还|确实|主要|就)")
_CJK_FINAL_ASSERTIVE_PARTICLE_RE = re.compile(r"(?:呀|啊)")
_CJK_GAP_IGNORABLE_RE = re.compile(
    r"[\s,，:：、()（）\[\]【】《》〈〉「」『』“”‘’]*"
)


@dataclass(frozen=True, slots=True)
class _Span:
    """Half-open normalized-text span used for conservative argument ordering."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ClaimAlignedProposition:
    """Claim-aligned projection computed from one quoted value clause.

    A model never supplies these fields. Direct application requires positive
    polarity, a trusted subject binding, relation support, and ordered arguments
    in the same clause.
    """

    clause: str
    polarity: str
    current_state_supported: bool
    subject_supported: bool
    relation_supported: bool
    arguments_aligned: bool


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _stem(token: str) -> str:
    value = _normalized(token).strip("-'_")
    if len(value) > 5 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 4 and value.endswith("ied"):
        value = f"{value[:-3]}y"
    elif len(value) > 4 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 3 and value.endswith("s") and not value.endswith("ss"):
        value = value[:-1]
    return value


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _phrase_spans(text: str, phrase: str) -> tuple[_Span, ...]:
    """Return exact phrase spans with word boundaries for Latin-like entities."""

    normalized_text = _normalized(text)
    normalized_phrase = _normalized(phrase)
    if not normalized_phrase:
        return ()
    if _contains_cjk(normalized_phrase):
        spans: list[_Span] = []
        offset = 0
        while True:
            index = normalized_text.find(normalized_phrase, offset)
            if index < 0:
                break
            spans.append(_Span(index, index + len(normalized_phrase)))
            offset = index + 1
        return tuple(spans)
    pattern = re.compile(
        rf"(?<![\w]){re.escape(normalized_phrase)}(?![\w])",
        re.UNICODE,
    )
    return tuple(_Span(match.start(), match.end()) for match in pattern.finditer(normalized_text))


def _value_clauses(quote: str, display_value: str) -> list[str]:
    normalized_quote = _DOTTED_NUMERIC_DATE_RE.sub(
        r"\1-\2-\3",
        _normalized(quote),
    )
    normalized_quote = _TWO_COMPONENT_DOTTED_DATE_RE.sub(
        r"\1-\2",
        normalized_quote,
    )
    normalized_quote = _MONTH_ABBREVIATION_PERIOD_RE.sub(
        r"\1",
        normalized_quote,
    )
    return [
        clause.strip()
        for clause in _CLAUSE_SPLIT_RE.split(normalized_quote)
        if _phrase_spans(clause, display_value)
    ]


def _claim_frame_context(clause: str, claim: ClaimDraft) -> str:
    """Mask aligned value spans before classifying predicate-frame semantics.

    A title or object may legitimately contain words such as ``不是``, ``if``,
    or a year. Those tokens describe the value, not the speaker's polarity or
    temporal stance toward the asserted relation.
    """

    normalized_clause = _normalized(clause)
    characters = list(normalized_clause)
    for span in _phrase_spans(normalized_clause, claim.display_value):
        characters[span.start : span.end] = " " * (span.end - span.start)
    return "".join(characters)


def _subject_spans(
    quote: str,
    claim: ClaimDraft,
    *,
    evidence: EvidenceReference,
) -> tuple[_Span, ...]:
    normalized_quote = _normalized(quote)
    explicit = _phrase_spans(normalized_quote, claim.subject)
    if explicit:
        return explicit
    speaker_subject = _normalized(evidence.speaker_subject)
    if not (
        _normalized(evidence.source_type) in DIRECT_EVIDENCE_SOURCE_TYPES
        and bool(speaker_subject)
        and speaker_subject == _normalized(claim.subject)
        and _ATTRIBUTION_RE.search(normalized_quote) is None
    ):
        return ()
    return tuple(
        _Span(match.start(), match.end())
        for match in _FIRST_PERSON_RE.finditer(normalized_quote)
    )


def _relation_spans(quote: str, claim: ClaimDraft) -> tuple[_Span, ...]:
    """Return predicate-frame spans without semantic-family authorization.

    Latin frames keep every token, including prepositions, while allowing only
    morphological variants of the same token (``work``/``works``). CJK
    predicates require the exact normalized phrase. Broader relation families
    belong to retrieval/review hints and must never grant durable authority.
    """

    normalized_quote = _normalized(quote)
    normalized_predicate = _normalized(claim.predicate)
    exact = _phrase_spans(normalized_quote, normalized_predicate)
    if exact or _contains_cjk(normalized_predicate):
        return exact

    predicate_tokens = tuple(_WORD_RE.finditer(normalized_predicate))
    quote_tokens = tuple(_WORD_RE.finditer(normalized_quote))
    predicate_stems = tuple(_stem(match.group(0)) for match in predicate_tokens)
    # Only morphological variants of the *same* predicate frame may authorize a
    # durable claim. Events such as move/relocate can be useful retrieval hints,
    # but they do not entail the current state "lives in".
    authority_frames = [predicate_stems]
    spans: list[_Span] = []
    for authority_frame in authority_frames:
        frame_size = len(authority_frame)
        if frame_size == 0 or len(quote_tokens) < frame_size:
            continue
        for index in range(len(quote_tokens) - frame_size + 1):
            frame = quote_tokens[index : index + frame_size]
            if tuple(_stem(match.group(0)) for match in frame) != authority_frame:
                continue
            spans.append(_Span(frame[0].start(), frame[-1].end()))
    return tuple(spans)


def _latin_predicate_looks_present(
    predicate: str,
    subject: str,
    *,
    explicit_current_marker: bool,
) -> bool:
    tokens = [match.group(0) for match in _WORD_RE.finditer(_normalized(predicate))]
    if not tokens:
        return False
    if any(_PRESENT_AUXILIARY_RE.fullmatch(token) for token in tokens):
        return True
    first = tokens[0]
    if first.endswith("s") and not first.endswith("ss"):
        return True
    if (
        (_FIRST_PERSON_SUBJECT_RE.fullmatch(_normalized(subject)) or explicit_current_marker)
        and not first.endswith(("ed", "ing"))
    ):
        return True
    return False


def _relation_frame_supports_current_state(clause: str, claim: ClaimDraft) -> bool:
    frame_context = _claim_frame_context(clause, claim)
    temporal_class = classify_durable_state_clause(frame_context)
    if temporal_class not in {"current_durable", "unknown"}:
        return False

    normalized_clause = _normalized(clause)
    normalized_predicate = _normalized(claim.predicate)
    relation_spans = _relation_spans(normalized_clause, claim)
    exact_spans = set(_phrase_spans(normalized_clause, normalized_predicate))
    explicit_marker = clause_has_explicit_current_marker(frame_context)
    if _contains_cjk(normalized_predicate):
        # Exact CJK predicate text carries no grammatical tense. Require the
        # complete ordered frame and allow only punctuation/current markers in
        # every gap, including before the subject and after the value. Unknown
        # temporal adverbials therefore remain review-only even when they are
        # absent from the historical marker lexicon.
        subjects = _phrase_spans(normalized_clause, claim.subject)
        values = _phrase_spans(normalized_clause, claim.display_value)
        return any(
            subject.end <= relation.start
            and relation.end <= value.start
            and _cjk_prefix_gap_is_ignorable(
                normalized_clause[: subject.start]
            )
            and _cjk_subject_relation_gap_is_ignorable(
                normalized_clause[subject.end : relation.start]
            )
            and _cjk_relation_value_gap_is_ignorable(
                normalized_clause[relation.end : value.start]
            )
            and _cjk_suffix_gap_is_ignorable(
                normalized_clause[value.end :]
            )
            for subject in subjects
            for relation in relation_spans
            for value in values
        )
    for span in relation_spans:
        if span in exact_spans:
            if _latin_predicate_looks_present(
                normalized_predicate,
                claim.subject,
                explicit_current_marker=explicit_marker,
            ):
                return True
            if _POST_RELATION_COPULA_RE.search(normalized_clause[span.end :]):
                return True
        prefix = normalized_clause[max(0, span.start - 32) : span.start]
        if _PROGRESSIVE_CURRENT_PREFIX_RE.search(prefix):
            return True
        surface_tokens = [
            match.group(0)
            for match in _WORD_RE.finditer(normalized_clause[span.start : span.end])
        ]
        if (
            surface_tokens
            and _FIRST_PERSON_CURRENT_PREFIX_RE.search(prefix)
            and not surface_tokens[0].endswith(("ed", "ing"))
        ):
            return True
    return False


def _cjk_gap_is_ignorable(gap: str, *modifiers: re.Pattern[str]) -> bool:
    remainder = _normalized(gap)
    for modifier in modifiers:
        remainder = modifier.sub("", remainder)
    return _CJK_GAP_IGNORABLE_RE.fullmatch(remainder) is not None


def _cjk_prefix_gap_is_ignorable(gap: str) -> bool:
    return _cjk_gap_is_ignorable(gap, _CJK_CURRENT_MODIFIER_RE)


def _cjk_subject_relation_gap_is_ignorable(gap: str) -> bool:
    return _cjk_gap_is_ignorable(
        gap,
        _CJK_CURRENT_MODIFIER_RE,
        _CJK_PRE_RELATION_ASSERTIVE_MODIFIER_RE,
    )


def _cjk_relation_value_gap_is_ignorable(gap: str) -> bool:
    return _cjk_gap_is_ignorable(gap, _CJK_CURRENT_MODIFIER_RE)


def _cjk_suffix_gap_is_ignorable(gap: str) -> bool:
    return _cjk_gap_is_ignorable(
        gap,
        _CJK_CURRENT_MODIFIER_RE,
        _CJK_FINAL_ASSERTIVE_PARTICLE_RE,
    )


def _arguments_aligned(
    quote: str,
    claim: ClaimDraft,
    *,
    evidence: EvidenceReference,
) -> bool:
    """Require an active-voice subject→relation→value ordering in one clause.

    Unsupported passive or elliptical wording intentionally falls back to review;
    lexical co-occurrence must never infer argument roles.
    """

    subjects = _subject_spans(quote, claim, evidence=evidence)
    relations = _relation_spans(quote, claim)
    values = _phrase_spans(quote, claim.display_value)
    if _contains_cjk(claim.subject) or _contains_cjk(claim.display_value):
        normalized_quote = _normalized(quote)
        return any(
            subject.end <= relation.start
            and relation.end <= value.start
            and _cjk_subject_relation_gap_is_ignorable(
                normalized_quote[subject.end : relation.start]
            )
            and _cjk_relation_value_gap_is_ignorable(
                normalized_quote[relation.end : value.start]
            )
            for subject in subjects
            for relation in relations
            for value in values
        )
    return any(
        subject.end <= relation.start and relation.end <= value.start
        for subject in subjects
        for relation in relations
        for value in values
    )


def _subject_supported(
    quote: str,
    claim: ClaimDraft,
    *,
    evidence: EvidenceReference,
) -> bool:
    return bool(_subject_spans(quote, claim, evidence=evidence))


def _relation_supported(quote: str, claim: ClaimDraft) -> bool:
    return bool(_relation_spans(quote, claim))


def _clause_polarity(clause: str, claim: ClaimDraft) -> str:
    frame_context = _claim_frame_context(clause, claim)
    if (
        _LATIN_NEGATION_RE.search(frame_context) is not None
        or _CJK_NEGATION_RE.search(frame_context) is not None
    ):
        return "negative_or_ambiguous"
    temporal_class = classify_durable_state_clause(frame_context)
    if (
        _UNCERTAINTY_RE.search(frame_context) is not None
        or _HISTORICAL_OR_CONDITIONAL_RE.search(frame_context) is not None
        or temporal_class in {"past", "future", "temporary", "conditional"}
    ):
        return "unknown"
    return "positive"


def _claim_aligned_propositions(
    evidence: EvidenceReference,
    claim: ClaimDraft,
) -> tuple[_ClaimAlignedProposition, ...]:
    return tuple(
        _ClaimAlignedProposition(
            clause=clause,
            polarity=_clause_polarity(clause, claim),
            current_state_supported=(
                not clause_has_transition_event(_claim_frame_context(clause, claim))
                and _relation_frame_supports_current_state(clause, claim)
            ),
            subject_supported=_subject_supported(
                clause,
                claim,
                evidence=evidence,
            ),
            relation_supported=_relation_supported(clause, claim),
            arguments_aligned=_arguments_aligned(
                clause,
                claim,
                evidence=evidence,
            ),
        )
        for clause in _value_clauses(evidence.quote, claim.display_value)
    )


def evidence_supports_claim(
    evidence: EvidenceReference,
    claim: ClaimDraft,
) -> bool:
    """Return whether this exact authoritative quote supports ``claim``.

    The check intentionally does not inspect sibling evidence or surrounding
    batch text.  That prevents a valid but unrelated user quote from laundering
    an assistant inference into direct authority.
    """

    source_type = _normalized(evidence.source_type)
    if source_type not in AUTHORITATIVE_EVIDENCE_SOURCE_TYPES:
        return False
    quote = str(evidence.quote or "").strip()
    if not quote:
        return False

    return any(
        proposition.polarity == "positive"
        and proposition.current_state_supported
        and proposition.subject_supported
        and proposition.relation_supported
        and proposition.arguments_aligned
        for proposition in _claim_aligned_propositions(evidence, claim)
    )


def evidence_supports_retraction(
    evidence: EvidenceReference,
    target_claim: ClaimDraft,
) -> bool:
    """Return whether a quote explicitly retracts the runtime-bound target claim."""

    source_type = _normalized(evidence.source_type)
    if source_type not in AUTHORITATIVE_EVIDENCE_SOURCE_TYPES:
        return False
    quote = str(evidence.quote or "").strip()
    if not quote:
        return False
    return any(
        proposition.polarity != "unknown"
        and proposition.subject_supported
        and proposition.relation_supported
        and proposition.arguments_aligned
        and _RETRACTION_CUE_RE.search(
            _claim_frame_context(proposition.clause, target_claim)
        )
        is not None
        for proposition in _claim_aligned_propositions(evidence, target_claim)
    )


__all__ = [
    "AUTHORITATIVE_EVIDENCE_SOURCE_TYPES",
    "CORROBORATING_EVIDENCE_SOURCE_TYPES",
    "DIRECT_EVIDENCE_SOURCE_TYPES",
    "INFERRED_EVIDENCE_SOURCE_TYPES",
    "TRUSTED_EVIDENCE_SOURCE_TYPES",
    "evidence_supports_claim",
    "evidence_supports_retraction",
]
