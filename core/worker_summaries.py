"""Grounded conversation summarization through the existing leased, budgeted model port."""

from __future__ import annotations

import json
from functools import partial

from ..contracts import ContractError, validate_payload
from . import lineage, private
from .claims import Qualification
from .consolidation_chunks import source_chunk
from .consolidation_summary import grounded_sentences
from .language_resources import prompt_text
from .resume_compaction import resume_evidence_refs
from .summary_storage import queue_summary_part
from .summary_validity import group_members, group_signature
from .worker_outcomes import budget_left, finalize_work, hold_for_private_mode, model_failure, stale_result, work_result


def summary_messages(sources, *, episode_ref=None):
    """Format bounded raw windows; task recovery is a hint, never authority for generated text."""
    records = []
    for source in sources:
        window = getattr(source, "consolidation_window", None)
        records.append(
            dict(
                ref=source.ref,
                revision=source.revision,
                content=source.event["content"],
                role=source.event["role"],
                start=window.start if window is not None else 0,
            )
        )
    body = dict(sources=records, task=episode_ref)
    messages = [
        dict(role="system", content=prompt_text("session_summary")),
        dict(role="user", content=json.dumps(body, ensure_ascii=False)),
    ]
    if sum(len(m["content"].encode()) for m in messages) > 12000:
        raise ContractError("INPUT_INVALID", "consolidation_input_budget")
    return messages


def _read_page(tx, group):
    members = json.loads(group["manifest_json"])
    index = group["next_offset"]
    sources = []
    for member in members[index : index + 16]:
        source = tx.source(member["ref"], member["revision"])
        if source is None or source.suppressed or source.capture_gaps:
            raise ContractError("SOURCE_MISSING", "summary_source")
        tx.claims.require_live_source(source.ref, source.revision)
        try:
            summary_messages((*sources, source))
        except ContractError:
            break
        sources.append(source)
    if not sources:
        member = members[index]
        source = tx.source(member["ref"], member["revision"])
        offset = group["covered_units"] - sum(m["total"] for m in members[:index])
        source, _chunk = source_chunk(source, offset, formatter=summary_messages)
        sources = [source]
    coverage = []
    for source in sources:
        window = getattr(source, "consolidation_window", None)
        coverage.append(
            [
                source.ref,
                source.revision,
                window.start if window else 0,
                window.end if window else len(source.event["content"]),
                window.total if window else len(source.event["content"]),
            ]
        )
    return tuple(sources), coverage


def _job(tx, item):
    return (
        tx._check()
        .execute(
            "SELECT g.*,p.ordinal,p.claim_ref,p.claim_revision FROM summary_parts p JOIN "
            "summary_groups g USING(group_key) "
            "WHERE p.claim_ref=? AND p.claim_revision=? AND p.generation=g.generation AND p.coverage_json='[]'",
            (item.subject_ref, item.subject_revision),
        )
        .fetchone()
    )


def _outbound_blocked(storage, clock, context, sources, group):
    """Recheck deletion, authorization and source versions at each transport boundary, beside private intervals."""
    with storage.read(context, remaining_seconds=1.0) as tx:
        if group["signature"] != group_signature(tx, group_members(tx, group)):
            raise ContractError("VERSION_CONFLICT", "summary_sources")
        for previous in sources:
            source = tx.source(previous.ref, previous.revision)
            tx.claims.require_live_source(previous.ref, previous.revision)
            if source is None or source.suppressed or source.capture_gaps:
                raise ContractError("SOURCE_MISSING", "summary_source")
    return private.outbound_blocked(storage, clock, context, sources=sources)


