"""Prefer attributable first-hand evidence within the admitted candidate pool."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from ..contracts import ContractError, verified_human_principal_ref
from . import lineage
from .claim_storage import parse_source_ref
from .claims import canonical_time
from .deadline import RequestDeadline
from .events import lexical_sequence, lexical_terms
from .recall_needs import statement_text
from .retrieval import CandidateRef, RetrievedObject, optional_json
from .retrieval_hydration import RetrievalHydration, replacements
from .source_records import StoredSource
from .visibility import allowed

Pair = tuple[CandidateRef, RetrievedObject]
LINEAGE_LIMIT = 64
#: Optional pairing cannot spend the rest of a recall's release allowance.
PAIRING_SECONDS = 0.02
#: Bounded text comparisons retain their incomplete flag and can never remove a body.
COMPARISON_CHARACTERS = 32768
COMPARISON_TOTAL_CHARACTERS = 262144


class _Expired(Exception):
    """The optional pairing pass must return the untouched ranking."""


@dataclass(frozen=True)
class _Text:
    """Ordered complete-content tokens and bounded terms for candidate preselection."""

    words: tuple[str, ...]
    terms: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class _Provenance:
    """Audience and person proofs read through the scoped source/version interfaces."""

    audience: tuple[str, str | None, str | None]
    people: frozenset[str]
    bound: bool
    entries: frozenset[str]


@dataclass(frozen=True)
class _Original:
    """One first-hand candidate and the already admitted objects that supersede it."""

    pair: Pair
    proof: _Provenance
    text: _Text
    targets: tuple[Pair, ...]


def _entries(obj: RetrievedObject) -> frozenset[str]:
    labels = optional_json(dict(obj.metadata).get("entries"))
    return (
        frozenset(row["id"] for row in labels if isinstance(row, dict) and isinstance(row.get("id"), str))
        if isinstance(labels, list)
        else frozenset()
    )


def _visible_node(tx, ref: str, *, automatic=False):
    """Resolve a node's audience before its identity or evidence can influence pairing."""
    try:
        key, revision = parse_source_ref(ref)
    except ContractError:
        return None
    if len(key) > 240 or revision > 2**63 - 1:
        return None
    source = tx.source(key, revision)
    if source is not None:
        return None if automatic and (source.suppressed or not allowed(tx, "event", key, automatic=True)) else source
    version = tx.claims.version(key, revision)
    return (
        None
        if version is None or (automatic and (version.suppressed or not allowed(tx, "claim", key, automatic=True)))
        else version
    )


def _roots(tx, obj: RetrievedObject, check=None, *, automatic=False) -> set[str]:
    """Follow only readable nodes; unknown and invisible references are never common roots."""
    pending, seen, found = list(obj.evidence_refs), set(), set()
    while pending and len(seen) < LINEAGE_LIMIT:
        if check is not None:
            check()
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        node = _visible_node(tx, ref, automatic=automatic)
        if node is None:
            continue
        found.add(ref)
        if isinstance(node, StoredSource):
            pending.extend(node.event.get("evidence_refs", ()))
        else:
            evidence = tx.remembered(
                ("restatement_evidence", node.ref, node.revision),
                lambda node=node: lineage.evidence(tx._check(), "claim", node.ref, node.revision),
            )
            pending.extend(f"{key}@{revision}" for key, revision in evidence)
    return found


def _provenance(tx, pair: Pair, check) -> _Provenance | None:
    candidate, obj = pair
    node = _visible_node(tx, f"{candidate.ref}@{candidate.revision}")
    if node is None:
        return None
    sources = [node] if isinstance(node, StoredSource) else []
    if not isinstance(node, StoredSource):
        for ref in obj.evidence_refs:
            check()
            if (source := tx.source(*parse_source_ref(ref))) is not None:
                sources.append(source)
    if not sources or (not isinstance(node, StoredSource) and len(sources) != len(obj.evidence_refs)):
        return None
    people = [verified_human_principal_ref(source.event.get("source_principal")) for source in sources]
    return _Provenance(
        (node.scope_id, node.project_id, node.branch_id),
        frozenset(person for person in people if person is not None),
        all(person is not None for person in people),
        frozenset(source.entry_id for source in sources),
    )


def _later(first: RetrievedObject, echo: RetrievedObject) -> bool:
    try:
        a = canonical_time(dict(first.metadata).get("occurred_at"))
        b = canonical_time(dict(echo.metadata).get("occurred_at"))
    except ContractError:
        return False
    return bool(a and b and a > b)


