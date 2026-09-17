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
from .embedding_budget import bounded_embedding_text
from .events import lexical_terms, query_terms, version_suffixes
from .retrieval import CandidateRef, SearchContext


EMBEDDING_SPACE = {
    "model": "gemini-embedding-2",
    "dimensions": 3072,
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
    "task_type": None,
    "prompt_encoding": {
        "document": "title: none | text: {NFKC source}",
        "query": "task: question answering | query: {NFKC query}",
    },
    "input_preprocessing": {"id": "nfkc-v1", "unicode_version": "15.0.0"},
    "metric": "cosine",
    "vector_normalization": "l2_at_scoring",
    "request_encoding": {
        "id": "gemini002-native-batch-v1",
        "task_type_field": None,
        "task_type_location": None,
        "prompt_encoding": "official-question-answering-v1",
    },
}
_SPACE_BYTES = json.dumps(
    EMBEDDING_SPACE, ensure_ascii=False, separators=(",", ":")
).encode("utf-8")
SPACE_ID = hashlib.sha256(_SPACE_BYTES).hexdigest()
VECTOR_SCORE_TOLERANCE = 1e-6

_WEAK_QUERY = frozenset({"那次", "那件", "那个", "这个", "怎样", "如何", "what", "that", "it"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,239}")
_HARD_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]{1,12}\d[A-Za-z0-9._/-]*|\d+[A-Za-z][A-Za-z0-9._/-]*)(?![A-Za-z0-9_])")
_CJK_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CLAUSE_BOUNDARY = re.compile(r"[，,；;。！？!?\n]+")


def canonical_embedding_space(value: dict) -> dict:
    """Return the frozen descriptor with a stable key order and strict fields."""

    if type(value) is not dict or set(value) != set(EMBEDDING_SPACE):
        raise ContractError("INPUT_INVALID", "embedding_space")
    # Shape and bounds, not identity. This used to require every field to equal
    # the module default, which made the embedding model unconfigurable: a
    # deployment could not choose its own provider even though the vector store
    # keys everything on the space digest and would have rebuilt cleanly.
    # Identity is still enforced, one layer out and where it belongs — a vector
    # whose space digest differs from the one its `RecallPolicy` is bound to
    # (`embedding_space_id`, the configured route's space in a runtime
    # instance) is refused admission in `RecallPolicy.vector_admission`.
    if type(value.get("model")) is not str or not 1 <= len(value["model"]) <= 200:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("dimensions")) is not int or not 8 <= value["dimensions"] <= 16384:
        raise ContractError("INPUT_INVALID", "embedding_space")
    endpoint = value.get("endpoint")
    if type(endpoint) is not str or not endpoint.startswith("https://") or len(endpoint) > 2048:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if value.get("task_type") is not None:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("prompt_encoding")) is not dict or set(value["prompt_encoding"]) != {"document", "query"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if value["prompt_encoding"] != EMBEDDING_SPACE["prompt_encoding"]:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("input_preprocessing")) is not dict or set(value["input_preprocessing"]) != {"id", "unicode_version"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if value["input_preprocessing"] != EMBEDDING_SPACE["input_preprocessing"]:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if value.get("metric") != "cosine" or value.get("vector_normalization") != "l2_at_scoring":
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value.get("request_encoding")) is not dict or set(value["request_encoding"]) != {"id", "task_type_field", "task_type_location", "prompt_encoding"}:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value["request_encoding"].get("id")) is not str or not value["request_encoding"]["id"]:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if value["request_encoding"]["task_type_field"] is not None or value["request_encoding"]["task_type_location"] is not None:
        raise ContractError("INPUT_INVALID", "embedding_space")
    if type(value["request_encoding"].get("prompt_encoding")) is not str or not value["request_encoding"]["prompt_encoding"]:
        raise ContractError("INPUT_INVALID", "embedding_space")
    result = {
        "model": value["model"],
        "dimensions": value["dimensions"],
        "endpoint": value["endpoint"],
        "task_type": None,
        "prompt_encoding": {
            "document": value["prompt_encoding"]["document"],
            "query": value["prompt_encoding"]["query"],
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
            "prompt_encoding": value["request_encoding"]["prompt_encoding"],
        },
    }
    return result


