"""One proposal application transaction; model transport and scheduling stay outside."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..contracts import ContractError, validate_evidence_positions
from .candidate_lifecycle import CandidateSnapshot, candidate_model_subject
from .claim_storage import parse_source_ref
from .consolidation_chunks import ChunkedSource, ConsolidationChunk
from .consolidation_summary import apply_summary, mark_partial, stage_fragment, validate_fragment
from .episodes import source_origin, source_watermark
from .evidence_quote import resolve_evidence_quotes
from .failure_retry import validation_feedback as safe_feedback
from .language_resources import model_consolidation_schema, prompt_text
from .mutate import Mutation, MutationReceipt, apply_claim_frames, validate_claims
from .secret_patterns import cut_between_runs
from .worker_outcomes import DerivationFence, claim_versions_mark, claims_changed, derivation_changed

if TYPE_CHECKING:
    from .source_records import StoredSource


@dataclass(frozen=True)
class ConsolidationWorkFence:
    work_id: int
    lease_token: int
    lease_owner: str
    subject_ref: str
    subject_revision: int
    #: What the result was derived from, read with the batch before the model call.
    dependencies: DerivationFence
    allowed_source_refs: frozenset[str]
    skipped_source_refs: frozenset[str] = frozenset()
    pending_sources: tuple[tuple[str, int, int], ...] = ()
    chunk: ConsolidationChunk | None = None
    rejected_claims: tuple[str, ...] = ()


def _model_source_principal(source) -> dict[str, str] | None:
    principal = source.event.get("source_principal")
    if isinstance(principal, dict):
        result = {
            "kind": principal["kind"],
            "resolution": principal["resolution"],
        }
        if principal.get("display_name"):
            result["display_name"] = principal["display_name"]
        return result
    if source.event["origin"] == "human_direct":
        return {"kind": "human", "resolution": "unresolved"}
    return None


#: Conservative UTF-8 byte ceiling for one consolidation request. The inlined
#: result schema, instructions and source envelope share it with the raw text.
#: Callers with a different fixed overhead pass their own ceiling rather than
#: appending beyond this one.
CONSOLIDATION_INPUT_BUDGET = 16000


def consolidation_messages(
    sources, *, episode_ref=None, budget=CONSOLIDATION_INPUT_BUDGET, validation_feedback=None, candidate_kind=None
):
    """Build one bounded model proposal input from already authorized raw sources.

    This is an input formatter, not another application pipeline or a source of
    authority. Acceptance rechecks every source and version in its transaction.
    No answer, future event or precomputed claim is supplied to the model.
    """
    if not 1 <= len(sources) <= 32:
        raise ContractError("INPUT_INVALID", "consolidation_batch")
    refs = [f"{s.ref}@{s.revision}" for s in sources]
    if len(set(refs)) != len(refs):
        raise ContractError("INPUT_INVALID", "consolidation_duplicates")
    records = []
    for ref, source in zip(refs, sources):
        record = dict(
            ref=ref,
            source_ref=source.ref,
            source_revision=source.revision,
            origin=source.event["origin"],
            verified_original_origin=source_origin(source),
            content=source.event["content"],
            occurred_at=source.event["occurred_at"],
            capture_gaps=source.capture_gaps,
            artifact_ids=source.event.get("artifact_refs", []),
            display_snapshot=source.event.get("display_snapshot"),
            artifact_versions=[
                f"{item['artifact_ref']}@{item['revision']}"
                for item in source.event.get("display_snapshot", {}).get("items", [])
            ],
        )
        principal = _model_source_principal(source)
        if principal is not None:
            record["source_principal"] = principal
        records.append(record)
    chunked = False
    for record, source in zip(records, sources):
        window = getattr(source, "consolidation_window", None)
        if window is not None:
            chunked = True
            record["source_window"] = dict(
                start=window.start,
                end=window.end,
                total=window.total,
                unit="unicode_codepoints",
                coverage="fragment_only",
            )
            record["prior_fragment_goals"] = getattr(source, "consolidation_seed", ())
    schema = model_consolidation_schema(candidate_kind=candidate_kind)
    system = prompt_text("importance") + prompt_text("consolidation").format(
        schema=json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    if chunked:
        system += prompt_text("fragment")
    if validation_feedback is not None:
        feedback = safe_feedback(validation_feedback.get("code"), validation_feedback.get("field"))
        system += prompt_text("repair").format(feedback=json.dumps(feedback, sort_keys=True, separators=(",", ":")))
    watermark = source_watermark(refs)
    # Sources lead the input so requests with the same schema, fragment and
    # repair mode can share a provider cache prefix before changing metadata.
    body = dict(
        sources=records,
        episode_ref=episode_ref,
        source_refs=refs,
        source_watermark=watermark,
        resume_envelope=dict(source_refs=refs, source_watermark=watermark),
        empty_result={name: [] for name, field in schema["properties"].items() if field.get("type") == "array"},
    )
    user = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    # UTF-8 bytes conservatively bound even tokenizers with byte fallback.
    if len((system + user).encode("utf-8")) > budget:
        raise ContractError("INPUT_INVALID", "consolidation_input_budget")
    return [dict(role="system", content=system), dict(role="user", content=user)]


#: Candidate re-evaluation uses a larger content budget than consolidation so the shared prompt and candidate block
#: leave room for evidence.  The 64,000-byte ceiling counts all message contents.  The transport needs a separate byte
#: allowance for JSON escaping and the request envelope.
CANDIDATE_EVALUATION_INPUT_BUDGET = 64000


#: A source longer than this reaches a candidate evaluation as a window around
#: the candidate's first verified saved quote, falling back to its value or
#: subject when no saved quote matches this source version.
#: Tool output dominated the evidence: replayed over one store's evaluations, the
#: calls still made after the verdict limit carried 28 M characters, and windows
#: of this size keep 44% of them.  Qualification reads the complete stored source
#: around each quote, so a window changes what the model reads, never what a
#: quote has to satisfy.
EVIDENCE_WINDOW_THRESHOLD = 3000
EVIDENCE_WINDOW_RADIUS = 1500


def _needle_pattern(text: object) -> re.Pattern[str] | None:
    """Match ``text`` whatever spacing or punctuation sits between its letters and digits."""
    characters = [character for character in str(text or "") if character.isalnum()]
    if not characters:
        return None
    return re.compile(r"[\W_]*".join(re.escape(character) for character in characters), re.IGNORECASE)


def evidence_window(source: StoredSource, needles, evidence_spans=()) -> StoredSource:
    """Prefer the first exact saved quote for this source version, then a needle.

    Several distant quotes cannot all fit one bounded contiguous window. Use
    the first valid span in payload order, retaining context on both sides;
    never concatenate fragments or treat a saved span as admission authority.
    """
    content = str(source.event.get("content") or "")
    total = len(content)
    if total <= EVIDENCE_WINDOW_THRESHOLD or getattr(source, "consolidation_window", None) is not None:
        return source
    start, end = 0, EVIDENCE_WINDOW_THRESHOLD
    anchor = None
    for span in evidence_spans:
        if not isinstance(span, dict) or (span.get("source_ref"), span.get("source_revision")) != (
            source.ref,
            source.revision,
        ):
            continue
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote:
            continue
        if "start" in span or "end" in span:
            try:
                validate_evidence_positions({"evidence_spans": [span]}, {(source.ref, source.revision): content})
            except ContractError:
                continue
            anchor = (span["start"], span["end"])
            break
        position = content.find(quote)
        if position >= 0:
            anchor = (position, position + len(quote))
            break
    if anchor is None:
        for needle in needles:
            pattern = _needle_pattern(needle)
            match = pattern.search(content) if pattern is not None else None
            if match is not None:
                anchor = (match.start(), match.end())
                break
    if anchor is not None:
        start = max(0, anchor[0] - EVIDENCE_WINDOW_RADIUS)
        end = min(total, anchor[1] + EVIDENCE_WINDOW_RADIUS)
    # Each edge moves inward, never past what the window is built around, to where the request guard reads the window
    # as the source reads: cut inside a value or a key's name, the evaluation would be refused every time it is asked.
    keep = anchor or (start, start)
    start = cut_between_runs(content, start, toward=keep[0])
    end = cut_between_runs(content, end, toward=keep[1])
    return ChunkedSource(
        **dict(source.__dict__, event=dict(source.event, content=content[start:end])),
        consolidation_window=ConsolidationChunk(start, end, total),
        consolidation_seed=(),
    )


def candidate_evidence_sources(candidate, sources):
    """Share the exact authorized windows between model formatting and quote alignment."""
    needles = (candidate.payload.get("value_text"), candidate.payload.get("subject"))
    spans = candidate.payload.get("evidence_spans") or ()
    return tuple(evidence_window(source, needles, spans) for source in sources)


def candidate_evaluation_messages(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
    *,
    budget: int = CANDIDATE_EVALUATION_INPUT_BUDGET,
    validation_feedback: dict[str, str] | None = None,
) -> list[dict]:
    """Build a bounded candidate-specific request for the shared model port."""
    sources = candidate_evidence_sources(candidate, sources)
    messages = consolidation_messages(
        sources,
        episode_ref=None,
        budget=budget,
        validation_feedback=validation_feedback,
        candidate_kind=candidate.payload["kind"],
    )
    candidate_json = json.dumps(
        {
            "candidate_ref": candidate.ref,
            "candidate_revision": candidate.revision,
            "kind": candidate.payload.get("kind"),
            "subject": candidate_model_subject(candidate, sources),
            "predicate": candidate.payload.get("predicate"),
            "value_text": candidate.payload.get("value_text"),
            "conditions": candidate.payload.get("conditions", []),
            "rule_version": candidate.rule_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(candidate_json.encode("utf-8")) > 32768:
        raise ContractError("INPUT_INVALID", "candidate_input_budget")
    # One system message, and only at the start: chat templates such as Qwen's
    # reject a request whose second message is also a system message.
    messages[0] = {
        "role": "system",
        "content": prompt_text("candidate") + "\n\n" + messages[0]["content"],
    }
    messages.append({"role": "user", "content": "candidate=" + candidate_json})
    # The consolidation formatter bounded only its own two messages. The system
    # preamble and candidate block are added after that check, so
    # without this the request could reach roughly 49 KB while still claiming to
    # be bounded. Raise the same field the consolidation path raises: callers
    # and the oversize recovery already recognise it.
    if sum(len(message["content"].encode("utf-8")) for message in messages) > budget:
        raise ContractError("INPUT_INVALID", "consolidation_input_budget")
    return messages


def _fence_consolidation(tx, value, fence: ConsolidationWorkFence, *, now: str) -> list[str]:
    """Check a worker's result against its lease and, for one page of a long source, against that page; return what
    the page proposed that it does not ground, which is dropped."""
    if not tx.work._verify_lease(fence.work_id, fence.lease_token, fence.lease_owner, now=now):
        raise ContractError("ACCESS_DENIED", "lease_stale")
    if tx.source(fence.subject_ref, fence.subject_revision) is None:
        raise ContractError("SOURCE_MISSING")
    # The leased subject itself must still be the live source revision.  A
    # newer capture or deletion between model return and this transaction
    # invalidates the whole result, even when its proposal cites other roots.
    tx.claims.require_live_source(fence.subject_ref, fence.subject_revision)
    covered = frozenset(value["source_refs"]) | fence.skipped_source_refs
    if f"{fence.subject_ref}@{fence.subject_revision}" not in covered:
        raise ContractError("DERIVATION_INVALID", "consolidation_subject_uncovered")
    for ref in fence.skipped_source_refs:
        tx.claims.require_live_source(*parse_source_ref(ref))
    for ref in value["source_refs"]:
        if ref not in fence.allowed_source_refs:
            raise ContractError("DERIVATION_INVALID", "source_refs")
        tx.claims.require_live_source(*parse_source_ref(ref))
    # Captures and unrelated writes move memory_epoch without touching anything
    # this result was derived from.  Only a changed dependency rejects it, as
    # the retryable ``VERSION_CONFLICT`` (``memory_epoch``).  A non-final
    # page only stages its summaries; the final page applies all of them.
    applies_summaries = fence.chunk is None or fence.chunk.final
    if (
        derivation_changed(
            tx,
            fence.dependencies,
            episode=applies_summaries and (fence.chunk is not None or bool(value["resume_proposals"])),
            references=applies_summaries and (fence.chunk is not None or bool(value["reference_proposals"])),
        )
        is not None
    ):
        raise ContractError("VERSION_CONFLICT", "memory_epoch")
    # Resolve only against the leased page. A literal match on an older page
    # must not mask this page's decoded match; final claim qualification still
    # sees the complete source, including qualifiers outside this window.
    if fence.chunk is not None:
        chunk = fence.chunk
        source = tx.source(fence.subject_ref, fence.subject_revision)
        content = source.event["content"]
        if (
            not 0 <= chunk.start < chunk.end <= len(content)
            or chunk.total != len(content)
            or tx.work.consolidation_offset(fence.work_id, fence.lease_token, fence.lease_owner, now=now) != chunk.start
        ):
            raise ContractError("DERIVATION_INVALID", "consolidation_offset")

        page = content[chunk.start : chunk.end]
        resolve_evidence_quotes(value, {(source.ref, source.revision): page})
        dropped = validate_fragment(tx, value, fence, content)
        for proposal in value["claim_proposals"]:
            for span in proposal["evidence_spans"]:
                if (
                    (span["source_ref"], span["source_revision"]) != (source.ref, source.revision)
                    or span["quote"] not in page
                    or ("start" in span and not chunk.start <= span["start"] < span["end"] <= chunk.end)
                ):
                    raise ContractError("DERIVATION_INVALID", "fragment_evidence")
        # Named where a dropped whole-message summary is (``apply_summary``), so an operator sees what a page lost.
        tx._check(write=True).executemany(
            """INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
               VALUES (?,?,'summary','DERIVATION_INVALID',?,?)""",
            [(fence.work_id, fence.lease_token, field, now) for field in dropped],
        )
        return dropped
    return []


def _drop_claims(tx, fence: ConsolidationWorkFence, fields: list[str] | tuple[str, ...], now: str) -> str:
    """Record each rejected claim field without losing accepted siblings.

    A page carries the reason to the whole message's outcome; a whole-message
    result is partial whenever it leaves out an ungrounded claim.
    """
    conn = tx._check(write=True)
    conn.executemany(
        """INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
           VALUES (?,?,'claim','DERIVATION_INVALID',?,?)""",
        [(fence.work_id, fence.lease_token, field, now) for field in fields],
    )
    if fence.chunk is None:
        mark_partial(conn, fence.work_id, fields[0], now)
    return fields[0]


def accept_consolidation(
    storage, clock, context, value, *, scope_id, remaining_seconds=1.0, work_fence: ConsolidationWorkFence | None = None
):
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        now = clock.utc_now()
        mark = 0
        dropped: list[str] = []
        if work_fence is not None:
            dropped = _fence_consolidation(tx, value, work_fence, now=now)
            if work_fence.rejected_claims:
                dropped.append(_drop_claims(tx, work_fence, work_fence.rejected_claims, now))
            mark = claim_versions_mark(tx)
        items, claim_refs = [], set()
        ungrounded: list[int] | None = [] if work_fence is not None else None
        try:
            result = validate_claims(tx, value, scope_id, ungrounded_values=ungrounded)
            if ungrounded and work_fence is not None:
                dropped.append(_drop_claims(tx, work_fence, ["evidence_value"] * len(ungrounded), now))
            for proposal in result["claim_proposals"]:
                applied = apply_claim_frames(tx, proposal, scope_id, now)
                claim_refs.update(item.ref for item in applied)
                items.extend(applied)
        except ContractError as exc:
            # A rejection caused by a claim written during the call is the
            # retryable ``VERSION_CONFLICT`` (``memory_epoch``) as well.
            if work_fence is not None and claims_changed(tx, work_fence.dependencies, until=mark):
                raise ContractError("VERSION_CONFLICT", "memory_epoch") from exc
            raise
        # A correction, confirmation or revision of a slot this result just
        # wrote to, made during the call, would otherwise have the stale result
        # silently ordered against the newer head instead of re-derived.
        if work_fence is not None and claims_changed(tx, work_fence.dependencies, until=mark, claim_refs=claim_refs):
            raise ContractError("VERSION_CONFLICT", "memory_epoch")
        if work_fence is not None and work_fence.chunk is not None:
            result = stage_fragment(tx, result, work_fence, now, dropped=dropped)

        for proposal in result["reference_proposals"]:
            item = apply_summary(tx, work_fence, "reference", proposal, scope_id, now)
            if item is not None:
                items.append(Mutation(item.ref, item.revision, "applied", item.payload["resolution"]))
        for proposal in result["resume_proposals"]:
            item = apply_summary(tx, work_fence, "resume", proposal, scope_id, now)
            if item is not None:
                items.append(Mutation(item.ref, item.revision, "applied", item.state))
        if work_fence is not None:
            if work_fence.chunk is not None:
                tx.work.advance_consolidation(
                    work_fence.work_id,
                    work_fence.lease_token,
                    work_fence.lease_owner,
                    now=now,
                    expected_offset=work_fence.chunk.start,
                    next_offset=work_fence.chunk.end,
                    final=work_fence.chunk.final,
                )
            else:
                tx.work.complete_consolidation(
                    work_fence.work_id,
                    work_fence.lease_token,
                    work_fence.lease_owner,
                    now=now,
                    covered_source_refs=frozenset(result["source_refs"]) | work_fence.skipped_source_refs,
                    pending_sources=work_fence.pending_sources,
                )
        epoch = tx.status().memory_epoch
    return MutationReceipt(tuple(items), epoch)