def _contains(words: tuple[str, ...], run: tuple[str, ...], check) -> bool:
    """Match a whole consecutive token sequence in linear time, preserving repetition."""
    if not run or len(run) > len(words):
        return False
    if words == run:
        return True
    prefix = [0] * len(run)
    matched = 0
    for index in range(1, len(run)):
        if index % 256 == 0:
            check()
        while matched and run[index] != run[matched]:
            matched = prefix[matched - 1]
        matched += run[index] == run[matched]
        prefix[index] = matched
    matched = 0
    for index, word in enumerate(words):
        if index % 256 == 0:
            check()
        while matched and word != run[matched]:
            matched = prefix[matched - 1]
        matched += word == run[matched]
        if matched == len(run):
            return True
    return False


def mark_restatement(obj: RetrievedObject, refs: tuple[str, ...], *, incomplete=False) -> RetrievedObject:
    """Keep attribution in existing public provenance fields and internal release proofs."""
    metadata = tuple(
        (key, value) for key, value in obj.metadata if key not in {"restatement_of", "restatement_comparison"}
    )
    return replace(
        obj,
        basis="derived_summary",
        metadata=metadata
        + (("restatement_of", json.dumps(refs)),)
        + ((("restatement_comparison", "incomplete"),) if incomplete else ()),
    )


def recheck_restatement(tx, context, old: RetrievedObject, fresh: RetrievedObject) -> RetrievedObject:
    """Carry attribution across the release fence only while its cited nodes remain readable."""
    refs = optional_json(dict(old.metadata).get("restatement_of"))
    if not isinstance(refs, list) or old.content != fresh.content:
        return fresh
    visible = tuple(
        ref
        for ref in refs
        if isinstance(ref, str) and _visible_node(tx, ref, automatic=context.mode == "auto") is not None
    )
    return (
        mark_restatement(fresh, visible, incomplete=dict(old.metadata).get("restatement_comparison") == "incomplete")
        if visible
        else fresh
    )


