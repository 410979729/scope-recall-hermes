"""Pure query-signal and recall-candidate admission contracts.

This module deliberately owns no provider, SQLite, vector-store, filesystem, or
network capability.  It turns already-collected candidate evidence into a
deterministic admission decision so ranking priors cannot manufacture relevance.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from ...gating import clean_text, is_trivial, semantic_query_tokens
from ...scoring import lexical_score

QuerySignalState = Literal[
    "positive",
    "weak",
    "none",
    "identifier_exact_only",
]

DEFAULT_VECTOR_ONLY_MIN_SCORE = 0.70
DEFAULT_VECTOR_ONLY_MIN_MARGIN = 0.035
VECTOR_BACKGROUND_RANK = 5

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_STRUCTURED_IDENTIFIER_RE = re.compile(
    r"^(?=.{4,128}$)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_.:-]+$"
)
_ASCII_ALNUM_RE = re.compile(r"^[A-Za-z0-9]+$")
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]"
)
_RARE_CJK_RE = re.compile(r"[\u3400-\u4dbf\U00020000-\U0002fa1f]")
@dataclass(frozen=True, slots=True)
class QuerySignalAssessment:
    """Aggregate evidence state for one query after candidate assessment."""

    state: QuerySignalState
    reason_codes: tuple[str, ...]
    semantic_tokens: tuple[str, ...]
    exact_lexical_match: bool
    lexical_match_count: int
    vector_only_admissible: bool


@dataclass(frozen=True, slots=True)
class CandidateAdmission:
    """Evidence authorities that may admit one ordinary-recall candidate."""

    admitted: bool
    reason_codes: tuple[str, ...]
    lexical_evidence: bool
    vector_evidence: bool
    curated_evidence: bool
    temporal_evidence: bool
    exact_identifier_evidence: bool


def _bounded_score(value: float, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    length = len(text)
    counts = {character: text.count(character) for character in set(text)}
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _looks_like_random_cjk(text: str) -> bool:
    """Identify only structurally unambiguous CJK extension-area noise.

    Common-Han text cannot be classified as nonsense safely without a language
    model or a complete linguistic resource.  Treating a finite vocabulary as
    proof would reject legitimate phrases from unseen domains, so common-Han
    queries remain eligible for calibrated vector evidence.  Rare extension
    runs provide a deterministic negative fixture without that false-positive
    boundary; exact lexical lookup remains available for them.
    """

    cjk_characters = _CJK_RE.findall(text)
    if len(cjk_characters) < 6:
        return False
    rare_count = len(_RARE_CJK_RE.findall(text))
    unique_ratio = len(set(cjk_characters)) / len(cjk_characters)
    return rare_count / len(cjk_characters) >= 0.60 and unique_ratio >= 0.80


def _looks_like_random_ascii(text: str, *, min_length: int = 10) -> bool:
    """Identify short consonant-heavy ASCII token salad deterministically."""

    if not min_length <= len(text) <= 32 or not text.isalpha() or not text.isascii():
        return False
    folded = text.casefold()
    unique_ratio = len(set(folded)) / len(folded)
    if unique_ratio < 0.58:
        return False
    vowels = frozenset("aeiou")
    vowel_ratio = sum(character in vowels for character in folded) / len(folded)
    if len(folded) < 10:
        # Six-to-nine-letter English words are too easy to misclassify from
        # vowel ratio alone.  The mixed-language guard uses this short range
        # only for a stricter zero-vowel machine-token signal; ``y`` is treated
        # as vowel-like here so ordinary words such as ``rhythms`` fail open.
        return not any(character in frozenset("aeiouy") for character in folded)
    longest_consonant_run = max(
        (len(run) for run in re.findall(r"[^aeiou]+", folded)),
        default=0,
    )
    return vowel_ratio <= 0.18 or longest_consonant_run >= 7


def is_opaque_query(query: str) -> bool:
    """Return whether vector similarity must not establish relevance by itself.

    Exact lexical matches remain admissible for every opaque form.  Chinese is
    intentionally not entropy-classified: an unsegmented natural CJK query must
    not be mistaken for a random ASCII identifier.
    """

    text = clean_text(query)
    if not text or is_trivial(text):
        return True
    contains_cjk = bool(_CJK_RE.search(text))
    if contains_cjk and _looks_like_random_cjk(text):
        return True
    if contains_cjk and any(
        _looks_like_random_ascii(segment, min_length=6)
        for segment in re.findall(r"[A-Za-z]{6,32}", text)
    ):
        return True
    if contains_cjk:
        return False
    compact = "".join(text.split())
    single_token = compact == text
    if _UUID_RE.fullmatch(compact) or _SHA_RE.fullmatch(compact):
        return True
    # A structured identifier is opaque only when it is the whole query.
    # Natural multi-word queries may legitimately contain one identifier
    # (for example ``Orion rollback checkpoint Delta-7``).
    if single_token and _STRUCTURED_IDENTIFIER_RE.fullmatch(compact):
        return True
    if single_token and _looks_like_random_ascii(compact):
        return True
    if (
        single_token
        and len(compact) >= 16
        and len(compact) % 4 == 0
        and _BASE64_RE.fullmatch(compact)
        and _shannon_entropy(compact.rstrip("=")) >= 3.25
    ):
        return True
    if (
        single_token
        and len(compact) >= 16
        and _ASCII_ALNUM_RE.fullmatch(compact)
        and _shannon_entropy(compact) >= 3.5
    ):
        return True
    return not semantic_query_tokens(text)


def _has_identifier_boundaries(haystack: str, *, start: int, end: int) -> bool:
    """Refuse an occurrence embedded in a larger identifier-like token."""

    if start > 0:
        left = haystack[start - 1]
        if left.isalnum() or left == "_":
            return False
        if left in ".:-" and start > 1 and haystack[start - 2].isalnum():
            return False
    if end < len(haystack):
        right = haystack[end]
        if right.isalnum() or right == "_":
            return False
        if right in ".:-" and end + 1 < len(haystack) and haystack[end + 1].isalnum():
            return False
    return True


def _contains_complete_identifier(haystack: str, needle: str) -> bool:
    start = 0
    while True:
        start = haystack.find(needle, start)
        if start < 0:
            return False
        end = start + len(needle)
        if _has_identifier_boundaries(haystack, start=start, end=end):
            return True
        start += 1


def _exact_opaque_match(
    query: str,
    *,
    candidate_id: str,
    documents: tuple[str, ...],
) -> bool:
    """Match a complete opaque query without accepting partial identifiers."""

    if not is_opaque_query(query):
        return False
    needle = clean_text(query).casefold()
    if not needle:
        return False
    compact = "".join(needle.split())
    identifier_shaped = bool(
        _UUID_RE.fullmatch(compact)
        or _SHA_RE.fullmatch(compact)
        or _STRUCTURED_IDENTIFIER_RE.fullmatch(compact)
        or _looks_like_random_ascii(compact)
        or _looks_like_random_cjk(compact)
        or (
            len(compact) >= 16
            and _BASE64_RE.fullmatch(compact)
            and _shannon_entropy(compact.rstrip("=")) >= 3.25
        )
    )
    if not identifier_shaped:
        # Stopwords, punctuation, emoji, and other zero-semantic noise are
        # opaque too, but an incidental exact word in a memory must never turn
        # those forms into identifier authority.
        return False
    normalized_candidate_id = str(candidate_id or "").strip().casefold()
    if needle == normalized_candidate_id:
        return True
    # Stored IDs may use a short, explicit namespace (``memory:<uuid>``).
    # Accept only a complete namespaced suffix here; prose occurrences still
    # use the stricter continuation-boundary scan below.
    if normalized_candidate_id.endswith(f":{needle}"):
        namespace = normalized_candidate_id[: -(len(needle) + 1)]
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", namespace):
            return True
    haystack = "\n".join(
        [str(candidate_id or ""), *(str(value or "") for value in documents)]
    ).casefold()
    if not haystack:
        return False
    return _contains_complete_identifier(haystack, needle)


def assess_candidate_admission(
    query: str,
    *,
    candidate_id: str,
    content: str,
    summary: str = "",
    source: str = "",
    target: str = "memory",
    vector_score: float = 0.0,
    vector_background_score: float | None = None,
    vector_only_min_score: float = DEFAULT_VECTOR_ONLY_MIN_SCORE,
    vector_only_min_margin: float = DEFAULT_VECTOR_ONLY_MIN_MARGIN,
    opaque_query_vector_only_enabled: bool = False,
    temporal_authoritative: bool = False,
    temporal_text: str = "",
) -> CandidateAdmission:
    """Return the deterministic admission authorities for one candidate.

    Vector evidence is intentionally *vector-only* evidence: it becomes an
    authority only when no lexical/curated/temporal authority exists and the
    query, absolute score, and separation margin all pass their gates.
    """

    normalized_query = clean_text(query)
    documents = (content, summary, temporal_text)
    opaque = is_opaque_query(normalized_query)
    exact_identifier_evidence = _exact_opaque_match(
        normalized_query,
        candidate_id=candidate_id,
        documents=documents,
    )

    recomputed_lexical_score = lexical_score(
        query=normalized_query,
        content=str(content or ""),
        summary=str(summary or ""),
        source=str(source or ""),
        target=str(target or "memory"),
    )
    # The legacy lexical scorer intentionally accepts exact phrase substrings.
    # For opaque identifiers that is too broad (``deadbeef`` must not match
    # inside ``00deadbeefff``), so their only lexical authority is the stricter
    # boundary-aware exact match above.
    lexical_evidence = (
        exact_identifier_evidence if opaque else recomputed_lexical_score > 0.0
    )
    curated_candidate = str(source or "").strip().casefold() == "builtin-curated" or str(
        candidate_id or ""
    ).startswith("curated:")
    curated_evidence = curated_candidate and lexical_evidence

    temporal_evidence = False
    if temporal_authoritative:
        temporal_score = lexical_score(
            query=normalized_query,
            content="\n".join(
                value for value in (str(content or ""), str(temporal_text or "")) if value
            ),
            summary=str(summary or ""),
            source=str(source or ""),
            target=str(target or "memory"),
        )
        temporal_evidence = temporal_score > 0.0

    reason_codes: list[str] = []
    if exact_identifier_evidence:
        reason_codes.append("exact_identifier_match")
    if lexical_evidence:
        reason_codes.append("lexical_semantic_match")
    if curated_evidence:
        reason_codes.append("curated_lexical_match")
    if temporal_evidence:
        reason_codes.append("temporal_query_match")

    non_vector_authority = bool(
        exact_identifier_evidence
        or lexical_evidence
        or curated_evidence
        or temporal_evidence
    )
    vector_evidence = False
    if not non_vector_authority:
        score = _bounded_score(vector_score)
        background_available = vector_background_score is not None
        background = _bounded_score(vector_background_score or 0.0)
        absolute_floor = _bounded_score(vector_only_min_score, default=0.70)
        margin_floor = _bounded_score(vector_only_min_margin, default=0.035)
        margin = score - background
        semantic_tokens = semantic_query_tokens(normalized_query)
        if score <= 0.0:
            reason_codes.append("vector_score_not_positive")
        elif opaque and not opaque_query_vector_only_enabled:
            reason_codes.append("opaque_query_vector_only_denied")
        elif not semantic_tokens:
            reason_codes.append("query_has_no_semantic_tokens")
        elif score < absolute_floor:
            reason_codes.append("vector_only_below_min_score")
        elif not background_available:
            # A nearest-neighbour search always has a top result.  Without a
            # real background neighbour there is no separation evidence, so
            # treating the missing value as zero would recreate the very
            # "least bad result wins" failure this gate exists to prevent.
            reason_codes.append("vector_background_unavailable")
        elif margin < margin_floor:
            reason_codes.append("vector_only_below_min_margin")
        else:
            vector_evidence = True
            reason_codes.append("vector_only_admitted")

    admitted = non_vector_authority or vector_evidence
    if not admitted:
        reason_codes.append("no_admissible_evidence")
    return CandidateAdmission(
        admitted=admitted,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        lexical_evidence=lexical_evidence,
        vector_evidence=vector_evidence,
        curated_evidence=curated_evidence,
        temporal_evidence=temporal_evidence,
        exact_identifier_evidence=exact_identifier_evidence,
    )


def assess_query_signal(
    query: str,
    admissions: Iterable[CandidateAdmission] = (),
) -> QuerySignalAssessment:
    """Aggregate candidate decisions into the frozen four-state query contract."""

    candidates = tuple(admissions)
    admitted = tuple(candidate for candidate in candidates if candidate.admitted)
    opaque = is_opaque_query(query)
    exact_match = any(candidate.exact_identifier_evidence for candidate in candidates)
    lexical_match_count = sum(
        1 for candidate in candidates if candidate.lexical_evidence
    )
    vector_only_admissible = any(
        candidate.admitted and candidate.vector_evidence for candidate in candidates
    )

    if admitted:
        if opaque and all(candidate.exact_identifier_evidence for candidate in admitted):
            state: QuerySignalState = "identifier_exact_only"
        else:
            state = "positive"
    elif candidates and not opaque:
        state = "weak"
    else:
        state = "none"

    reason_codes: list[str] = []
    if opaque:
        reason_codes.append("opaque_query")
    for candidate in candidates:
        reason_codes.extend(candidate.reason_codes)
    state_reason = {
        "positive": "admissible_evidence_present",
        "weak": "weak_evidence_only",
        "none": "no_admissible_evidence",
        "identifier_exact_only": "identifier_exact_lexical_only",
    }[state]
    reason_codes.append(state_reason)
    return QuerySignalAssessment(
        state=state,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        semantic_tokens=tuple(semantic_query_tokens(query)),
        exact_lexical_match=exact_match,
        lexical_match_count=lexical_match_count,
        vector_only_admissible=vector_only_admissible,
    )


__all__ = [
    "CandidateAdmission",
    "DEFAULT_VECTOR_ONLY_MIN_MARGIN",
    "DEFAULT_VECTOR_ONLY_MIN_SCORE",
    "VECTOR_BACKGROUND_RANK",
    "QuerySignalAssessment",
    "QuerySignalState",
    "assess_candidate_admission",
    "assess_query_signal",
    "is_opaque_query",
]
