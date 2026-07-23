"""Citation-bound grounding checks for durable reflection candidates.

The reflection LLM contract validates citation identifiers, but a valid ID does
not prove that the cited evidence entails the generated text.  This module adds
a conservative deterministic lexical-and-structural boundary used only before
persistence. Read-only reflection responses may remain abstractive; durable review
candidates contain supported observations only.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .reflection import ReflectionEvidence
from .reflection_llm import (
    MAX_REFLECTION_ANSWER_CHARS,
    ReflectionStatement,
    ReflectionSynthesis,
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*|[\u4e00-\u9fff]+")
_STOP_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "available",
        "based",
        "be",
        "been",
        "by",
        "design",
        "evidence",
        "for",
        "from",
        "identifies",
        "identify",
        "in",
        "indicate",
        "indicates",
        "is",
        "it",
        "of",
        "on",
        "one",
        "region",
        "source",
        "sources",
        "storage",
        "support",
        "supports",
        "that",
        "the",
        "these",
        "this",
        "those",
        "together",
        "was",
        "were",
        "with",
    }
)
_MIN_COVERAGE = 1.0
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[.;!?。！？；\n]+|\b(?:and|but|however|whereas)\b|(?:并且|而且|但是|不过|然而|可是))",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(?:\b(?:not|never|no|cannot|can['’]?t|don['’]?t|doesn['’]?t|"
    r"didn['’]?t|isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|"
    r"won['’]?t|wouldn['’]?t|shouldn['’]?t|haven['’]?t|hasn['’]?t|"
    r"hadn['’]?t|without|neither|nor)\b|(?:不|没|未|无|非|否|别|莫))",
    re.IGNORECASE,
)
_SEMANTIC_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "conditional",
        re.compile(
            r"(?:\b(?:if|unless|provided|assuming|when)\b|(?:如果|若|除非|假如|条件是))",
            re.IGNORECASE,
        ),
    ),
    (
        "historical",
        re.compile(
            r"(?:\b(?:used\s+to|formerly|previously|historically)\b|(?:曾经|以前|此前|过去))",
            re.IGNORECASE,
        ),
    ),
    (
        "future",
        re.compile(
            r"(?:\b(?:will|shall|plans?\s+to|scheduled\s+to)\b|(?:将会|将要|计划|预定))",
            re.IGNORECASE,
        ),
    ),
    (
        "uncertain",
        re.compile(
            r"(?:\b(?:may|might|could|perhaps|possibly|likely|unlikely)\b|(?:可能|也许|或许|大概))",
            re.IGNORECASE,
        ),
    ),
    (
        "current",
        re.compile(
            r"(?:\b(?:now|currently|today|as\s+of)\b|(?:当前|目前|现在|截至))",
            re.IGNORECASE,
        ),
    ),
    (
        "reported",
        re.compile(
            r"(?:\b(?:said|says|claimed|claims|reported|reports|according\s+to)\b|(?:声称|据称|据说|表示|转述))",
            re.IGNORECASE,
        ),
    ),
)
_QUANTIFIER_RE = re.compile(
    r"(?:\b(?:all|every|each|only|none|some|many|most|few|at\s+least|at\s+most)\b|"
    r"(?:全部|所有|每个|仅|只有|没有任何|一些|多数|至少|至多))",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TextGrounding:
    """Bounded lexical support result for one citation-bound fragment."""

    supported: bool
    coverage: float
    content_token_count: int
    unsupported_token_count: int
    structural_supported: bool = False


@dataclass(frozen=True, slots=True)
class CandidateGrounding:
    """Safe candidate material or a stable fail-closed reason."""

    synthesis: ReflectionSynthesis | None
    reason: str
    answer: TextGrounding
    observation_count: int
    supported_observation_count: int


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _stem(token: str) -> str:
    value = _normalized(token).strip("_'\"")
    if len(value) > 7 and value.endswith("ation"):
        value = value[:-3]
    elif len(value) > 6 and value.endswith("ment"):
        value = value[:-4]
    elif len(value) > 5 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 4 and value.endswith("ied"):
        value = f"{value[:-3]}y"
    elif len(value) > 4 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 3 and value.endswith("s") and not value.endswith("ss"):
        value = value[:-1]
    return value


def _parts(raw_token: str) -> list[str]:
    return [part for part in _normalized(raw_token).split("-") if part]


def _content_tokens(text: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        for part in _parts(raw):
            stem = _stem(part)
            if (
                stem
                and _normalized(part) not in _STOP_TOKENS
                and stem not in _STOP_TOKENS
            ):
                output.append((part, stem))
    return output


def _token_supported(token: str, evidence_tokens: set[str]) -> bool:
    return token in evidence_tokens


def _critical_raw_tokens(text: str) -> list[str]:
    output: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        if any(character.isdigit() for character in raw):
            output.append(raw)
            continue
        if any(character.isupper() for character in raw[1:]):
            output.append(raw)
    return output


def _critical_supported(raw: str, evidence_text: str) -> bool:
    normalized_raw = _normalized(raw)
    normalized_evidence = _normalized(evidence_text)
    if normalized_raw in normalized_evidence:
        return True
    parts = [_stem(part) for part in _parts(raw)]
    evidence_tokens = {stem for _part, stem in _content_tokens(evidence_text)}
    return bool(parts) and all(_token_supported(part, evidence_tokens) for part in parts)


@dataclass(frozen=True, slots=True)
class _SemanticSignature:
    polarity: str
    markers: frozenset[str]
    quantifiers: frozenset[str]


def _semantic_signature(clause: str) -> _SemanticSignature | None:
    negations = list(_NEGATION_RE.finditer(clause))
    # Multiple negative operators require a real entailment engine.  Treat them
    # as ambiguous rather than guessing whether they cancel.
    if len(negations) > 1:
        return None
    markers = frozenset(
        name for name, pattern in _SEMANTIC_MARKERS if pattern.search(clause)
    )
    quantifiers = frozenset(
        " ".join(_normalized(match.group(0)).split())
        for match in _QUANTIFIER_RE.finditer(clause)
    )
    return _SemanticSignature(
        polarity="negative" if negations else "positive",
        markers=markers,
        quantifiers=quantifiers,
    )


def _clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in _CLAUSE_SPLIT_RE.split(str(text or ""))
        if clause.strip()
    ]


def _ordered_subsequence(needles: list[str], haystack: list[str]) -> bool:
    if not needles:
        return False
    position = 0
    for needle in needles:
        while position < len(haystack) and haystack[position] != needle:
            position += 1
        if position >= len(haystack):
            return False
        position += 1
    return True


def _structural_supported(
    text: str,
    cited: list[ReflectionEvidence],
) -> bool:
    """Require each output proposition to match one cited content clause.

    Evidence summaries remain useful for lexical recall, but they cannot alone
    authorize a proposition because a summary may omit negation, modality, or
    argument order from the authoritative content.
    """

    evidence_clauses: list[tuple[list[str], _SemanticSignature]] = []
    for item in cited:
        for clause in _clauses(item.content):
            signature = _semantic_signature(clause)
            tokens = [stem for _raw, stem in _content_tokens(clause)]
            if signature is not None and tokens:
                evidence_clauses.append((tokens, signature))
    if not evidence_clauses:
        return False

    statement_count = 0
    for clause in _clauses(text):
        tokens = [stem for _raw, stem in _content_tokens(clause)]
        if not tokens:
            continue
        statement_count += 1
        signature = _semantic_signature(clause)
        if signature is None:
            return False
        if not any(
            signature == evidence_signature
            and _ordered_subsequence(tokens, evidence_tokens)
            for evidence_tokens, evidence_signature in evidence_clauses
        ):
            return False
    return statement_count > 0


def text_grounding(
    text: str,
    *,
    citations: tuple[str, ...],
    evidence_by_id: dict[str, ReflectionEvidence],
) -> TextGrounding:
    """Measure whether one fragment is supported by the cited evidence only."""

    cited = [evidence_by_id[item] for item in citations if item in evidence_by_id]
    if len(cited) != len(citations) or not cited:
        return TextGrounding(False, 0.0, 0, 0)
    evidence_text = "\n".join(
        f"{item.content}\n{item.summary}" for item in cited
    )
    evidence_tokens = {stem for _part, stem in _content_tokens(evidence_text)}
    content_tokens = _content_tokens(text)
    if not content_tokens:
        return TextGrounding(False, 0.0, 0, 0)
    unsupported = [
        token
        for _raw, token in content_tokens
        if not _token_supported(token, evidence_tokens)
    ]
    coverage = (len(content_tokens) - len(unsupported)) / len(content_tokens)
    critical_supported = all(
        _critical_supported(raw, evidence_text)
        for raw in _critical_raw_tokens(text)
    )
    structural_supported = _structural_supported(text, cited)
    return TextGrounding(
        supported=(
            coverage >= _MIN_COVERAGE
            and critical_supported
            and structural_supported
        ),
        coverage=round(coverage, 4),
        content_token_count=len(content_tokens),
        unsupported_token_count=len(unsupported),
        structural_supported=structural_supported,
    )


def grounded_candidate_synthesis(
    synthesis: ReflectionSynthesis,
    evidence: tuple[ReflectionEvidence, ...],
) -> CandidateGrounding:
    """Build a durable candidate from citation-bound observations only."""

    evidence_by_id = {item.evidence_id: item for item in evidence}
    answer_grounding = text_grounding(
        synthesis.answer,
        citations=synthesis.citations,
        evidence_by_id=evidence_by_id,
    )
    if not answer_grounding.supported:
        return CandidateGrounding(
            synthesis=None,
            reason="unsupported_answer",
            answer=answer_grounding,
            observation_count=len(synthesis.observations),
            supported_observation_count=0,
        )

    supported_observations: list[ReflectionStatement] = []
    for statement in synthesis.observations:
        grounding = text_grounding(
            statement.text,
            citations=statement.citations,
            evidence_by_id=evidence_by_id,
        )
        if not grounding.supported:
            return CandidateGrounding(
                synthesis=None,
                reason="unsupported_observation",
                answer=answer_grounding,
                observation_count=len(synthesis.observations),
                supported_observation_count=len(supported_observations),
            )
        supported_observations.append(statement)
    if not supported_observations:
        return CandidateGrounding(
            synthesis=None,
            reason="no_supported_observations",
            answer=answer_grounding,
            observation_count=0,
            supported_observation_count=0,
        )

    candidate_text = "\n".join(statement.text for statement in supported_observations)
    if len(candidate_text) > MAX_REFLECTION_ANSWER_CHARS:
        return CandidateGrounding(
            synthesis=None,
            reason="candidate_observations_too_large",
            answer=answer_grounding,
            observation_count=len(synthesis.observations),
            supported_observation_count=len(supported_observations),
        )
    candidate_citations = tuple(
        dict.fromkeys(
            citation
            for statement in supported_observations
            for citation in statement.citations
        )
    )
    candidate = ReflectionSynthesis(
        observations=tuple(supported_observations),
        inferences=(),
        uncertainties=(),
        answer=candidate_text,
        citations=candidate_citations,
        followup_queries=(),
    )
    return CandidateGrounding(
        synthesis=candidate,
        reason="",
        answer=answer_grounding,
        observation_count=len(synthesis.observations),
        supported_observation_count=len(supported_observations),
    )


__all__ = [
    "CandidateGrounding",
    "TextGrounding",
    "grounded_candidate_synthesis",
    "text_grounding",
]
