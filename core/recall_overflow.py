"""Append bounded excerpts from matched sessions without replacing full evidence.

The list is a release-time view of existing retrieval candidates. It makes no
identity or event-count assertion and never participates in answer judgement.
"""

from __future__ import annotations

import json
from dataclasses import replace

from ..contracts import RecallPacket
from . import private
from .claim_storage import parse_source_ref
from .model_content import withhold_secret_items
from .recall_budget import canonical_render_json, estimate_tokens
from .recall_items import packet_item
from .recall_policy import meaningful_query_terms
from .recall_restatement import prefer_originals
from .retrieval import MAX_PACKET_ITEMS, SearchContext
from .summary_validity import CONVERSATION_SUMMARY_REASONS

LABEL = "Other-session matches"
MAX_ROWS = 12
MAX_UNITS = 1024
EXCERPT_CHARS = 160


def readable_sources(tx, obj, context):
    """Resolve raw evidence under the same current private and suppression policy."""
    sources = tuple(tx.source(*parse_source_ref(ref)) for ref in obj.evidence_refs)
    if not sources or any(source is None or source.suppressed for source in sources):
        return ()
    intervals = tx.remembered(("recall_private", context.now), lambda: private.active_intervals(tx, context.now))
    return () if any(private.source_blocked(intervals, source) for source in sources) else sources


def visible_query_pairs(tx, ranked, context, *, now):
    """Keep the ordinary evidence filter independent of optional list admission."""
    if context.mode not in {"auto", "current"} or context.focus_refs or len(ranked) < 3:
        return ranked
    policy = replace(context, now=now)
    return [pair for pair in ranked if pair[1].kind not in {"event", "claim"} or readable_sources(tx, pair[1], policy)]


def _session(source):
    return source.scope_id, source.entry_id, source.session_id


def _qualified(obj):
    return (
        obj.kind in {"event", "claim"}
        and dict(obj.metadata).get("qualification_reason") not in CONVERSATION_SUMMARY_REASONS
        and obj.temporal_status == "current"
        and not {"restatement_of", "replaced_by", "recall_echo"}.intersection(dict(obj.metadata))
    )


def _hydrate(tx, reader, candidates, context):
    reader.prefetch(tx, candidates, context)
    pairs = [
        (candidate, obj) for candidate in candidates if (obj := reader.hydrate(tx, candidate, context)) is not None
    ]
    return withhold_secret_items(pairs, [], content=lambda pair: pair[1])


def _representatives(tx, reader, result, context, packet, clock):
    """Select one remaining original per session, preserving source identity."""
    policy = replace(context, now=getattr(clock, "utc_now", lambda: context.now)())
    selected_refs = {ref for item in packet["items"] for ref in item["evidence_refs"]}
    roots = {}
    for candidate, obj in _hydrate(tx, reader, result.overflow_candidates, context):
        if not _qualified(obj):
            continue
        for source in readable_sources(tx, obj, policy):
            ref = f"{source.ref}@{source.revision}"
            if ref not in roots:
                roots[ref] = replace(candidate, kind="event", ref=source.ref, revision=source.revision)
    originals = _hydrate(tx, reader, tuple(roots.values()), context)
    originals = [pair for pair in originals if readable_sources(tx, pair[1], policy)]
    originals = prefer_originals(tx, originals, context, clock=clock.monotonic, gaps=[])
    rows, sessions = [], set()
    for candidate, obj in originals:
        if clock.monotonic() >= context.deadline:
            break
        source = tx.source(candidate.ref, candidate.revision)
        session = _session(source)
        if not _qualified(obj) or f"{obj.ref}@{obj.revision}" in selected_refs or session in sessions:
            continue
        rows.append((candidate, obj))
        sessions.add(session)
    return rows


def _excerpt(content, terms):
    positions = [content.casefold().find(term.casefold()) for term in terms]
    start = max(0, min((pos for pos in positions if pos >= 0), default=0) - 40)
    end = min(len(content), start + EXCERPT_CHARS)
    clipped = start > 0 or end < len(content)
    text = ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")
    # JSON quoting keeps each source on one physical line without altering
    # whitespace inside its excerpt or manufacturing an original quotation.
    return json.dumps(text, ensure_ascii=False), "clipped" if clipped else "complete excerpt"


