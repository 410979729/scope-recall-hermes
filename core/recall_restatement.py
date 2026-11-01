"""Prefer first-hand evidence to matching cross-entry restatements in one candidate pool."""

from __future__ import annotations

import json
from dataclasses import replace

from ..contracts import ContractError
from . import lineage
from .claim_storage import parse_source_ref
from .recall_needs import statement_text
from .recall_policy import meaningful_query_terms
from .retrieval import CandidateRef, RetrievedObject, optional_json
from .retrieval_hydration import replacements
from .visibility import allowed

Pair = tuple[CandidateRef, RetrievedObject]
#: Unlinked texts must share most of the smaller statement, beyond a shared topic alone.
SAME_CONTENT_COVERAGE = 0.8
#: A short unmatched lead-in can attribute otherwise fully covered content.
MAX_LEAD_IN_TERMS = 4
#: Evidence traversal is limited to the current candidate pool's lineage, never a corpus scan.
LINEAGE_LIMIT = 64


def _entries(obj: RetrievedObject) -> frozenset[str]:
    labels = optional_json(dict(obj.metadata).get("entries"))
    return (
        frozenset(label["id"] for label in labels if isinstance(label, dict) and "id" in label)
        if isinstance(labels, list)
        else frozenset()
    )


def _roots(tx, obj: RetrievedObject) -> set[str]:
    """Follow captured references and claim evidence, bounded and cycle safe in this snapshot."""
    pending = list(obj.evidence_refs)
    found: set[str] = set()
    while pending and len(found) < LINEAGE_LIMIT:
        ref = pending.pop()
        if ref in found:
            continue
        found.add(ref)
        try:
            key, revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(key, revision)
        if source is not None:
            pending.extend(source.event.get("evidence_refs", ()))
        elif allowed(tx, "claim", key):
            evidence = tx.remembered(
                ("restatement_evidence", key, revision),
                lambda key=key, revision=revision: lineage.evidence(tx._check(), "claim", key, revision),
            )
            pending.extend(f"{key}@{revision}" for key, revision in evidence)
    return found


def _matches(original: set[str], echo: set[str], linked: bool) -> bool:
    """Lineage permits paraphrase; lexical containment needs several shared content terms."""
    shared = len(original & echo)
    return (linked and shared >= 2 and shared >= 0.5 * min(len(original), len(echo))) or (
        shared >= 3 and shared >= SAME_CONTENT_COVERAGE * min(len(original), len(echo))
    )


def _adds_information(obj: RetrievedObject, covered: set[str], terms: set[str]) -> bool:
    """Keep every unmatched content term after the shared statement begins.

    Only a short contiguous lead-in may be attribution. No language templates or
    speaker phrases are recognized; uncertain paraphrases remain available.
    """
    novel = terms - covered
    if not novel:
        return False
    text = statement_text(obj).casefold()
    starts = [text.find(term) for term in terms & covered if term in text]
    if not starts or len(novel) > MAX_LEAD_IN_TERMS:
        return True
    first = min(starts)
    return any((at := text.find(term)) < 0 or at >= first for term in novel)


def mark_restatement(obj: RetrievedObject, refs: tuple[str, ...]) -> RetrievedObject:
    """Annotate a released restatement without changing its body or source authority."""
    return replace(
        obj,
        metadata=tuple((key, value) for key, value in obj.metadata if key != "restatement_of")
        + (("restatement_of", json.dumps(refs)),),
    )


def _matching_originals(tx, originals, echo: Pair, terms, roots, by_key, entries):
    """Find visible anchors, including current replacements of a corrected source."""
    candidate, obj = echo
    anchors, covered, refs = [], set(), set()
    for first, original in originals:
        if not entries[first.key].isdisjoint(entries[candidate.key]):
            continue
        original_at = dict(original.metadata).get("occurred_at", "")
        echo_at = dict(obj.metadata).get("occurred_at", "")
        if original_at and echo_at and original_at > echo_at:
            continue
        for c, item in ((first, original), (candidate, obj)):
            if c.key not in roots:
                roots[c.key] = _roots(tx, item)
        linked = bool(roots[first.key] & roots[candidate.key])
        if not _matches(terms[first.key], terms[candidate.key], linked):
            continue
        targets = [by_key[key] for key in replacements(original) if key in by_key]
        if original.temporal_status != "historical" or dict(original.metadata).get("state") == "proposed":
            targets = [(first, original)]
        if targets:
            anchors.extend(targets)
            covered.update(terms[first.key])
            refs.update(original.evidence_refs)
    return anchors, covered, refs


def _ordered(ranked, before, omitted, labelled) -> list[Pair]:
    """Preserve unrelated order while moving each first-hand anchor ahead of its echo."""
    result, emitted = [], set()
    for candidate, obj in ranked:
        for anchor, original in before.get(candidate.key, ()):
            if anchor.key not in emitted and anchor.key not in omitted:
                result.append(
                    (anchor, replace(original, metadata=original.metadata + (("restatement_anchor", "true"),)))
                )
                emitted.add(anchor.key)
        if candidate.key not in emitted and candidate.key not in omitted:
            result.append((candidate, labelled.get(candidate.key, obj)))
            emitted.add(candidate.key)
    return result


def prefer_originals(tx, ranked: list[Pair], context) -> list[Pair]:
    """Order matching first-hand candidates before another entry's later retelling.

    Only admitted, visible candidates compete. Historical reads and exact reference
    lookups keep their order. A retelling with substantial unmatched content survives
    with attribution; an absent or unrelated original cannot suppress assistant evidence.
    """
    if context.mode not in {"auto", "current"}:
        return ranked
    originals = [(candidate, obj) for candidate, obj in ranked if obj.origin == "human_direct" and _entries(obj)]
    echoes = [
        (candidate, obj)
        for candidate, obj in ranked
        if obj.origin == "assistant_visible" and candidate.source != "exact_ref" and _entries(obj)
    ]
    if not originals or not echoes:
        return ranked
    terms = {
        candidate.key: set(meaningful_query_terms(statement_text(obj))) for candidate, obj in (*originals, *echoes)
    }
    entries = {candidate.key: _entries(obj) for candidate, obj in (*originals, *echoes)}
    roots: dict[tuple[str, str, int], set[str]] = {}
    by_key = {candidate.key: (candidate, obj) for candidate, obj in ranked}
    before: dict[tuple[str, str, int], list[Pair]] = {}
    omitted, labelled = set(), {}
    for candidate, obj in echoes:
        anchors, covered, refs = _matching_originals(tx, originals, (candidate, obj), terms, roots, by_key, entries)
        if not anchors:
            continue
        before[candidate.key] = anchors
        echo_terms = terms[candidate.key]
        if echo_terms and not _adds_information(obj, covered, echo_terms):
            omitted.add(candidate.key)
        else:
            labelled[candidate.key] = mark_restatement(obj, tuple(sorted(refs)))
    return _ordered(ranked, before, omitted, labelled)
