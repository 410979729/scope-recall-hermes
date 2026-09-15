"""Pure admission and ranking policy for the P08 retrieval pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Iterable
import unicodedata

from ..contracts import ContractError
from .events import lexical_terms, query_terms
from .retrieval import CandidateRef, SearchContext


EMBEDDING_SPACE = {
    "model": "gemini-embedding-001",
    "dimensions": 3072,
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents",
    "task_type": {
        "document": "RETRIEVAL_DOCUMENT",
        "query": "RETRIEVAL_QUERY",
    },
    "input_preprocessing": {"id": "nfkc-v1", "unicode_version": "15.0.0"},
    "metric": "cosine",
    "vector_normalization": "l2_at_scoring",
    "request_encoding": {
        "id": "gemini001-native-top-level-v1",
        "task_type_field": "taskType",
        "task_type_location": "request",
    },
}
_SPACE_BYTES = json.dumps(
    EMBEDDING_SPACE, ensure_ascii=False, separators=(",", ":")
).encode("utf-8")
SPACE_ID = hashlib.sha256(_SPACE_BYTES).hexdigest()
COSINE_DEFINITION = "cosine similarity = dot(a,b) / (sqrt(dot(a,a)) * sqrt(dot(b,b))); native distance converts as 1 - cosine"
VECTOR_SCORE_TOLERANCE = 1e-6

_WEAK_QUERY = frozenset({"那次", "那件", "那个", "这个", "怎样", "如何", "what", "that", "it"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,239}")
_HARD_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]{1,12}\d[A-Za-z0-9._/-]*|\d+[A-Za-z][A-Za-z0-9._/-]*)(?![A-Za-z0-9_])")


def canonical_embedding_space(value: dict) -> dict:
    """Return the frozen descriptor with a stable key order and strict fields."""

    if type(value) is not dict or set(value) != set(EMBEDDING_SPACE):
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("task_type")) is not dict or set(value["task_type"]) != {"document", "query"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("input_preprocessing")) is not dict or set(value["input_preprocessing"]) != {"id", "unicode_version"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("request_encoding")) is not dict or set(value["request_encoding"]) != {"id", "task_type_field", "task_type_location"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    result = {
        "model": value["model"],
        "dimensions": value["dimensions"],
        "endpoint": value["endpoint"],
        "task_type": {
            "document": value["task_type"]["document"],
            "query": value["task_type"]["query"],
        },
        "input_preprocessing": {
            "id": value["input_preprocessing"]["id"],
            "unicode_version": value["input_preprocessing"]["unicode_version"],
        },
        "metric": value["metric"],
        "vector_normalization": value["vector_normalization"],
        "request_encoding": {
            "id": value["request_encoding"]["id"],
            "task_type_field": value["request_encoding"]["task_type_field"],
            "task_type_location": value["request_encoding"]["task_type_location"],
        },
    }
    if result != EMBEDDING_SPACE:
        raise ContractError("INPUT_INVALID", "embedding_space")
    return result


def embedding_space_id(value: dict) -> str:
    canonical = canonical_embedding_space(value)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _finite_score(value: object) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return None
    return float(value)


@dataclass(frozen=True)
class RecallPolicy:
    """Admission thresholds are injected by the frozen space configuration."""

    vector_threshold: float | None
    lexical_min_terms: int = 1
    rrf_k: int = 60

    def __post_init__(self) -> None:
        if self.vector_threshold is not None and (
            type(self.vector_threshold) not in (int, float)
            or not math.isfinite(float(self.vector_threshold))
            or not -1.0 <= float(self.vector_threshold) <= 1.0
        ):
            raise ContractError("INPUT_INVALID", "vector_threshold")
        if type(self.lexical_min_terms) is not int or self.lexical_min_terms < 1:
            raise ContractError("INPUT_INVALID", "lexical_min_terms")
        if type(self.rrf_k) is not int or self.rrf_k < 1:
            raise ContractError("INPUT_INVALID", "rrf_k")

    def vector_admission(self, candidate: CandidateRef) -> tuple[bool, str | None]:
        if candidate.source != "vector":
            return False, "not_vector"
        if type(candidate.vector_id) is not str or not candidate.vector_id:
            return False, "vector_id_missing"
        if candidate.embedding_space != SPACE_ID:
            return False, "embedding_space_mismatch"
        score = _finite_score(candidate.vector_score)
        if score is None:
            return False, "vector_score_invalid"
        if self.vector_threshold is None:
            return False, "vector_threshold_unconfigured"
        if score + VECTOR_SCORE_TOLERANCE < self.vector_threshold:
            return False, "vector_below_threshold"
        return True, None

    def lexical_admission(self, candidate: CandidateRef, query: str, *, exact: bool = False) -> tuple[bool, str | None]:
        if candidate.source == "exact_ref" or exact:
            return True, None
        if candidate.source not in {"lexical", "recent_raw", "relation"}:
            return False, "not_lexical"
        score = _finite_score(candidate.lexical_score)
        if score is None:
            return False, "lexical_score_invalid"
        terms = [term for term in query_terms(query) if term not in _WEAK_QUERY]
        if not terms and candidate.source == "recent_raw":
            return False, "weak_query_only"
        if score < self.lexical_min_terms:
            return False, "lexical_below_minimum"
        if not query_is_specific(query, matched_terms=score):
            return False, "lexical_insufficient_specificity"
        return True, None


def parse_time(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContractError("INPUT_INVALID", "timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ContractError("INPUT_INVALID", "timestamp")
    return stamp.astimezone(timezone.utc)


def in_time_window(value: str | None, context: SearchContext) -> bool:
    """Check source/object time against one fixed query clock."""

    if value is None:
        return context.mode not in {"current", "as_of"}
    stamp = parse_time(value)
    now = parse_time(context.now)
    if stamp > now:
        return False
    if context.as_of is not None and stamp > parse_time(context.as_of):
        return False
    return True


def query_is_relevant(query: str, content: str) -> bool:
    """Use meaningful lexical anchors for recent raw admission.

    Weak conversational words alone do not force an unrelated same-project
    source into the result; vector candidates remain independent of this gate.
    """

    q = set(meaningful_query_terms(query))
    if not q:
        return False
    overlap = len(q.intersection(lexical_terms(content)))
    if hard_identifiers(query).intersection(hard_identifiers(content)):
        return True
    if len(q) == 1:
        return overlap == 1
    return overlap >= max(2, math.ceil(len(q) * 0.30))


def query_is_specific(query: str, *, matched_terms: float) -> bool:
    """Require enough distinct lexical coverage for generic candidates."""

    terms = meaningful_query_terms(query)
    if not terms:
        return False
    if hard_identifiers(query):
        return True
    if len(terms) == 1:
        return matched_terms >= 1.0
    required = max(2, math.ceil(len(terms) * 0.30))
    if len(terms) >= 3:
        required = max(required, 3)
    return matched_terms >= required and matched_terms / len(terms) >= 0.30


def meaningful_query_terms(query: str) -> tuple[str, ...]:
    """Terms that can establish lexical relevance for one candidate."""

    return tuple(term for term in query_terms(query) if term not in _WEAK_QUERY and len(term) > 1)


def hard_identifiers(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _HARD_IDENTIFIER.finditer(text))


def identifiers_compatible(query: str, content: str) -> bool:
    """Keep distinct hard identifiers separate while supporting comparisons.

    A query naming H100 may only admit content that carries H100.  A query
    naming both H100 and H200 can therefore admit one side of the comparison
    at a time, with the evidence identity preserved for each side.
    """

    requested = hard_identifiers(query)
    if not requested:
        return True
    present = hard_identifiers(content)
    return bool(requested.intersection(present))


def applicability(context: SearchContext, project_id: str | None, branch_id: str | None) -> str:
    parts = []
    if project_id is not None:
        parts.append(f"project:{project_id}")
    if branch_id is not None:
        parts.append(f"branch:{branch_id}")
    return ",".join(parts) if parts else "trusted scope"


def rrf_score(ranks: Iterable[int], *, k: int = 60) -> float:
    return sum(1.0 / (k + rank) for rank in ranks)