def process_summary(storage, clock, context, item, *, model, started, budget):
    """Generate one page outside SQLite, then commit only under unchanged sources and work lease."""
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    if not callable(getattr(model, "judge", None)):
        return finish("retry", "model_unavailable")
    try:
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            group = _job(tx, item)
            if group is None:
                return finish("obsolete", "SOURCE_MISSING")
            sources, coverage = _read_page(tx, group)
            originals = {(s.ref, s.revision): tx.source(s.ref, s.revision).event["content"] for s in sources}
            episode = tx.episodes.get(group["subject_ref"]) if group["kind"] == "task" else None
            page_refs = {f"{s.ref}@{s.revision}" for s in sources}
            resume = episode.resume if episode is not None else None
            if not set(resume_evidence_refs(resume)) <= page_refs:
                resume = None
            task = dict(ref=episode.ref, resume=resume) if episode is not None else None
        guard = partial(_outbound_blocked, storage, clock, context, sources, group)
        if guard():
            return hold_for_private_mode(storage, clock, context, item, started=started, budget=budget)
        with private.using_outbound_check(guard):
            private.require_outbound()
            raw = model.judge(
                summary_messages(sources, episode_ref=task),
                remaining_seconds=budget_left(started, clock, budget),
            )
        value = json.loads(raw) if isinstance(raw, str) else raw
        sentences, spans = grounded_sentences(value, sources, originals=originals)
        return _publish_summary(
            storage,
            clock,
            context,
            item,
            (group, sources, coverage, sentences, spans, page_refs),
            started=started,
            budget=budget,
        )
    except (ContractError, ValueError, TypeError) as exc:
        with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
                return stale_result(tx, item)
            current = _job(tx, item)
            if current is None:
                return work_result(tx.work.mark_obsolete(*item.lease, now=clock.utc_now()))
            tx._check().execute(
                "UPDATE summary_groups SET state='partial',failure=? WHERE group_key=? AND generation=? "
                "AND state='pending'",
                (getattr(exc, "field", None) or "summary_invalid", current["group_key"], current["generation"]),
            )
        return finish("retry", getattr(exc, "code", None) or "DERIVATION_INVALID")
    except (OSError, RuntimeError, TimeoutError) as exc:
        return finish(*model_failure(exc))


def _publish_summary(storage, clock, context, item, prepared, *, started, budget):
    """Publish a grounded page and its next job in one fenced transaction."""
    group, sources, coverage, sentences, spans, page_refs = prepared
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return stale_result(tx, item)
        current = _job(tx, item)
        if current is None or current["signature"] != group_signature(tx, group_members(tx, current)):
            return work_result(tx.work.mark_obsolete(*item.lease, now=clock.utc_now()))
        head = tx.claims.version(item.subject_ref, item.subject_revision)
        roles = {(s.ref, s.revision): s.event["role"] for s in sources}
        text = "\n".join(
            f"{roles[(span['source_ref'], span['source_revision'])]}: {sentence}"
            for sentence, span in zip(sentences, spans, strict=True)
        )
        payload = dict(head.payload, value_text=text, evidence_spans=spans)
        validate_payload(
            "consolidation_result",
            dict(
                protocol_version="1.1",
                source_refs=list(page_refs),
                claim_proposals=[payload],
                resume_proposals=[],
                reference_proposals=[],
            ),
        )
        saved = tx.claims.append(
            group["scope_id"],
            payload,
            Qualification("active", "derived_summary", "grounded_" + group["kind"] + "_summary"),
            previous=head,
            recorded_at=clock.utc_now(),
        )
        for source in sources:
            lineage.link(
                tx._check(),
                "claim",
                saved.ref,
                saved.revision,
                source.ref,
                source.revision,
                relation="derived_from",
            )
        covered = group["covered_units"] + sum(end - start for _r, _v, start, end, _t in coverage)
        next_offset = group["next_offset"] + sum(end == total for _r, _v, _s, end, total in coverage)
        tx._check().execute(
            "UPDATE summary_parts SET claim_revision=?,coverage_json=? WHERE group_key=? AND "
            "generation=? AND ordinal=?",
            (saved.revision, json.dumps(coverage), group["group_key"], group["generation"], group["ordinal"]),
        )
        complete = covered == group["total_units"]
        tx._check().execute(
            "UPDATE summary_groups SET covered_units=?,next_offset=?,state=? WHERE group_key=?",
            (covered, next_offset, "complete" if complete else "pending", group["group_key"]),
        )
        if not complete:
            updated = dict(group, covered_units=covered, next_offset=next_offset)
            queue_summary_part(tx, updated, group["ordinal"] + 1, clock.utc_now())
        return work_result(tx.work.complete(*item.lease, now=clock.utc_now()))


def is_summary_job(tx, item):
    """Recognize only claim-backed summary jobs; other projection work keeps its owner."""
    return (
        tx._check()
        .execute(
            "SELECT 1 FROM summary_parts WHERE claim_ref=? AND claim_revision=?",
            (item.subject_ref, item.subject_revision),
        )
        .fetchone()
        is not None
    )