def embedding_space_id(value: dict) -> str:
    canonical = canonical_embedding_space(value)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


#: Wire dialects the embedding adapter can speak. "gemini" is Google's native
#: batchEmbedContents shape; "openai" is the /v1/embeddings shape that MiniMax,
#: Qwen, OpenAI and most other providers accept.
EMBEDDING_DIALECTS = frozenset({"gemini", "openai"})


def build_embedding_space(*, model: str, dimensions: int, endpoint: str, dialect: str = "gemini") -> dict:
    """Describe one embedding space so its digest can key the vector store.

    Everything about how text reaches a model belongs in this descriptor,
    because the digest of it is the vector directory name. Change the model, the
    dimensionality or the request shape and the digest changes, the store moves
    to a new directory, and vectors from the old space are refused admission
    instead of being silently compared across incompatible geometries. That
    property is what makes swapping embedding models safe; it was already here,
    and only the hardcoded descriptor stopped anyone from using it.
    """
    if dialect not in EMBEDDING_DIALECTS:
        raise ContractError("INPUT_INVALID", "embedding_dialect")
    return canonical_embedding_space({
        "model": model,
        "dimensions": dimensions,
        "endpoint": endpoint,
        "task_type": None,
        "prompt_encoding": dict(EMBEDDING_SPACE["prompt_encoding"]),
        "input_preprocessing": dict(EMBEDDING_SPACE["input_preprocessing"]),
        "metric": "cosine",
        "vector_normalization": "l2_at_scoring",
        "request_encoding": {
            "id": f"{dialect}-embed-v1",
            "task_type_field": None,
            "task_type_location": None,
            "prompt_encoding": EMBEDDING_SPACE["request_encoding"]["prompt_encoding"],
        },
    })


def encode_embedding_text(raw_text: str, *, kind: str) -> str:
    """Encode raw source/query text for Gem2 without changing raw identity.

    The one choke point every embedded body passes through -- source, claim and
    query alike -- which is why the input bound lives here rather than in each
    caller.  Six sources on alpha were permanently unembeddable because there
    was no bound at all; see ``core/embedding_budget.py``.
    """

    if type(raw_text) is not str or kind not in {"document", "query"}:
        raise ContractError("INPUT_INVALID", "embedding_text")
    normalized = unicodedata.normalize("NFKC", raw_text)
    bounded, _truncated = bounded_embedding_text(normalized)
    if kind == "query":
        return f"task: question answering | query: {bounded}"
    return f"title: none | text: {bounded}"


def claim_embedding_text(payload: dict) -> str:
    """Render one claim as the assertion it makes, for embedding.

    The stored payload is JSON; embedding it verbatim would index braces and
    field names rather than meaning. Subject, predicate and value are the
    assertion, so they lead; conditions follow because they qualify it rather
    than identify it.

    Derived objects are unreachable from automatic recall until they are in the
    vector index: the lexical, vector and recent channels all yield events, so a
    claim's only other route is relation expansion out of an event that happened
    to be retrieved first.
    """
    if not isinstance(payload, dict):
        raise ContractError("INPUT_INVALID", "claim_embedding_text")
    statement = " ".join(
        part for field in ("subject", "predicate", "value_text")
        if (part := str(payload.get(field) or "").strip())
    )
    conditions = payload.get("conditions")
    if isinstance(conditions, (list, tuple)):
        qualifiers = " ".join(
            text for item in conditions if (text := str(item).strip())
        )
        if qualifiers:
            statement = f"{statement}（{qualifiers}）" if statement else qualifiers
    if not statement:
        raise ContractError("DERIVATION_INVALID", "claim_embedding_text")
    return statement


def _finite_score(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value)


