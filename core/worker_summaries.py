"""Grounded conversation summarization through the existing leased, budgeted model port."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial

from ..contracts import ContractError
from . import private
from .consolidation_chunks import ChunkedSource, ConsolidationChunk, source_chunk
from .consolidation_summary import grounded_sentences
from .entry_control import entry_fence, paused_since, sources_paused
from .language_resources import prompt_text
from .resume_compaction import resume_evidence_refs
from .source_qualification import sentence_bounds
from .summary_parts import queue_summary_part, store_excerpt_claim, unprocessed_ranges, update_progress
from .summary_validity import group_members, group_signature, obsolete_ranges
from .worker_outcomes import (
    budget_left,
    defer_model_pass,
    finalize_work,
    hold_for_entry_state,
    hold_for_private_mode,
    model_failure,
    record_error_detail,
    stale_result,
    work_result,
)

SUMMARY_SOURCE_LIMITS = {"user": 1000, "assistant": 600, "tool": 300}


def summary_messages(sources, *, episode_ref=None):
    """Format bounded raw windows; task recovery is a hint, never authority for generated text."""
    records = []
    for source in sources:
        window = getattr(source, "consolidation_window", None)
        content = source.event["content"]
        limit = SUMMARY_SOURCE_LIMITS[source.event["role"]]
        total = window.total if window else len(content)
        if total > limit:
            available = max(0, limit - (window.start if window else 0))
            content = content[:available] + f"（已截断，全文 {total} 字符）"
        records.append(
            dict(
                ref=source.ref,
                revision=source.revision,
                content=content,
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


def _task_hint(episode, sources):
    """Include recovery context only when all its evidence is on this page."""
    if episode is None:
        return None
    refs = {f"{source.ref}@{source.revision}" for source in sources}
    resume = episode.resume if set(resume_evidence_refs(episode.resume)) <= refs else None
    return dict(ref=episode.ref, resume=resume)


def _page_task(sources, episode, omitted):
    """Fit the optional recovery hint once, returning the exact hint used to test the page."""
    task = _task_hint(episode, sources)
    if omitted and task is not None:
        task = dict(task, resume=None)
    try:
        summary_messages(sources, episode_ref=task)
        return task, omitted, True
    except ContractError as exc:
        if exc.code != "INPUT_INVALID" or exc.field != "consolidation_input_budget":
            raise
    if task is not None and task["resume"] is not None:
        source = sources[-1]
        empty = replace(source, event=dict(source.event, content=""))
        try:
            summary_messages((empty,), episode_ref=task)
        except ContractError as exc:
            if exc.code != "INPUT_INVALID" or exc.field != "consolidation_input_budget":
                raise
            task, omitted = dict(task, resume=None), True
            try:
                summary_messages(sources, episode_ref=task)
                return task, omitted, True
            except ContractError as retry_error:
                if retry_error.code != "INPUT_INVALID" or retry_error.field != "consolidation_input_budget":
                    raise
    return task, omitted, False


def _read_page(tx, group, episode):
    """Prepare only unprocessed ranges, hiding whole sentences of obsolete facts."""
    members = json.loads(group["manifest_json"])
    blocked = obsolete_ranges(tx, members)
    sources, task, omitted = [], None, False
    for index, start, end in unprocessed_ranges(tx, group):
        member = members[index]
        source = tx.source(member["ref"], member["revision"])
        if source is None or source.suppressed or source.capture_gaps:
            raise ContractError("SOURCE_MISSING", "summary_source")
        tx.claims.require_live_source(source.ref, source.revision)
        content = source.event["content"]
        mask = list(content)
        for a, b in blocked.get((source.ref, source.revision), ()):
            left, right = sentence_bounds(content, a, b)
            mask[left:right] = " " * (right - left)
        # Previously started generations can have gaps beyond the prefix. Mark
        # those ranges traversed without sending a later part of the message.
        limit = SUMMARY_SOURCE_LIMITS[source.event["role"]]
        if start >= limit:
            mask[start:end] = " " * (end - start)
        source = replace(source, event=dict(source.event, content="".join(mask)))
        if start or end != member["total"]:
            if sources:
                break
            values: dict = dict(source.__dict__, event=dict(source.event, content=source.event["content"][start:end]))
            source = ChunkedSource(**values, consolidation_window=ConsolidationChunk(start, end, member["total"]))
        candidate = (*sources, source)
        candidate_task, candidate_omitted, fits = _page_task(candidate, episode, omitted)
        if not fits:
            if sources:
                break
            source = tx.source(member["ref"], member["revision"])
            source = replace(source, event=dict(source.event, content="".join(mask)))
            try:
                source, _chunk = source_chunk(source, start, formatter=summary_messages, episode_ref=candidate_task)
            except ContractError as exc:
                if (
                    exc.code != "INPUT_INVALID"
                    or exc.field != "consolidation_metadata_budget"
                    or candidate_task is None
                    or candidate_task["resume"] is None
                ):
                    raise
                # Even one source character must fit for the page to progress.
                candidate_task, candidate_omitted = dict(candidate_task, resume=None), True
                source, _chunk = source_chunk(source, start, formatter=summary_messages, episode_ref=candidate_task)
            assert _chunk is not None
            if _chunk.end > end:
                source = replace(
                    source,
                    event=dict(source.event, content=source.event["content"][: end - start]),
                    consolidation_window=ConsolidationChunk(start, end, member["total"]),
                )
        sources.append(source)
        task, omitted = candidate_task, candidate_omitted
        if len(sources) == 16 or getattr(source, "consolidation_window", None) is not None:
            break
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
    return tuple(sources), coverage, task, omitted


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
        if sources_paused(tx, sources):
            raise ContractError("ACCESS_DENIED", "entry_state:paused")
        for previous in sources:
            source = tx.source(previous.ref, previous.revision)
            tx.claims.require_live_source(previous.ref, previous.revision)
            if source is None or source.suppressed or source.capture_gaps:
                raise ContractError("SOURCE_MISSING", "summary_source")
    return private.outbound_blocked(storage, clock, context, sources=sources)


def process_summary(storage, clock, context, item, *, model, started, budget):
    """Generate one page outside SQLite, then commit only under unchanged sources and work lease."""
    if not callable(getattr(model, "judge", None)):
        return finalize_work(
            storage, clock, context, item, "retry", "model_unavailable", started=started, budget=budget
        )
    deferred = defer_model_pass(storage, clock, context, item, model, started=started, budget=budget)
    if deferred is not None:
        return deferred
    return _process_summary_page(storage, clock, context, item, model=model, started=started, budget=budget)


def _process_summary_page(storage, clock, context, item, *, model, started, budget):
    """Read, generate and publish one page after the pass allowance has been checked."""
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    try:
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            group = _job(tx, item)
            if group is None:
                return finish("obsolete", "SOURCE_MISSING")
            episode = tx.episodes.get(group["subject_ref"]) if group["kind"] == "task" else None
            sources, coverage, task, hint_omitted = _read_page(tx, group, episode)
            originals = {(s.ref, s.revision): tx.source(s.ref, s.revision).event["content"] for s in sources}
            blocked = obsolete_ranges(tx, json.loads(group["manifest_json"]))
            entries = entry_fence(tx, sources)
        entry_paused = any(state == "paused" for state, _version in entries.values())
        guard = partial(_outbound_blocked, storage, clock, context, sources, group)
        if entry_paused or guard():
            hold = hold_for_entry_state if entry_paused else hold_for_private_mode
            return hold(storage, clock, context, item, started=started, budget=budget)
        submitted = tuple(source for source in sources if source.event["content"].strip())
        sentences, spans = [], []
        if submitted:
            with private.using_outbound_check(guard):
                private.require_outbound()
                raw = model.judge(
                    summary_messages(submitted, episode_ref=task),
                    remaining_seconds=budget_left(started, clock, budget),
                )
            value = json.loads(raw) if isinstance(raw, str) else raw
            sentences, spans = grounded_sentences(value, submitted, originals=originals, blocked=blocked)
            # Whole-sentence expansion must not turn text beyond the displayed
            # prefix into an excerpt that suppresses the unshown original.
            limits = {}
            for source in submitted:
                window = getattr(source, "consolidation_window", None)
                start = window.start if window else 0
                end = window.end if window else len(source.event["content"])
                limits[(source.ref, source.revision)] = (start, min(end, SUMMARY_SOURCE_LIMITS[source.event["role"]]))
            accepted = [
                (sentence, span)
                for sentence, span in zip(sentences, spans, strict=True)
                if limits[(span["source_ref"], span["source_revision"])][0] <= span["start"]
                and span["end"] <= limits[(span["source_ref"], span["source_revision"])][1]
            ]
            sentences = [sentence for sentence, _span in accepted]
            spans = [span for _sentence, span in accepted]
        return _publish_summary(
            storage,
            clock,
            context,
            item,
            (group, coverage, sentences, spans, hint_omitted and bool(submitted), entries),
            started=started,
            budget=budget,
        )
    except (ContractError, ValueError, TypeError) as exc:
        if getattr(exc, "field", None) == "entry_state:paused":
            return hold_for_entry_state(storage, clock, context, item, started=started, budget=budget)
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
    group, coverage, sentences, spans, hint_omitted, entries = prepared
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return stale_result(tx, item)
        if paused_since(tx, entries):
            return work_result(
                tx.work.defer_without_attempt(*item.lease, now=clock.utc_now(), error_code="entry_paused", seconds=60),
                error_code="entry_paused",
            )
        current = _job(tx, item)
        if current is None or current["signature"] != group_signature(tx, group_members(tx, current)):
            return work_result(tx.work.mark_obsolete(*item.lease, now=clock.utc_now()))
        head = tx.claims.version(item.subject_ref, item.subject_revision)
        if hint_omitted:
            record_error_detail(
                tx._check(),
                item.work_id,
                item.lease_token,
                now=clock.utc_now(),
                stage="summary",
                code="resume_hint_omitted",
                field="resume_hint_omitted_input_budget",
            )
        saved = store_excerpt_claim(tx, group, head, sentences, spans, coverage, now=clock.utc_now())
        tx._check().execute(
            "UPDATE summary_parts SET claim_revision=?,coverage_json=? WHERE group_key=? AND "
            "generation=? AND ordinal=?",
            (saved.revision, json.dumps(coverage), group["group_key"], group["generation"], group["ordinal"]),
        )
        updated = update_progress(tx, group)
        if updated["state"] != "complete":
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
