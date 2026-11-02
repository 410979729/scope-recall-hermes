"""Prefer attributable first-hand evidence within the admitted candidate pool."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from ..contracts import ContractError
from . import lineage
from .claim_storage import parse_source_ref
from .claims import canonical_time
from .deadline import RequestDeadline
from .events import lexical_terms
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
    """Whitespace-normalized body and bounded terms for candidate preselection."""

    body: str
    terms: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class _Provenance:
    """Audience and installation-owner proofs for scoped sources and fact versions."""

    audience: tuple[str, str | None, str | None]
    owners: frozenset[str]
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


def _provenance(tx, pair: Pair, check, owner) -> _Provenance | None:
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
    kind = "human" if obj.origin == "human_direct" else "assistant"
    if any(source.event.get("source_principal", {}).get("kind") != kind for source in sources):
        return None
    owners = [owner(source.entry_id, source.event.get("source_principal")) for source in sources]
    return _Provenance(
        (node.scope_id, node.project_id, node.branch_id),
        frozenset(person for person in owners if person is not None),
        all(person is not None for person in owners),
        frozenset(source.entry_id for source in sources),
    )


def _later(first: RetrievedObject, echo: RetrievedObject) -> bool:
    try:
        a = canonical_time(dict(first.metadata).get("occurred_at"))
        b = canonical_time(dict(echo.metadata).get("occurred_at"))
    except ContractError:
        return False
    return bool(a and b and a > b)


def mark_restatement(obj: RetrievedObject, refs: tuple[str, ...], *, incomplete=False) -> RetrievedObject:
    """Keep attribution in existing public provenance fields and internal release proofs."""
    # The existing packet field is bounded; never publish a truncated source identity.
    entry = next(iter(sorted(_entries(obj))), "")
    qualification = dict(obj.metadata).get("qualification_reason")
    suffix = f"; {qualification}" if qualification else ""
    descriptions = [f"{entry} retelling of {ref}{suffix}" for ref in refs] + [
        f"Retelling of {ref}{suffix}" for ref in refs
    ]
    reason = next((description for description in descriptions if len(description) <= 120), None)
    if reason is None:
        return obj
    metadata = tuple(
        (key, value)
        for key, value in obj.metadata
        if key not in {"restatement_of", "restatement_comparison", "restatement_qualification"}
    )
    return replace(
        obj,
        basis="derived_summary",
        metadata=metadata
        + (("restatement_of", json.dumps(refs)), ("restatement_qualification", reason))
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
        self.owners: dict[tuple[str, str, str], str | None] = {}
        self.owner_snapshot: dict = {}
        self.roots: dict[tuple[str, str, int], set[str]] = {}
        self.characters = COMPARISON_TOTAL_CHARACTERS
        self.incomplete = False
        self.before, self.omitted, self.labelled = {}, set(), {}

    def check(self) -> None:
        if self.deadline.exhausted():
            raise _Expired

    def owner(self, entry, principal) -> str | None:
        """Resolve a speaker through the host's owner binding, cached only in this pool."""
        resolve = self.context.trusted_context.owner_resolver
        if not entry or resolve is None:
            return None
        if principal is not None and principal.get("resolution") != "verified":
            return None
        # A read belongs to its attested entry owner, independently of the read's
        # actor kind. Stored sources always supply their own checked speaker.
        key = (
            (entry, "entry_owner", "")
            if principal is None
            else (entry, principal.get("kind"), principal.get("principal_ref"))
        )
        if key not in self.owners:
            self.check()
            value = resolve(entry, None if principal is None else dict(principal), self.owner_snapshot)
            self.owners[key] = value if isinstance(value, str) and value else None
            self.check()
        return self.owners[key]

    def text(self, obj: RetrievedObject) -> _Text:
        self.check()
        if obj.content not in self.texts:
            limit = min(COMPARISON_CHARACTERS, self.characters)
            body, statement = obj.content[:limit], statement_text(obj)
            complete = len(obj.content) <= limit and len(statement) <= limit
            self.characters -= min(limit, len(obj.content))
            self.texts[obj.content] = _Text(
                " ".join(body.split()), frozenset(lexical_terms(statement[:limit])), complete
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
        if later or original.proof.audience != proof.audience or not strong:
            linked = bool(self.lineage(original.pair) & self.lineage(echo))
        corrected = later and self.corrected(original.pair, echo)
        if later and not corrected:
            return False
        if original.proof.audience != proof.audience:
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
            proof = _provenance(self.tx, pair, self.check, self.owner)
            if proof is None or not proof.bound or proof.owners != {reader}:
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
        for echo in self.ranked:
            self.check()
            candidate, obj = echo
            if obj.origin != "assistant_visible" or candidate.source == "exact_ref" or not _entries(obj):
                continue
            proof = _provenance(self.tx, echo, self.check, self.owner)
            if proof is None or not proof.bound or proof.owners != {reader}:
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
                complete = original.text.complete and text.complete
                labelled = mark_restatement(obj, original.pair[1].evidence_refs, incomplete=not complete)
                if labelled is obj:
                    continue
                self.check()
                # Publish a relation only after all its reads and comparisons finish.
                self.before[candidate.key] = original.targets
                self.labelled[candidate.key] = labelled
                if complete and original.text.body == text.body:
                    self.omitted.add(candidate.key)
                break
        return self.result()

    def result(self, *, retain_all=False) -> list[Pair]:
        """Completed relations survive a cutoff; an interrupted pass never removes content."""
        return _ordered(self.ranked, self.before, set() if retain_all else self.omitted, self.labelled)


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

    Missing owner proofs preserve the original pool. A cutoff keeps completed pairs
    and every body. Only complete body equality after whitespace normalization permits omission.
    """
    if context.mode not in {"auto", "current"}:
        return ranked
    origins = {obj.origin for _, obj in ranked if _entries(obj)}
    if not {"human_direct", "assistant_visible"} <= origins:
        return ranked
    trusted = context.trusted_context
    if trusted.owner_resolver is None:
        return ranked
    pool = _Pool(tx, ranked, context, clock)
    try:
        reader = pool.owner(trusted.entry_id, None)
        if reader is None:
            return ranked
        result = pool.select(reader)
        pool.check()
    except _Expired:
        if gaps is not None:
            gaps.append("restatement_pairing_incomplete")
        return pool.result(retain_all=True)
    if pool.incomplete and gaps is not None:
        gaps.append("restatement_comparison_incomplete")
    return result