@dataclass(frozen=True)
class RecallPolicy:
    """Admission thresholds are injected by the frozen space configuration."""

    vector_threshold: float | None
    lexical_min_terms: int = 1
    rrf_k: int = 60
    #: Digest of the space the query is embedded in; only vectors from that
    #: space are comparable to it.  The shipped space is the default, so a Core
    #: or an installation that names no embedding route keeps its behavior.
    embedding_space_id: str = SPACE_ID

    def __post_init__(self) -> None:
        threshold = _finite_score(self.vector_threshold)
        if self.vector_threshold is not None and (threshold is None or not -1.0 <= threshold <= 1.0):
            raise ContractError("INPUT_INVALID", "vector_threshold")
        if type(self.lexical_min_terms) is not int or self.lexical_min_terms < 1:
            raise ContractError("INPUT_INVALID", "lexical_min_terms")
        if type(self.rrf_k) is not int or self.rrf_k < 1:
            raise ContractError("INPUT_INVALID", "rrf_k")
        if type(self.embedding_space_id) is not str or not self.embedding_space_id.strip():
            raise ContractError("INPUT_INVALID", "embedding_space_id")

    def vector_admission(self, candidate: CandidateRef) -> tuple[bool, str | None]:
        if candidate.source != "vector":
            return False, "not_vector"
        if type(candidate.vector_id) is not str or not candidate.vector_id:
            return False, "vector_id_missing"
        if candidate.embedding_space != self.embedding_space_id:
            return False, "embedding_space_mismatch"
        score = _finite_score(candidate.vector_score)
        if score is None:
            return False, "vector_score_invalid"
        # Cosine similarity is mathematically bounded.  Allow only the same
        # fixed float32/calibration tolerance used at the threshold boundary;
        # an arbitrary out-of-range score must never become high-confidence.
        if score > 1.0 + VECTOR_SCORE_TOLERANCE or score < -1.0 - VECTOR_SCORE_TOLERANCE:
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
        if not query_is_specific(
            query,
            matched_terms=score,
            matched_query_terms=candidate.matched_query_terms,
        ):
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


def _specificity_required(term_count: int) -> int:
    required = max(2, math.ceil(term_count * 0.30))
    return max(required, 3) if term_count >= 3 else required


def _substantive_chinese_clauses(query: str) -> tuple[tuple[str, ...], ...]:
    if _CJK_TEXT.search(query) is None:
        return ()
    clauses = tuple(
        meaningful_query_terms(clause)
        for clause in _CLAUSE_BOUNDARY.split(query)
        if clause.strip() and _CJK_TEXT.search(clause) is not None
    )
    substantive = tuple(terms for terms in clauses if len(terms) >= 5)
    return substantive if len(substantive) >= 2 else ()


def query_is_specific(
    query: str,
    *,
    matched_terms: float,
    matched_query_terms: Iterable[str] | None = None,
) -> bool:
    """Require global, or conservative Chinese clause-level, lexical coverage."""

    terms = meaningful_query_terms(query)
    if not terms:
        return False
    if hard_identifiers(query):
        return True
    if len(terms) == 1:
        return matched_terms >= 1.0
    required = _specificity_required(len(terms))
    if matched_terms >= required and matched_terms / len(terms) >= 0.30:
        return True
    matches = frozenset(matched_query_terms or ())
    for clause_terms in _substantive_chinese_clauses(query):
        clause_hits = len(matches.intersection(clause_terms))
        if (
            clause_hits >= max(5, _specificity_required(len(clause_terms)))
            and clause_hits / len(clause_terms) >= 0.30
        ):
            return True
    return False


def meaningful_query_terms(query: str) -> tuple[str, ...]:
    """Terms that can establish lexical relevance for one candidate."""

    return tuple(term for term in query_terms(query) if term not in _WEAK_QUERY and len(term) > 1)


def hard_identifiers(text: str) -> frozenset[str]:
    # A release is also identified by its version suffix: "rc28" for 3.1.0rc28.
    matches = frozenset(match.group(0).casefold() for match in _HARD_IDENTIFIER.finditer(text))
    return matches | version_suffixes(text)


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
