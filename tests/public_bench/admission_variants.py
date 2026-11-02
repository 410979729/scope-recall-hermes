"""Lexical admission alternatives over an immutable retrieval fixture."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from statistics import median

from scope_recall.core import lexical_index
from scope_recall.core.recall_policy import (
    RecallPolicy,
    _specificity_required,
    _substantive_chinese_clauses,
    meaningful_query_terms,
)

SCHEMES = ("A", "B", "C", "D")


class SnapshotWeights:
    """Read corpus statistics once per query from a closed, copied fixture."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.documents = int(self.connection.execute("SELECT COUNT(*) FROM source_events").fetchone()[0])
        self.frequencies: dict[str, dict[str, int]] = {}
        self.cached: dict[tuple[str, str], dict[str, float]] = {}

    def weights(self, scheme: str, query: str) -> dict[str, float]:
        """Return IDF with maximum, zero, or observed-median weights for unseen terms."""
        key = (scheme, query)
        if key in self.cached:
            return self.cached[key]
        terms = meaningful_query_terms(query)
        if query not in self.frequencies:
            self.frequencies[query] = lexical_index.document_frequency(self.connection, terms)
        frequencies = self.frequencies[query]
        values = {term: 1.0 + math.log((self.documents + 1) / (frequencies.get(term, 0) + 1)) for term in terms}
        if scheme in {"C", "D"}:
            observed = [value for term, value in values.items() if frequencies.get(term, 0)]
            unseen = median(observed) if scheme == "D" and observed else 0.0
            values = {term: value if frequencies.get(term, 0) else unseen for term, value in values.items()}
        self.cached[key] = values
        return values

    def close(self) -> None:
        """Release the read-only snapshot connection."""
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def coverage(terms: Iterable[str], matched: set[str], weights: dict[str, float]) -> float:
    """Normalize matched query weight without consuming a one-pass iterable twice."""
    terms = tuple(terms)
    total = sum(weights.get(term, 1.0) for term in terms)
    return sum(weights.get(term, 1.0) for term in terms if term in matched) / total if total else 0.0


@dataclass(frozen=True)
class ReplayPolicy(RecallPolicy):
    """Compare lexical floors while retaining the product's nonlexical admission rules."""

    scheme: str = "A"
    statistics: SnapshotWeights | None = None

    @classmethod
    def from_policy(cls, policy: RecallPolicy, scheme: str, statistics: SnapshotWeights, threshold: float):
        """Retain the store's geometry and policy settings while changing only the measured floor."""
        if scheme not in SCHEMES:
            raise ValueError("unknown_lexical_scheme")
        settings = {field.name: getattr(policy, field.name) for field in fields(RecallPolicy)}
        settings["vector_threshold"] = threshold
        return cls(**settings, scheme=scheme, statistics=statistics)

    def automatic_admission(self, candidate, query: str, *, term_weights=None) -> bool:
        """Apply a selected coverage normalization after the unchanged matched-term gate."""
        if self.scheme == "A" or candidate.source in {"vector", "exact_ref", "scoped", "relation"}:
            return RecallPolicy.automatic_admission(self, candidate, query)
        minimum_only = replace(self, auto_lexical_min_coverage=0.0)
        if not RecallPolicy.automatic_admission(minimum_only, candidate, query):
            return False
        if self.statistics is None:
            raise ValueError("variant_statistics_required")
        terms = set(meaningful_query_terms(query))
        matched = terms.intersection(candidate.matched_query_terms or ())
        weights = self.statistics.weights(self.scheme, query)
        if coverage(terms, matched, weights) >= self.auto_lexical_min_coverage:
            return True
        return any(
            len(hits) >= max(5, _specificity_required(len(clause)), min(len(clause), self.auto_lexical_min_terms))
            and coverage(clause, hits, weights) >= self.auto_lexical_min_coverage
            for clause in _substantive_chinese_clauses(query)
            if (hits := matched.intersection(clause))
        )