class _Pool:
    """Read-only, deadline-bounded pairing state shared across one candidate pool."""

    def __init__(self, tx, ranked: list[Pair], context, clock=None):
        self.tx, self.ranked, self.context = tx, ranked, context
        self.deadline = RequestDeadline.within(context.deadline, budget_seconds=PAIRING_SECONDS, clock=clock)
        self.by_key = {pair[0].key: pair for pair in ranked}
        self.texts: dict[str, _Text] = {}
        self.roots: dict[tuple[str, str, int], set[str]] = {}
        self.characters = COMPARISON_TOTAL_CHARACTERS
        self.incomplete = False

    def check(self) -> None:
        if self.deadline.exhausted():
            raise _Expired

    def text(self, obj: RetrievedObject) -> _Text:
        self.check()
        if obj.content not in self.texts:
            limit = min(COMPARISON_CHARACTERS, self.characters)
            body, statement = obj.content[:limit], statement_text(obj)
            complete = len(obj.content) <= limit and len(statement) <= limit
            self.characters -= min(limit, len(obj.content))
            self.texts[obj.content] = _Text(
                lexical_sequence(body), frozenset(lexical_terms(statement[:limit])), complete
            )
            self.incomplete |= not complete
        self.check()
        return self.texts[obj.content]

    def lineage(self, pair: Pair) -> set[str]:
        key = pair[0].key
        if key not in self.roots:
            self.roots[key] = _roots(self.tx, pair[1], self.check, automatic=self.context.mode == "auto")
        return self.roots[key]

    def corrected(self, original: Pair, echo: Pair) -> bool:
        """Reuse hydration's established replacement chain, including sources outside this pool."""
        for ref in sorted(self.lineage(echo)):
            self.check()
            key, revision = parse_source_ref(ref)
            source = self.tx.source(key, revision)
            if source is None or source.event.get("origin") != "human_direct":
                continue

            def load(key=key, revision=revision):
                obj = RetrievalHydration().hydrate(
                    self.tx, CandidateRef("event", key, revision, "relation"), self.context
                )
                return replacements(obj) if obj is not None else ()

            targets = self.tx.remembered(
                ("restatement_replacements", key, revision, self.context.mode, self.context.now), load
            )
            if original[0].key in targets:
                return True
        return False

    def compatible(
        self, original: _Original, echo: Pair, proof: _Provenance, shared: int, terms: frozenset[str]
    ) -> bool:
        self.check()
        if original.proof.entries & proof.entries:
            return False
        linked = False
        strong = shared >= 3 and shared >= 0.8 * min(len(original.text.terms), len(terms))
        later = _later(original.pair[1], echo[1])
        if later or original.proof.audience != proof.audience or not proof.bound or not strong:
            linked = bool(self.lineage(original.pair) & self.lineage(echo))
        corrected = later and self.corrected(original.pair, echo)
        if later and not corrected:
            return False
        if original.proof.audience != proof.audience or not proof.bound:
            if not (linked or corrected):
                return False
        return strong or linked or corrected

    def originals(self, reader: str) -> list[_Original]:
        found = []
        for pair in self.ranked:
            self.check()
            candidate, obj = pair
            if obj.origin != "human_direct" or not _entries(obj):
                continue
            proof = _provenance(self.tx, pair, self.check)
            if proof is None or not proof.bound or proof.people != {reader}:
                continue
            targets = tuple(self.by_key[key] for key in replacements(obj) if key in self.by_key)
            if obj.temporal_status != "historical" or dict(obj.metadata).get("state") == "proposed":
                targets = (pair,)
            if targets:
                found.append(_Original(pair, proof, self.text(obj), targets))
        return found

    def candidates(self, terms, postings):
        """Count shared lexical terms with deadline checks before any lineage work."""
        counts = Counter()
        for term in terms:
            self.check()
            counts.update(postings.get(term, ()))
        return counts

    def select(self, reader: str):
        originals = self.originals(reader)
        postings = defaultdict(list)
        for index, original in enumerate(originals):
            for at, term in enumerate(original.text.terms):
                if at % 128 == 0:
                    self.check()
                postings[term].append(index)
        before, omitted, labelled = {}, set(), {}
        for echo in self.ranked:
            self.check()
            candidate, obj = echo
            if obj.origin != "assistant_visible" or candidate.source == "exact_ref" or not _entries(obj):
                continue
            proof = _provenance(self.tx, echo, self.check)
            if proof is None or proof.people - {reader}:
                continue
            text = self.text(obj)
            counts = self.candidates(text.terms, postings)
            for index in sorted(counts):
                self.check()
                original, shared = originals[index], counts[index]
                if shared < 2 or (
                    shared < 0.5 * min(len(original.text.terms), len(text.terms)) and not _later(original.pair[1], obj)
                ):
                    continue
                if not self.compatible(original, echo, proof, shared, text.terms):
                    continue
                before[candidate.key] = original.targets
                complete = original.text.complete and text.complete
                if complete and _contains(original.text.words, text.words, self.check):
                    omitted.add(candidate.key)
                else:
                    labelled[candidate.key] = mark_restatement(
                        obj, original.pair[1].evidence_refs, incomplete=not complete
                    )
                break
        return _ordered(self.ranked, before, omitted, labelled)


def _ordered(ranked, before, omitted, labelled) -> list[Pair]:
    """Move each anchor once while preserving every unrelated candidate's order."""
    result, emitted = [], set()
    for candidate, obj in ranked:
        for anchor, original in before.get(candidate.key, ()):
            if anchor.key not in emitted:
                result.append(
                    (anchor, replace(original, metadata=original.metadata + (("restatement_anchor", "true"),)))
                )
                emitted.add(anchor.key)
        if candidate.key not in emitted and candidate.key not in omitted:
            result.append((candidate, labelled.get(candidate.key, obj)))
            emitted.add(candidate.key)
    return result


def prefer_originals(tx, ranked: list[Pair], context, *, clock=None, gaps=None) -> list[Pair]:
    """Apply scoped first-hand priority, retaining uncertain or incompletely compared content.

    Missing person proofs and expired optional pairing preserve the original pool.
    Only complete consecutive token containment permits removing a restatement.
    """
    principal = context.trusted_context.source_principal
    reader = verified_human_principal_ref(principal.to_payload()) if principal is not None else None
    if context.mode not in {"auto", "current"} or reader is None or not any(_entries(obj) for _, obj in ranked):
        return ranked
    pool = _Pool(tx, ranked, context, clock)
    try:
        result = pool.select(reader)
        pool.check()
    except _Expired:
        if gaps is not None:
            gaps.append("restatement_pairing_incomplete")
        return ranked
    if pool.incomplete and gaps is not None:
        gaps.append("restatement_comparison_incomplete")
    return result
