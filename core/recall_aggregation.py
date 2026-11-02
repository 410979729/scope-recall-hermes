"""Fold bounded, attributable cross-session evidence before packet admission.

This is a view of the admitted candidate pool, never a stored fact or a claim
that all historical occurrences have been found. Release rehydrates every
member through the ordinary reader; a changed member invalidates the view.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace

from . import private
from .claim_storage import parse_source_ref
from .events import lexical_terms
from .recall_budget import canonical_render_json, estimate_tokens
from .recall_items import packet_item
from .recall_needs import statement_text
from .recall_policy import meaningful_query_terms
from .retrieval import CandidateRef, RetrievedObject, optional_json

Pair = tuple[CandidateRef, RetrievedObject]
MAX_MEMBERS = 12
MAX_UNITS = 2048
QUOTE_CHARS = 180
COMPARISON_CHARS = 32768


def aggregation_candidates(candidate: CandidateRef, obj: RetrievedObject) -> tuple[CandidateRef, ...]:
    """The underlying candidates to prefetch and revalidate, without a public schema extension."""
    rows = optional_json(dict(obj.metadata).get("aggregation_members"))
    if not isinstance(rows, list):
        return (candidate,)
    return tuple(CandidateRef(kind, ref, revision, "lexical") for kind, ref, revision in rows)


def _sources(tx, obj, context):
    sources = [tx.source(*parse_source_ref(ref)) for ref in obj.evidence_refs]
    if not sources or any(source is None or source.suppressed for source in sources):
        return ()
    intervals = tx.remembered(("aggregation_private", context.now), lambda: private.active_intervals(tx, context.now))
    return tuple(sources) if not any(private.source_blocked(intervals, source) for source in sources) else ()


def _audience(sources):
    """Unbound speakers and evidence crossing audiences cannot support one counted group."""
    keys = set()
    for source in sources:
        principal = source.event.get("source_principal", {})
        if principal.get("kind") != "human" or principal.get("resolution") != "verified":
            return None
        keys.add((source.scope_id, source.project_id, source.branch_id, principal.get("principal_ref")))
    return next(iter(keys)) if len(keys) == 1 else None


def _group_key(tx, pair, terms, context):
    candidate, obj = pair
    metadata = dict(obj.metadata)
    if (
        obj.kind not in {"event", "claim"}
        or obj.origin != "human_direct"
        or obj.temporal_status != "current"
        or candidate.source in {"exact_ref", "background"}
        or {"restatement_of", "replaced_by"}.intersection(metadata)
        or len(obj.content) > COMPARISON_CHARS
    ):
        return None
    shared = tuple(sorted(terms.intersection(lexical_terms(statement_text(obj)))))
    if not shared:
        return None
    sources = _sources(tx, obj, context)
    audience = _audience(sources)
    if audience is None:
        return None
    if obj.kind == "claim":
        payload = optional_json(obj.content)
        if metadata.get("state") != "active" or not isinstance(payload, dict):
            return None
        subject, predicate = payload.get("subject"), payload.get("predicate")
        if not isinstance(subject, str) or not isinstance(predicate, str):
            return None
        return audience, ("claim", (subject, predicate)), sources
    # Raw messages have no normalized predicate. Require a shared lexical
    # anchor and at least three shared meaningful body terms; no question templates
    # or inferred entity identities take part in grouping.
    return audience, ("event", shared), sources


def _quote(text: str, terms) -> str:
    """A verbatim bounded window, with explicit ellipses rather than a generated paraphrase."""
    positions = [text.casefold().find(term) for term in terms]
    start = max(0, min((pos for pos in positions if pos >= 0), default=0) - 40)
    end = min(len(text), start + QUOTE_CHARS)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _summary(pairs, sources, terms, budget, total):
    """Build one schema-compatible item whose count, dates and refs describe exactly its visible rows."""
    anchor, original = pairs[0]
    rows, refs, stamps, sessions = [], [], [], set()
    for (_candidate, obj), cited in zip(pairs, sources, strict=True):
        when = dict(obj.metadata).get("occurred_at") or "unknown"
        stamps.append(when)
        sessions.update((source.entry_id, source.session_id) for source in cited)
        own_refs = tuple(dict.fromkeys((f"{obj.ref}@{obj.revision}", *obj.evidence_refs)))
        # The body contains fact versions as well as every original-source
        # version. The public evidence array remains a list of raw sources.
        quotes = " | ".join(_quote(source.event["content"], terms) for source in cited)
        rows.append(f"- {when} [{', '.join(own_refs)}] {quotes}")
        refs.extend(obj.evidence_refs)
    known = [stamp for stamp in stamps if stamp != "unknown"]
    span = f"{min(known)} to {max(known)}" if known else "unknown"
    missing = "; some dates unknown" if len(known) != len(stamps) else ""
    body = (
        f"Cross-session evidence: {len(rows)} records across {len(sessions)} sessions; "
        f"time range {span}{missing}. Showing {len(rows)} of {total} matching candidate records; "
        "not a lifetime occurrence count.\n" + "\n".join(rows)
    )
    members = [(candidate.kind, candidate.ref, candidate.revision) for candidate, _ in pairs]
    metadata = tuple((key, value) for key, value in original.metadata if key not in {"payload_json", "state"})
    summary = replace(
        original,
        content=body,
        evidence_refs=tuple(dict.fromkeys(refs)),
        basis="derived_summary",
        metadata=metadata
        + (
            ("qualification_reason", "Cross-session evidence"),
            ("aggregation_members", json.dumps(members)),
            ("aggregation_total", str(total)),
        ),
    )
    # Include the existing item envelope in the half-packet allocation; the
    # compiler still weighs the complete packet and may withhold this item.
    if len(summary.evidence_refs) > 32 or len(body) > 12000:
        return None
    return (anchor, summary) if estimate_tokens(canonical_render_json(packet_item(summary))) <= budget else None


def _groups(tx, ranked, context, terms, clock):
    """Index overlapping query anchors and cache body terms once per candidate."""
    groups, bodies = defaultdict(list), {}
    for pair in ranked:
        if clock() >= context.deadline:
            return None, bodies
        found = _group_key(tx, pair, terms, context)
        if found is None:
            continue
        audience, key, sources = found
        bodies[pair[0].key] = set(meaningful_query_terms(statement_text(pair[1])[:8192]))
        # Extra matching words must not split one event class into separate
        # groups. Every proposed anchor still needs shared body evidence.
        keys = [("event", (term,)) for term in key[1]] if key[0] == "event" else [key]
        for anchor in keys:
            groups[(audience, anchor)].append((pair, sources))
    return groups, bodies


def _unique(entries, protected):
    """Fold repeated bodies and shared source roots, keeping session coverage ahead of repeats."""
    primary, first, later, refs, bodies, sessions = [], [], [], set(), set(), set()
    for pair, sources in sorted(entries, key=lambda entry: entry[0][0].key not in protected):
        obj = pair[1]
        body = (obj.kind, obj.content)
        if refs.intersection(obj.evidence_refs) or body in bodies:
            continue
        refs.update(obj.evidence_refs)
        bodies.add(body)
        found = {(s.entry_id, s.session_id) for s in sources}
        if pair[0].key in protected:
            primary.append((pair, sources))
        else:
            (first if found - sessions else later).append((pair, sources))
        sessions.update(found)
    return primary + first + later


def _fold_group(entries, terms, budget, bodies, protected):
    """Only a distributed group with a shared raw event vocabulary can become one view."""
    sessions = {(s.entry_id, s.session_id) for _, sources in entries for s in sources}
    if len(sessions) < 3:
        return None
    common = set.intersection(*(bodies[pair[0].key] for pair, _ in entries))
    if entries[0][0][1].kind == "event" and len(common) < 3:
        return None
    unique = _unique(entries, protected)
    required = {ref for pair, _ in entries if pair[0].key in protected for ref in pair[1].evidence_refs}
    chosen = unique[:MAX_MEMBERS]
    while len(chosen) >= 3:
        retained = {ref for pair, _ in chosen for ref in pair[1].evidence_refs}
        if not required <= retained:
            return None
        covered = {(s.entry_id, s.session_id) for _, sources in chosen for s in sources}
        if len(covered) < 3:
            break
        summary = _summary([p for p, _ in chosen], [s for _, s in chosen], terms, budget, len(unique))
        if summary is not None:
            return summary, len(chosen) < len(unique)
        chosen.pop()
    return None


def aggregate_evidence(
    tx, ranked: list[Pair], context, *, clock, gaps: list[str], protected=(), now=None
) -> list[Pair]:
    """Group readable hits by normalized fact key or raw lexical anchor, without expanding retrieval."""
    if context.mode not in {"auto", "current"} or context.focus_refs or len(ranked) < 3:
        return ranked
    context = replace(context, now=now or context.now)
    ranked = [pair for pair in ranked if pair[1].kind not in {"event", "claim"} or _sources(tx, pair[1], context)]
    sessions = {(s.entry_id, s.session_id) for _, obj in ranked for s in _sources(tx, obj, context)}
    if len(sessions) < 3:
        return ranked
    terms = frozenset(meaningful_query_terms(context.query))
    groups, bodies = _groups(tx, ranked, context, terms, clock)
    if groups is None:
        return ranked
    replacements, omitted = {}, set()
    budget = min(MAX_UNITS, context.limits.budget_tokens // 2)
    for _, entries in sorted(groups.items(), key=lambda entry: (entry[0][1][0] != "claim", -len(entry[1]))):
        if clock() >= context.deadline:
            return ranked
        entries = [(pair, sources) for pair, sources in entries if pair[0].key not in omitted]
        consumed = {pair[0].key for pair, _ in entries}
        consumed.update(("event", *parse_source_ref(ref)) for pair, _ in entries for ref in pair[1].evidence_refs)
        # Compaction may collect tail evidence, but cannot promote a tail-only
        # cluster ahead of the original admission or spend unrelated hits' room.
        if protected and not any(candidate.key in consumed for candidate, _ in protected):
            continue
        reserved = sum(
            estimate_tokens(canonical_render_json(packet_item(obj)))
            for candidate, obj in protected
            if candidate.key not in consumed
        )
        allowance = min(budget, context.limits.budget_tokens - reserved - 256)
        folded = _fold_group(entries, terms, allowance, bodies, {candidate.key for candidate, _ in protected})
        if folded is None:
            continue
        summary, truncated = folded
        anchor, obj = summary
        obj = replace(obj, metadata=obj.metadata + (("aggregation_private_revision", str(private.rule_revision(tx))),))
        replacements[anchor.key] = (anchor, obj)
        omitted.update(pair[0].key for pair, _ in entries)
        omitted.update(("event", *parse_source_ref(ref)) for pair, _ in entries for ref in pair[1].evidence_refs)
        budget -= estimate_tokens(canonical_render_json(packet_item(obj)))
        if truncated:
            gaps.append("aggregation_truncated")
    result = [replacements.get(c.key, (c, o)) for c, o in ranked if c.key in replacements or c.key not in omitted]
    if not replacements:
        return result
    primary = {candidate.key for candidate, _ in protected}
    # Folding must not reopen size admission for previously rejected, bulky
    # neighbors. Retain the original admitted prefix, then fill freed slots.
    result.sort(key=lambda pair: pair[0].key not in primary)
    return [
        (candidate, replace(obj, metadata=obj.metadata + (("aggregation_priority", "true"),)))
        if candidate.key in primary and "aggregation_members" not in dict(obj.metadata)
        else (candidate, obj)
        for candidate, obj in result
    ]


def recheck_aggregation(tx, context, candidate, obj, reader, *, now=None):
    """Rebuild the exact view from fresh ordinary hydration; any changed member withholds the view."""
    metadata = dict(obj.metadata)
    if "aggregation_members" not in metadata:
        return None
    pairs, sources = [], []
    policy = replace(context, now=now or context.now)
    revision = int(metadata.get("aggregation_private_revision", "0"))
    # A newly opened full rule invalidates the view even when it closed before
    # publication. Rules use the primary-key suffix, independent of a frozen
    # query timestamp and of how much closed privacy history the store holds.
    changed = tx.remembered(
        ("aggregation_private_changes", revision),
        lambda: [
            dict(row)
            for row in tx._check().execute(
                "SELECT * FROM private_intervals WHERE interval_id>? AND mode='full'", (revision,)
            )
        ],
    )
    terms = frozenset(meaningful_query_terms(context.query))
    for member in aggregation_candidates(candidate, obj):
        fresh = reader.hydrate(tx, member, context)
        if fresh is None or _group_key(tx, (member, fresh), terms, policy) is None:
            return None
        cited = _sources(tx, fresh, policy)
        if any(private.source_blocked(changed, source) for source in cited):
            return None
        pairs.append((member, fresh))
        sources.append(cited)
    rebuilt = _summary(
        pairs, sources, terms, min(MAX_UNITS, context.limits.budget_tokens // 2), int(metadata["aggregation_total"])
    )
    if rebuilt is None or rebuilt[1].content != obj.content:
        return None
    fresh = rebuilt[1]
    return replace(fresh, metadata=fresh.metadata + (("aggregation_private_revision", str(revision)),))