def _item(rows, total, query):
    terms = meaningful_query_terms(query)
    lines = []
    for _, obj in rows:
        excerpt, status = _excerpt(obj.content, terms)
        when = dict(obj.metadata).get("occurred_at", "unknown")
        lines.append(
            f"- {when} [{obj.ref}@{obj.revision}] {obj.origin}; "
            f"Excerpt ({len(obj.content)} characters total; {status}): {excerpt}"
        )
    view = replace(
        rows[0][1],
        basis="derived_summary",
        evidence_refs=tuple(f"{obj.ref}@{obj.revision}" for _, obj in rows),
        content="More matching records from other sessions (excerpts, not deduplicated).\n"
        + f"Listed {len(rows)} records; {total - len(rows)} other session representatives not listed. "
        + "Use inspect on each ref for full text.\n"
        + "\n".join(lines),
        metadata=(("qualification_reason", LABEL),),
    )
    return packet_item(view)


def _fit(packet: RecallPacket, rows, context: SearchContext):
    total = len(rows)
    chosen = rows[:MAX_ROWS]
    while chosen:
        item = _item(chosen, total, context.query)
        extended: RecallPacket = {**packet, "items": [*packet["items"], item]}
        if (
            estimate_tokens(canonical_render_json(item)) <= min(MAX_UNITS, context.limits.budget_tokens // 4)
            and len(item["content"]) <= 12000
            and estimate_tokens(canonical_render_json(extended)) <= context.limits.budget_tokens
        ):
            return extended, chosen
        chosen.pop()
    return packet, ()


def _unchanged(tx, reader, context, packet, result, rows, epoch, revision):
    if reader.epoch(tx) != epoch or private.rule_revision(tx) != revision:
        return False
    selected = {(item["ref"], item["revision"]) for item in packet["items"]}
    candidates = tuple(c for c in result.candidates if (c.ref, c.revision) in selected) + tuple(c for c, _ in rows)
    fresh = _hydrate(tx, reader, candidates, context)
    if len(fresh) != len(candidates):
        return False
    expected = {candidate.key: obj for candidate, obj in rows}
    return all(
        readable_sources(tx, obj, context)
        and (candidate.key not in expected or (_qualified(obj) and obj.content == expected[candidate.key].content))
        for candidate, obj in fresh
        if obj.kind in {"event", "claim"}
    )


def append_overflow(storage, context: SearchContext, result, packet: RecallPacket, reader, clock) -> RecallPacket:
    """Extend a finalized packet using only spare budget and fresh ordinary reader exits.

    Failure or deadline exhaustion discards only the optional extension. The
    original packet has already passed its own release fence and admission.
    """
    selected = {(item["ref"], item["revision"]) for item in packet["items"]}
    if (
        not packet["items"]
        or len(packet["items"]) >= MAX_PACKET_ITEMS
        or all((candidate.ref, candidate.revision) in selected for candidate in result.overflow_candidates)
        or not result.overflow_candidates
        or context.mode not in {"auto", "current"}
        or context.focus_refs
        or clock.monotonic() >= context.deadline
    ):
        return packet
    try:
        with storage.read(
            context.trusted_context, remaining_seconds=max(0.001, context.deadline - clock.monotonic())
        ) as tx:
            revision = private.rule_revision(tx)
            if result.overflow_policy_revision is not None and revision != result.overflow_policy_revision:
                return packet
            epoch = reader.epoch(tx)
            retracted = getattr(reader, "retracted_since", None)
            if (
                retracted is not None
                and packet["memory_epoch"] is not None
                and retracted(tx, context, packet["memory_epoch"])
            ):
                return packet
            rows = _representatives(tx, reader, result, context, packet, clock)
        if len(rows) < 3 or clock.monotonic() >= context.deadline:
            return packet
        extended, chosen = _fit(packet, rows, context)
        if not chosen:
            return packet
        with storage.read(
            context.trusted_context, remaining_seconds=max(0.001, context.deadline - clock.monotonic())
        ) as tx:
            policy = replace(context, now=getattr(clock, "utc_now", lambda: context.now)())
            if not _unchanged(tx, reader, policy, packet, result, chosen, epoch, revision):
                return packet
        return extended if clock.monotonic() < context.deadline else packet
    except Exception:  # noqa: BLE001 - Optional reads cannot revoke a finalized packet.
        # Optional evidence cannot turn a successfully released full packet
        # into a partial or unavailable one when its own read fails.
        return packet
