"""Completed summary parts, processed input ranges and source-preserving reuse."""

from __future__ import annotations

import json

from ..contracts import ContractError, validate_payload
from . import lineage
from .claims import Qualification
from .consolidation_summary import grounded_sentences
from .summary_validity import obsolete_ranges


def unprocessed_ranges(tx, group):
    """Return gaps in input traversal, independent of which characters were quoted."""
    processed = {}
    for row in tx._check().execute(
        "SELECT coverage_json FROM summary_parts WHERE group_key=? AND generation=?",
        (group["group_key"], group["generation"]),
    ):
        for ref, revision, start, end, _total in json.loads(row[0]):
            processed.setdefault((ref, revision), []).append((start, end))
    gaps = []
    for index, member in enumerate(json.loads(group["manifest_json"])):
        position = 0
        for start, end in sorted(processed.get((member["ref"], member["revision"]), ())):
            if start > position:
                gaps.append((index, position, start))
            position = max(position, end)
        if position < member["total"]:
            gaps.append((index, position, member["total"]))
    return gaps


def update_progress(tx, group):
    """Persist traversal progress from completed ranges, including reused parts."""
    gaps = unprocessed_ranges(tx, group)
    members = json.loads(group["manifest_json"])
    covered = sum(m["total"] for m in members) - sum(end - start for _i, start, end in gaps)
    next_offset = gaps[0][0] if gaps else len(members)
    state = "pending" if gaps else "complete"
    tx._check(write=True).execute(
        "UPDATE summary_groups SET covered_units=?,next_offset=?,state=? WHERE group_key=?",
        (covered, next_offset, state, group["group_key"]),
    )
    return dict(group, covered_units=covered, next_offset=next_offset, state=state)


def queue_summary_part(tx, group, ordinal, now):
    """Create one ordinary claim-backed work item for the first unprocessed input."""
    members = json.loads(group["manifest_json"])
    member = members[group["next_offset"]]
    source = tx.source(member["ref"], member["revision"])
    if source is None or source.suppressed:
        raise ContractError("SOURCE_MISSING", "summary_source")
    proposal = dict(
        kind="fact",
        subject="summary:" + group["group_key"] + ":" + str(ordinal),
        predicate="summary",
        value_text="Conversation summary",
        conditions=[],
        statement_kind="assertion",
        valid_from=None,
        valid_to=None,
        evidence_spans=[
            dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"][:1])
        ],
    )
    previous = tx.claims.slot(group["scope_id"], proposal)
    saved = tx.claims.append(
        group["scope_id"],
        proposal,
        Qualification("proposed", "derived_summary", "summary_pending"),
        recorded_at=now,
        previous=previous[-1] if previous else None,
    )
    tx._check().execute(
        "INSERT INTO summary_parts VALUES(?,?,?,?,?,?,?)",
        (group["group_key"], group["generation"], ordinal, saved.ref, saved.revision, "[]", group["signature"]),
    )
    tx.work.enqueue("rebuild_projection", saved.ref, saved.revision, available_at=now)


def store_excerpt_claim(tx, group, head, sentences, spans, coverage, *, now):
    """Publish safe quotes, or an unreadable processed part when none remains."""
    roles = {}
    for ref, revision, _start, _end, _total in coverage:
        source = tx.source(ref, revision)
        tx.claims.require_live_source(ref, revision)
        if source is None or source.suppressed or source.capture_gaps:
            raise ContractError("SOURCE_MISSING", "summary_source")
        roles[(ref, revision)] = source.event["role"]
    text = "\n".join(
        f"{roles[(span['source_ref'], span['source_revision'])]}: {sentence}"
        for sentence, span in zip(sentences, spans, strict=True)
    )
    # A processed part with no current quotes remains useful for progress, but
    # never becomes readable summary content or causes a second model request.
    payload = dict(
        head.payload,
        value_text=text or "No current source sentence.",
        evidence_spans=spans or head.payload["evidence_spans"],
    )
    validate_payload(
        "consolidation_result",
        dict(
            protocol_version="1.1",
            source_refs=[f"{ref}@{rev}" for ref, rev in roles],
            claim_proposals=[payload],
            resume_proposals=[],
            reference_proposals=[],
        ),
    )
    saved = tx.claims.append(
        group["scope_id"],
        payload,
        Qualification("active" if spans else "retracted", "derived_summary", "grounded_" + group["kind"] + "_summary"),
        previous=head,
        recorded_at=now,
    )
    for ref, revision in roles:
        lineage.link(tx._check(), "claim", saved.ref, saved.revision, ref, revision, relation="derived_from")
    return saved


def reuse_completed_parts(tx, previous, group, *, now):
    """Transfer unchanged completed input pages and prune obsolete quotes without asking a model."""
    members = {(m["ref"], m["revision"]): m for m in json.loads(group["manifest_json"])}
    old_members = {(m["ref"], m["revision"]): m for m in json.loads(previous["manifest_json"])}
    blocked = obsolete_ranges(tx, list(members.values()))
    rows = (
        tx._check()
        .execute(
            "SELECT * FROM summary_parts WHERE group_key=? AND generation=? AND coverage_json!='[]' ORDER BY ordinal",
            (group["group_key"], previous["generation"]),
        )
        .fetchall()
    )
    for row in rows:
        coverage = json.loads(row["coverage_json"])
        if not all(
            (ref, rev) in members
            and (ref, rev) in old_members
            and members[(ref, rev)]["digest"] == old_members[(ref, rev)]["digest"]
            and members[(ref, rev)]["total"] == total
            for ref, rev, _start, _end, total in coverage
        ):
            continue
        head = tx.claims.version(row["claim_ref"], row["claim_revision"])
        if head is None or head.current_revision != head.revision or head.suppressed:
            continue
        sources = tuple(tx.source(ref, rev) for ref, rev in dict.fromkeys((c[0], c[1]) for c in coverage))
        if any(source is None or source.suppressed or source.capture_gaps for source in sources):
            continue
        value = dict(
            sentences=[
                dict(text=span["quote"], source_ref=span["source_ref"], source_revision=span["source_revision"])
                for span in head.payload["evidence_spans"]
            ]
        )
        sentences, spans = (
            grounded_sentences(value, sources, blocked=blocked, positions=head.payload["evidence_spans"])
            if head.state == "active"
            else ([], [])
        )
        roles = {(source.ref, source.revision): source.event["role"] for source in sources}
        expected = "\n".join(
            f"{roles[(p['source_ref'], p['source_revision'])]}: {text}"
            for text, p in zip(sentences, spans, strict=True)
        )
        saved = head
        if head.state == "active" and (
            expected != head.payload["value_text"] or spans != head.payload["evidence_spans"]
        ):
            saved = store_excerpt_claim(tx, group, head, sentences, spans, coverage, now=now)
        tx._check(write=True).execute(
            "UPDATE summary_parts SET generation=?,claim_revision=?,dependencies_json=? "
            "WHERE group_key=? AND generation=? AND ordinal=?",
            (
                group["generation"],
                saved.revision,
                group["signature"],
                group["group_key"],
                previous["generation"],
                row["ordinal"],
            ),
        )
    return update_progress(tx, group)
