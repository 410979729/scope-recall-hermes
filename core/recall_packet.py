"""Typed RecallPacket compiler for the single P08 retrieval pipeline.

The compiler consumes exactly one completed :class:`RetrievalResult` and never
searches, hydrates independently, or re-determines truth.  Release-time
rechecks go through the existing read boundary within the original deadline.

``compile`` reads top-down: release (verify, then fence) -> order -> admit ->
fit -> finalize -> optional excerpts. Full evidence lives in :class:`_Draft`;
:func:`assemble_packet` owns status, answerability and coverage. Excerpts use
only the finalized packet's spare room and do not revise its verdict.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, Protocol, cast

from ..contracts import ContractError, RecallItem, RecallPacket
from . import recall_log_view
from .background_context import is_background, mark_background
from .entity_queries import fence_entity_query
from .recall_budget import canonical_render_json, estimate_tokens, event_admission_order
from .recall_diagnostics import RECALL_DIAGNOSTIC_PREFIX, RecallDiagnostics, RecallTimings
from .recall_items import PacketKind as PacketKind
from .recall_items import packet_item as packet_item
from .recall_items import packet_kind as packet_kind
from .recall_needs import asks_to_resume, unmet_needs
from .recall_overflow import append_overflow
from .recall_restatement import (
    has_conversation_summaries,
    host_memory_duplicate,
    prioritize_summary_originals,
    recheck_restatement,
    redundant_summary_keys,
)
from .recall_trace import note_candidate
from .resume_compaction import (
    compact_episode_variants,
    next_step_provenance_supported,
    resume_fields,
)
from .retrieval import (
    CandidateRef,
    RetrievalResult,
    RetrievedObject,
    SearchContext,
    SearchLimits,
    effective_limits,
)
from .retrieval_storage import RetrievalStorage

_AUTHORITY_GAP_PREFIXES = ("sqlite_unavailable",)
#: Gaps after which the evidence delivered may lack part of the answer: authority failed, or a read did not finish.
#: Query evidence dropped as stale or by the budget may have held part of it too (``_Draft.answer_dropped``).  The
#: other gaps leave the answer as whole as the evidence delivered: candidates past the packet's slots and background
#: past its own (``coverage_truncated``), the relation bound (``relation_bound_reached``), duplicate bodies folded
#: into one (``duplicates_collapsed``), an absent or failing vector channel beside the lexical ones (``vector_*``),
#: background dropped by the budget, and a day the question names that could not be read (``scope_unreadable``),
#: which recalls the question as one that names no day.
_INCOMPLETE_GAP_PREFIXES = ("deadline_exceeded", "sqlite_unavailable", "sqlite_candidate_error")
#: Markers published as their bare prefix, so diagnostics stay useful without
#: refs consuming the packet budget.  Everything else passes through intact.
_BARE_MARKER_PREFIXES = frozenset(
    {
        "budget_token_cap",
        "budget_packet_cap",
        "budget_oversized",
        "expandable",
        "stale_candidate",
        "revision_changed",
        "resume_state_unverified",
    }
)
_MAX_PACKET_ITEM_CHARS = 12000
_MAX_APPLICABILITY_CHARS = 2048
_MAX_ORIGIN_CHARS = 80
_MAX_EVIDENCE_REFS = 32
_MAX_RENDER_PREPARED = 64
_RENDER_CONTEXT_SCHEMA = "scope-recall.recall_context/1.1"
# Same width as ``RecallDiagnostics.record`` refs (the prefix + 16 hex),
# so a packet measured before recording has the bytes of the packet returned.
_DIAGNOSTIC_REF_PLACEHOLDER = RECALL_DIAGNOSTIC_PREFIX + ("0" * 16)

Pair = tuple[CandidateRef, RetrievedObject]


class RecallClock(Protocol):
    def monotonic(self) -> float: ...


def isolate_recall_packet(packet: RecallPacket) -> RecallPacket:
    """Return a caller-owned copy that cannot mutate cached compiler state."""
    # Evidence refs and other item fields are nested mutable lists; a shallow
    # item copy would still let a caller mutate the cached packet indirectly.
    return copy.deepcopy(packet)


# -- public vocabulary --------------------------------------------------------


def public_marker(value: object) -> str:
    if type(value) is not str:
        return "diagnostic_invalid"
    prefix = value.split(":", 1)[0]
    return prefix if prefix in _BARE_MARKER_PREFIXES else value[:160]


def public_gaps(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(public_marker(value) for value in values if value))[:16]


def public_needs(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(public_marker(value) for value in values if value))[:8]


def envelope_tokens(packet: RecallPacket, *, include_diagnostic_ref: bool = True) -> int:
    """The one estimate used for admission and the final canonical JSON."""
    measured = packet if include_diagnostic_ref else dict(packet, diagnostic_ref=None)
    return estimate_tokens(canonical_render_json(measured))


def fits_packet_schema(obj: RetrievedObject) -> bool:
    return (
        len(obj.content) <= _MAX_PACKET_ITEM_CHARS
        and len(obj.applicability) <= _MAX_APPLICABILITY_CHARS
        and len(obj.origin) <= _MAX_ORIGIN_CHARS
        and len(obj.evidence_refs) <= _MAX_EVIDENCE_REFS
        and all(type(ref) is str and len(ref) <= 240 for ref in obj.evidence_refs)
    )


# -- ordering -----------------------------------------------------------------


def prioritize_resume_evidence(context: SearchContext, verified: list[Pair]) -> list[Pair]:
    """Keep a complete current episode ahead of incidental relation hits."""
    if not asks_to_resume(context.query):
        return verified
    return sorted(
        verified,
        key=lambda pair: (
            1 if is_background(pair[1]) else 0,
            0 if pair[1].kind == "episode" else 1,
            0 if pair[0].source == "exact_ref" else 1,
            -pair[0].fusion_score,
            pair[0].kind,
            pair[0].ref,
            pair[0].revision,
        ),
    )


def is_active_direct_claim(obj: RetrievedObject) -> bool:
    """A current claim backed by direct human evidence."""
    return (
        obj.kind == "claim"
        and not is_background(obj)
        and obj.temporal_status == "current"
        and obj.origin == "human_direct"
        and obj.basis == "direct_report"
        and dict(obj.metadata).get("state") == "active"
        and bool(obj.evidence_refs)
    )


def prioritize_current_claims(context: SearchContext, verified: list[Pair]) -> list[Pair]:
    """Keep a normalized current fact ahead of its raw event under budget.

    Deliberately narrow: only ordinary current recall, only after every
    candidate passed the SQLite release fence, and only when the claim's exact
    evidence event is competing for the same packet.  Explicit refs and
    historical/method queries keep their original relevance order.
    """
    if context.mode not in {"auto", "current"} or asks_to_resume(context.query):
        return verified
    if any(candidate.source == "exact_ref" for candidate, _obj in verified):
        return verified

    prioritized: list[Pair] = []
    run: list[Pair] = []

    def flush_run() -> None:
        event_refs = {f"{obj.ref}@{obj.revision}" for _candidate, obj in run if obj.kind == "event"}
        promoted = [
            pair for pair in run if is_active_direct_claim(pair[1]) and event_refs.intersection(pair[1].evidence_refs)
        ]
        promoted_keys = {pair[0].key for pair in promoted}
        prioritized.extend(promoted)
        prioritized.extend(pair for pair in run if pair[0].key not in promoted_keys)
        run.clear()

    for pair in verified:
        if pair[1].kind in {"event", "claim"}:
            run.append(pair)
            continue
        flush_run()
        prioritized.append(pair)
    flush_run()
    return prioritized


# -- the packet ---------------------------------------------------------------


@dataclass
class _Draft:
    """Everything the public packet is computed from, before it is rendered."""

    context: SearchContext
    result: RetrievalResult
    memory_epoch: int | None
    items: list[RecallItem] = field(default_factory=list)
    objects: list[RetrievedObject] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    unmet_needs: list[str] = field(default_factory=list)
    stale_drops: int = 0
    budget_drops: int = 0
    #: Whether query evidence, not background, was dropped as stale or by the budget while the rest was delivered.  A
    #: withdrawal that drops the whole release leaves no evidence to answer with at all.
    answer_dropped: bool = False

    def note_epoch(self, epoch: int, gap: str, *, retracted: Callable[[int], bool] | None = None) -> bool:
        """Record an epoch move; ``True`` means the verified set is no longer released.

        Every capture moves the epoch.  With several entries writing to one store a recall rarely
        finishes without a move, and withdrawing at each would empty the packet: an agent asking
        about something the owner has just told another agent, while the worker is writing it down,
        would get nothing.  Only a deletion or suppression in the recall's own scopes withdraws
        what was verified (``retracted``, given the epoch of the last read); any other change is
        caught item by item by the fresh reads.  Without ``retracted`` every move withdraws.
        """
        if self.memory_epoch is None or epoch == self.memory_epoch:
            return False
        since, self.memory_epoch = self.memory_epoch, epoch
        if retracted is not None and not retracted(since):
            return False
        self.gaps.append(gap)
        return True

    def discard(self, verified: list[Pair], gap: str | None = None) -> list[Pair]:
        if gap is not None:
            self.gaps.append(gap)
        self.stale_drops += len(verified)
        return []

    def expire(self) -> None:
        """Withhold the entire packet when compilation finishes after its cutoff."""
        self.gaps.append("deadline_exceeded_compile")
        self.stale_drops += len(self.items)
        self.items.clear()
        self.objects.clear()

    def deliver(self, item: RecallItem, obj: RetrievedObject, needs: list[str]) -> None:
        self.items.append(item)
        self.objects.append(obj)
        self.unmet_needs.extend(needs)


def _packet_status(
    items, result, gaps, needs, dropped: bool, *, failed: bool
) -> Literal["ok", "no_match", "partial", "unavailable"]:
    """Unavailable when authority or a read failed and nothing came back; no match when nothing came back (partial
    if results were dropped); partial with gaps, unmet needs or drops; else ok."""
    if failed and not items:
        return "unavailable"
    if not items:
        return "partial" if result.items and dropped else "no_match"
    if gaps or needs or dropped:
        return "partial"
    return "ok"


def _answerability(
    status: str, answer_objects, needs, incomplete: bool, verdict
) -> Literal["supported", "partial", "ambiguous", "unknown"]:
    """Unknown without answer evidence; partial with unmet needs, or when the evidence delivered may lack part of the
    answer (``incomplete``: ``_INCOMPLETE_GAP_PREFIXES``, ``_Draft.answer_dropped``); else the retrieval's verdict on
    its evidence (``RetrievalPipeline._result``)."""
    if status in {"no_match", "unavailable"} or not answer_objects:
        return "unknown"
    if needs or incomplete:
        return "partial"
    return verdict


def _packet_epoch(status: str, items, memory_epoch: int | None) -> int | None:
    """The memory epoch a packet answers at: none without items, 0 when items came from a read that had none."""
    if status in {"no_match", "unavailable"}:
        return None
    if items:
        return memory_epoch if memory_epoch is not None else 0
    return memory_epoch


def assemble_packet(draft: _Draft, *, items=None, objects=None, extra_needs=()) -> tuple[RecallPacket, tuple[str, ...]]:
    """Build the exact public packet shape without side effects.

    ``diagnostic_ref`` carries a fixed-width placeholder of the real ref so the
    budget can be measured on the packet that will be returned, before the
    diagnostics record exists.  Returns the packet and the raw (pre-collapse)
    gap tuple the diagnostics record needs.
    """
    items = draft.items if items is None else items
    objects = draft.objects if objects is None else objects
    result = draft.result
    raw_gaps = tuple(dict.fromkeys(gap for gap in draft.gaps if gap))
    gaps = public_gaps(raw_gaps)
    authority_failed = any(gap.startswith(prefix) for gap in raw_gaps for prefix in _AUTHORITY_GAP_PREFIXES)
    # An unfinished read, without treating ordinary vector gaps as faults.
    read_incomplete = any(gap.startswith(prefix) for gap in raw_gaps for prefix in _INCOMPLETE_GAP_PREFIXES)
    answer_objects = tuple(obj for obj in objects if not is_background(obj))
    raw_needs = [*draft.unmet_needs, *extra_needs]
    if items and not answer_objects:
        raw_needs.append("query_evidence_missing")
    raw_needs = [*raw_needs, *unmet_needs(draft.context.query, answer_objects), *result.unmet_needs]
    needs = list(public_needs(dict.fromkeys(raw_needs)))
    dropped = bool(draft.stale_drops or draft.budget_drops)

    status = _packet_status(items, result, gaps, needs, dropped, failed=authority_failed or read_incomplete)
    incomplete = read_incomplete or draft.answer_dropped
    answerability = _answerability(status, answer_objects, needs, incomplete, result.answerability_hint)
    coverage = "partial" if gaps or dropped or needs else result.coverage
    packet_epoch = _packet_epoch(status, items, draft.memory_epoch)

    packet: RecallPacket = {
        "protocol_version": "1.1",
        "request_id": (result.request_id or draft.context.request_id)[:100],
        "status": status,
        "memory_epoch": packet_epoch,
        "items": list(items),
        "gaps": list(gaps),
        "diagnostic_ref": _DIAGNOSTIC_REF_PLACEHOLDER,
        "answerability": answerability,
        "coverage": coverage,
        "unmet_needs": needs,
    }
    return packet, raw_gaps


def bounded_packet(packet: RecallPacket, budget: int) -> RecallPacket:
    """Keep answer evidence and public budget diagnostics under the cap."""
    if envelope_tokens(packet) <= budget:
        return isolate_recall_packet(packet)
    if envelope_tokens(packet, include_diagnostic_ref=False) <= budget:
        return isolate_recall_packet(cast(RecallPacket, dict(packet, diagnostic_ref=None)))
    minimal = dict(
        packet,
        status="unavailable",
        memory_epoch=None,
        items=[],
        gaps=["budget_packet_cap"],
        diagnostic_ref=None,
        answerability="unknown",
        coverage="unknown",
        unmet_needs=[],
    )
    if estimate_tokens(canonical_render_json(minimal)) > budget:
        raise ContractError("INPUT_INVALID", "budget_tokens")
    return isolate_recall_packet(cast(RecallPacket, minimal))


def _sqlite_gap(exc: BaseException) -> str:
    return f"sqlite_unavailable:{exc.code if isinstance(exc, ContractError) else type(exc).__name__}"


@dataclass(frozen=True)
class RecallPacketCompiler:
    """Compile one retrieval snapshot into the public RecallPacket contract."""

    storage_reader: RetrievalStorage
    diagnostics: RecallDiagnostics | None = None
    clock: RecallClock | None = None

    @property
    def _clock(self) -> RecallClock:
        return self.clock if self.clock is not None else time

    def compile(self, context: SearchContext, result: RetrievalResult, storage) -> RecallPacket:
        if not isinstance(context, SearchContext) or not isinstance(result, RetrievalResult):
            raise ContractError("INPUT_INVALID", "compile_input")
        context = result.release_context(context)
        started = self._clock.monotonic()
        limits = effective_limits(context)
        draft = _Draft(
            context, result, result.memory_epoch, gaps=list(result.gaps), unmet_needs=list(result.unmet_needs)
        )
        if self._remaining_ms(context) == 0:
            draft.gaps.append("deadline_exceeded_compile")
            return self._finalize(draft, limits, started)
        timings = RecallTimings(self._clock.monotonic)
        with timings.measure("verify"):
            verified = self._verify(storage, draft, list(zip(result.candidates, result.items)))
        with timings.measure("fence"):
            verified = self._fence(storage, draft, verified)
        if context.automatic_injection and context.host_memory_entries:
            try:
                matches = [host_memory_duplicate(obj, context.host_memory_entries) for _, obj in verified]
            except Exception:  # noqa: BLE001 - optional host comparison cannot block recall
                matches = [False] * len(verified)
            for (candidate, obj), duplicate in zip(verified, matches, strict=True):
                if duplicate:
                    note_candidate(candidate, "host_memory_duplicate", obj=obj)
            verified = [pair for pair, duplicate in zip(verified, matches, strict=True) if not duplicate]
        draft.result = replace(result, diagnostics=(*result.diagnostics, *timings.snapshot()))
        draft, include_diagnostic_ref = self._select(draft, self._ordered(context, verified, limits), limits)
        if context.mode == "auto" and not any(not is_background(obj) for obj in draft.objects):
            # Background depends on evidence that actually ships. A grounded
            # resume task can stand alone because it answers the resume request.
            keep = [
                index
                for index, obj in enumerate(draft.objects)
                if obj.kind == "episode" and asks_to_resume(context.query)
            ]
            draft.items = [draft.items[index] for index in keep]
            draft.objects = [draft.objects[index] for index in keep]
        if draft.items and self._remaining_ms(context) == 0:
            draft.expire()
        packet = self._finalize(draft, limits, started, include_diagnostic_ref=include_diagnostic_ref)
        self._note_selection(verified, packet)
        extended = append_overflow(
            storage, replace(context, limits=limits), result, packet, self.storage_reader, self._clock
        )
        if extended is not packet:
            self._record_extension(packet, extended, started, context)
        return extended

    @staticmethod
    def _note_selection(verified, packet):
        """Explain only final fitted bodies; exploratory packings cannot omit derived content."""
        selected = {(item["ref"], item["revision"]): item for item in packet["items"]}
        displayed = [
            replace(obj, content=selected[(obj.ref, obj.revision)]["content"])
            for _candidate, obj in verified
            if (obj.ref, obj.revision) in selected
        ]
        summaries = [obj for _candidate, obj in verified if obj.kind == "claim"]
        covered = redundant_summary_keys([*displayed, *summaries])
        for candidate, obj in verified:
            if (obj.ref, obj.revision) not in selected:
                reason = "restatement" if obj.origin == "inductive_observation" else "summary_covered"
                note_candidate(candidate, reason if candidate.key in covered else "budget", obj=obj)

    def _record_extension(self, packet, extended, started, context):
        """Keep the diagnostic aligned with the bytes actually delivered."""
        record = self.diagnostics.get(packet.get("diagnostic_ref")) if self.diagnostics is not None else None
        if record is not None and self.diagnostics is not None:
            metrics = asdict(record)
            metrics.pop("ref")
            measured = canonical_render_json(extended)
            metrics.update(
                items_delivered=len(extended["items"]),
                rendered_bytes=len(measured.encode("utf-8")),
                estimated_tokens=estimate_tokens(measured),
                elapsed_ms=int((self._clock.monotonic() - started) * 1000),
                deadline_remaining_ms=self._remaining_ms(context),
            )
            extended["diagnostic_ref"] = self.diagnostics.record(**metrics)

    # -- release --------------------------------------------------------------

    def _remaining_ms(self, context: SearchContext) -> int:
        return max(0, int((context.deadline - self._clock.monotonic()) * 1000))

    def _read(self, storage, context: SearchContext):
        return storage.read(
            context.trusted_context, remaining_seconds=max(context.deadline - self._clock.monotonic(), 0.001)
        )

    def _recheck(
        self, tx, context: SearchContext, candidate: CandidateRef, obj: RetrievedObject, gaps: list[str]
    ) -> RetrievedObject | None:
        if self._remaining_ms(context) == 0:
            gaps.append("deadline_exceeded_release")
            return None
        fresh = self.storage_reader.hydrate(tx, candidate, context)
        if fresh is None:
            note_candidate(candidate, recall_log_view.rejection_reason(tx, candidate, context))
            gaps.append(f"stale_candidate:{candidate.ref}@{candidate.revision}")
            return None
        note_candidate(candidate, obj=fresh)
        if (fresh.ref, fresh.revision) != (obj.ref, obj.revision):
            gaps.append(f"revision_changed:{obj.ref}@{obj.revision}")
            return None
        fresh = recheck_restatement(tx, context, obj, fresh)
        return mark_background(fresh) if candidate.source == "background" else fresh

    def _retracted(self, tx, context: SearchContext) -> Callable[[int], bool] | None:
        """This read's view of whether anything in the recall's scopes was withdrawn since an epoch."""
        probe = getattr(self.storage_reader, "retracted_since", None)
        return None if probe is None else (lambda since: probe(tx, context, since))

    def _prefetch(self, tx, context: SearchContext, pairs: list[Pair]) -> None:
        """Load what rehydrating these candidates reads, together, as the retrieval loaded it
        (``RetrievalStorage.prefetch``): one by one, each claim's judgement and each source would cost statements of
        their own in every read."""
        prefetch = getattr(self.storage_reader, "prefetch", None)
        if prefetch is not None and self._remaining_ms(context) > 0:
            prefetch(
                tx,
                tuple(candidate for candidate, _obj in pairs),
                context,
            )

    def _verify(self, storage, draft: _Draft, ranked: list[Pair]) -> list[Pair]:
        """Rehydrate every candidate in one fresh read; a withdrawal in its scopes empties it."""
        context = draft.context
        verified: list[Pair] = []
        try:
            with self._read(storage, context) as tx:
                retracted = self._retracted(tx, context)
                draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed", retracted=retracted)
                self._prefetch(tx, context, ranked)
                for candidate, item in ranked:
                    if self._remaining_ms(context) == 0:
                        draft.gaps.append("deadline_exceeded_release")
                        break
                    fresh = self._recheck(tx, context, candidate, item, draft.gaps)
                    if fresh is None:
                        draft.stale_drops += 1
                        draft.answer_dropped |= not is_background(item)
                        continue
                    verified.append((candidate, fresh))
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release", retracted=retracted):
                    return draft.discard(verified)
        except Exception as exc:
            return draft.discard(verified, _sqlite_gap(exc))
        return verified

    def _fence(self, storage, draft: _Draft, verified: list[Pair]) -> list[Pair]:
        """One more fresh SQLite read is the release linearization point.

        It rehydrates every object that may be delivered and checks the epoch
        after those reads, all within the original deadline.
        """
        context = draft.context
        if not verified:
            return verified
        if draft.memory_epoch is None or self._remaining_ms(context) == 0:
            return draft.discard(verified, "deadline_exceeded_release_fence")
        try:
            with self._read(storage, context) as tx:
                retracted = self._retracted(tx, context)
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release_fence", retracted=retracted):
                    return draft.discard(verified)
                self._prefetch(tx, context, verified)
                verified = fence_entity_query(tx, context, verified, draft.gaps)
                for candidate, obj in verified:
                    if self._recheck(tx, context, candidate, obj, draft.gaps) is None:
                        return draft.discard(verified, "release_delete_fence")
                    if self._remaining_ms(context) == 0:
                        return draft.discard(verified, "deadline_exceeded_release_fence")
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release_fence", retracted=retracted):
                    return draft.discard(verified)
        except Exception as exc:
            return draft.discard(verified, _sqlite_gap(exc))
        if self._remaining_ms(context) == 0:
            return draft.discard(verified, "deadline_exceeded_release_fence")
        return verified

    # -- selection ------------------------------------------------------------

    @staticmethod
    def _ordered(context: SearchContext, verified: list[Pair], limits: SearchLimits) -> list[Pair]:
        verified = event_admission_order(verified, limits)
        verified = prioritize_resume_evidence(context, verified)
        verified = prioritize_current_claims(context, verified)
        # Ambient preferences/task state cannot displace answer evidence.
        verified.sort(key=lambda pair: pair[0].source == "background")
        return verified

    def _pack(self, base: _Draft, verified: list[Pair], limits: SearchLimits) -> tuple[_Draft, bool]:
        """Try an ordering with the existing schema, admission and envelope checks."""
        draft = replace(base, items=[], objects=[], gaps=list(base.gaps), unmet_needs=list(base.unmet_needs))
        self._admit(draft, verified, limits)
        return draft, self._fit(draft, limits)

    @staticmethod
    def _represents(previous: list[RetrievedObject], shown: list[RetrievedObject]) -> bool:
        """Every retained object still ships, or its summary is fully displayed by originals."""
        present = {(obj.kind, obj.ref, obj.revision) for obj in shown}
        present.update(redundant_summary_keys([*shown, *(obj for obj in previous if obj.kind == "claim")]))
        return all((obj.kind, obj.ref, obj.revision) in present for obj in previous)

    @staticmethod
    def _displayed(draft: _Draft) -> list[RetrievedObject]:
        """Bind comparisons to the actual item bodies, including compacted displays."""
        return [replace(obj, content=item["content"]) for item, obj in zip(draft.items, draft.objects, strict=True)]

    def _select(self, base: _Draft, verified: list[Pair], limits: SearchLimits) -> tuple[_Draft, bool]:
        """Prefer originals only when their complete fitted packet preserves the ordinary answer."""
        ordinary = self._pack(base, verified, limits)
        if has_conversation_summaries(verified) and self._remaining_ms(base.context) > 0:
            trial = self._deduplicate(
                base, verified, limits, self._pack(base, prioritize_summary_originals(verified), limits)
            )
            if self._represents(ordinary[0].objects, self._displayed(trial[0])):
                return trial
        return self._deduplicate(base, verified, limits, ordinary)

    def _deduplicate(
        self, base: _Draft, verified: list[Pair], limits: SearchLimits, packed: tuple[_Draft, bool]
    ) -> tuple[_Draft, bool]:
        """Omit covered summaries and refill without losing fitted evidence.

        Rejected summaries whose entire text is already displayed also consume
        no budget or rejection diagnostics. Each pass excludes a new summary,
        so the bounded candidate pool bounds the number of admission passes.
        """
        draft, diagnostic = packed
        omitted = set()
        summaries = [obj for _candidate, obj in verified if obj.kind == "claim"]
        while True:
            duplicates = redundant_summary_keys([*self._displayed(draft), *summaries]) - omitted
            if not duplicates:
                return draft, diagnostic
            omitted.update(duplicates)
            keep = [
                index for index, obj in enumerate(draft.objects) if (obj.kind, obj.ref, obj.revision) not in duplicates
            ]
            draft.items = [draft.items[index] for index in keep]
            draft.objects = [draft.objects[index] for index in keep]
            if self._remaining_ms(base.context) == 0:
                return draft, diagnostic
            retained = {(obj.kind, obj.ref, obj.revision) for obj in draft.objects}
            choices = [pair for pair in verified if pair[0].key not in omitted]
            choices.sort(key=lambda pair: (pair[1].kind, pair[1].ref, pair[1].revision) not in retained)
            refill, refill_diagnostic = self._pack(base, choices, limits)
            previous = [*draft.objects, *(obj for obj in summaries if (obj.kind, obj.ref, obj.revision) in omitted)]
            if not self._represents(previous, self._displayed(refill)):
                return draft, diagnostic
            draft, diagnostic = refill, refill_diagnostic

    def _fits(
        self,
        draft: _Draft,
        limits: SearchLimits,
        *,
        items=None,
        objects=None,
        extra_needs=(),
        diagnostic_ref: bool = True,
    ) -> bool:
        """Measure the exact packet that would be returned with these items."""
        packet, _raw_gaps = assemble_packet(draft, items=items, objects=objects, extra_needs=extra_needs)
        return envelope_tokens(packet, include_diagnostic_ref=diagnostic_ref) <= limits.budget_tokens

    def _admit(self, draft: _Draft, verified: list[Pair], limits: SearchLimits) -> None:
        """Admit candidates in order, each measured in the envelope it would ship in.

        Per-candidate rejection markers are deferred until selection ends: a
        rejected candidate must not charge its markers to the admission of
        every lower-ranked candidate.  The final fit enforces them.
        """
        rejected_gaps: list[str] = []
        rejected_needs: list[str] = []

        def reject(obj: RetrievedObject, gap: str, *needs: str) -> None:
            draft.budget_drops += 1
            draft.answer_dropped |= not is_background(obj)
            rejected_gaps.append(f"{gap}:{obj.ref}@{obj.revision}")
            rejected_needs.extend(needs)

        for _candidate, obj in verified:
            expandable = f"expandable:{obj.ref}@{obj.revision}"
            if len(draft.items) >= limits.max_items:
                reject(obj, "budget_item_cap")
                continue
            if not fits_packet_schema(obj):
                reject(obj, "budget_oversized", expandable)
                continue
            variants = compact_episode_variants(obj)
            if obj.kind == "episode" and not next_step_provenance_supported(obj):
                if not variants:
                    reject(obj, "resume_state_unverified", "resume_state")
                    continue
                contents, needs = variants, ["resume_state"]
            else:
                contents, needs = (None, *variants), []
            for content in contents:
                item = packet_item(obj, content=content)
                if self._fits(
                    draft, limits, items=[*draft.items, item], objects=[*draft.objects, obj], extra_needs=needs
                ):
                    draft.deliver(item, obj, needs)
                    if content is not None:
                        draft.unmet_needs.extend(_compaction_needs(resume_fields(obj) or {}, content))
                    break
            else:
                reject(obj, "budget_token_cap", expandable, *needs)
        draft.gaps.extend(rejected_gaps)
        draft.unmet_needs.extend(rejected_needs)

    def _fit(self, draft: _Draft, limits: SearchLimits) -> bool:
        """Re-check the exact final envelope; returns whether the diagnostic ref fits.

        Preference order at the cap: keep everything; withhold only the optional
        diagnostic lookup ref (the persistent record itself stays); repack
        one retained episode into fewer complete fields; finally drop the
        lowest-ranked complete unit.  Content, qualifiers and evidence are never
        sliced.
        """
        while True:
            if self._fits(draft, limits):
                return True
            if self._fits(draft, limits, diagnostic_ref=False):
                return False
            if not draft.items:
                # Nothing left to trade; ``bounded_packet`` applies the minimal envelope.
                return True
            if self._repack_episode(draft, limits):
                continue
            draft.items.pop()
            dropped = draft.objects.pop()
            draft.budget_drops += 1
            draft.answer_dropped |= not is_background(dropped)
            draft.gaps.append(f"budget_packet_cap:{dropped.ref}@{dropped.revision}")
            draft.unmet_needs.append(f"expandable:{dropped.ref}@{dropped.revision}")

    def _repack_episode(self, draft: _Draft, limits: SearchLimits) -> bool:
        """Replace the lowest-ranked episode with its first smaller complete variant that fits."""
        for index in range(len(draft.items) - 1, -1, -1):
            obj = draft.objects[index]
            current = draft.items[index]["content"]
            for compact in compact_episode_variants(obj):
                if canonical_render_json(compact) == current:
                    continue
                trial = list(draft.items)
                trial[index] = packet_item(obj, content=compact)
                if self._fits(draft, limits, items=trial, diagnostic_ref=False):
                    draft.items[index] = trial[index]
                    if "goal" not in compact or "next_step" not in compact:
                        draft.unmet_needs.append("resume_state")
                    return True
        return False

    def _finalize(
        self, draft: _Draft, limits: SearchLimits, started: float, *, include_diagnostic_ref: bool = True
    ) -> RecallPacket:
        packet, raw_gaps = assemble_packet(draft)
        packet["diagnostic_ref"] = _DIAGNOSTIC_REF_PLACEHOLDER if include_diagnostic_ref and self.diagnostics else None
        packet = bounded_packet(packet, limits.budget_tokens)
        measured = canonical_render_json(packet)
        diagnostic_ref = None
        if self.diagnostics is not None:
            # Compilation stages metrics in the outer attempt. Only the public
            # lookup ref is optional when it would displace answer evidence.
            context = draft.context
            diagnostic_ref = self.diagnostics.record(
                installation_id=context.trusted_context.binding.installation_id,
                session_id=context.trusted_context.session_id,
                request_id=draft.result.request_id or context.request_id,
                memory_epoch=draft.memory_epoch,
                phase="compile",
                status=packet["status"],
                retrieval_gaps=tuple(draft.result.gaps),
                compile_gaps=raw_gaps,
                items_delivered=len(packet["items"]),
                rendered_bytes=len(measured.encode("utf-8")),
                estimated_tokens=estimate_tokens(measured),
                budget_tokens=limits.budget_tokens,
                items_dropped_stale=draft.stale_drops,
                items_dropped_budget=draft.budget_drops,
                elapsed_ms=int((self._clock.monotonic() - started) * 1000),
                deadline_remaining_ms=self._remaining_ms(context),
                stage_timings=(
                    *draft.result.diagnostics,
                    ("compile", f"{(self._clock.monotonic() - started) * 1000:.3f}"),
                ),
            )
        packet["diagnostic_ref"] = diagnostic_ref if packet.get("diagnostic_ref") else None
        if packet["items"] and self._remaining_ms(draft.context) == 0:
            draft.expire()
            # The second pass has no items, so it cannot recurse again. Its
            # diagnostic describes the empty packet that is actually returned.
            return self._finalize(draft, limits, started, include_diagnostic_ref=include_diagnostic_ref)
        return packet


def _compaction_needs(resume: dict, compact: dict) -> list[str]:
    """A compacted resume that omits the superseded goal, or a next step the
    full resume had, leaves a bounded resume slot for a later focused retrieval."""
    if "goal" not in compact or ("next_step" in resume and "next_step" not in compact):
        return ["resume_state"]
    return []


# -- rendering ----------------------------------------------------------------


@dataclass(frozen=True)
class RecallRenderPreparation:
    """Prepared render context for host injection; receipt is not host adoption."""

    render_ref: str | None
    context: dict[str, object] | None
    canonical_text: str | None = None


class RecallPacketRenderer:
    """Bounded renderer boundary producing memory-only injection contexts."""

    def __init__(self, *, max_prepared: int = _MAX_RENDER_PREPARED) -> None:
        self._max_prepared = max_prepared
        self._lock = threading.RLock()
        self._prepared: OrderedDict[tuple[str, str, str, int | None], RecallRenderPreparation] = OrderedDict()
        self._counter = 0

    def prepare(
        self, packet: RecallPacket, *, installation_id: str, session_id: str, request_id: str
    ) -> RecallRenderPreparation:
        key = (installation_id, session_id, request_id, packet.get("memory_epoch"))
        with self._lock:
            if key in self._prepared:
                # A repeated automatic callback is acknowledged as a duplicate
                # without producing a second non-empty host injection payload.
                return RecallRenderPreparation(None, None, None)
            context = self._build_context(packet)
            render_ref = None
            if context is not None:
                self._counter += 1
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "installation_id": installation_id,
                            "session_id": session_id,
                            "request_id": request_id[:100],
                            "counter": self._counter,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:16]
                render_ref = f"recall-render:{digest}"
            prepared = RecallRenderPreparation(
                render_ref, context, canonical_render_json(context) if context is not None else None
            )
            self._prepared[key] = prepared
            while len(self._prepared) > self._max_prepared:
                self._prepared.popitem(last=False)
            return prepared

    @staticmethod
    def _build_context(packet: RecallPacket) -> dict[str, object] | None:
        if not packet["items"]:
            return None
        payload = {
            "schema": _RENDER_CONTEXT_SCHEMA,
            "request_id": packet["request_id"],
            "status": packet["status"],
            "memory_epoch": packet["memory_epoch"],
            "items": [
                {
                    "ref": item["ref"],
                    "revision": item["revision"],
                    "kind": item["kind"],
                    "content": item["content"],
                    "temporal_status": item["temporal_status"],
                    "origin": item["origin"],
                    "applicability": item["applicability"],
                    "evidence_refs": list(item["evidence_refs"]),
                    "basis": item["basis"],
                    "expandable": item["expandable"],
                    **(
                        {"source_contexts": [dict(context) for context in item["source_contexts"]]}
                        if "source_contexts" in item
                        else {}
                    ),
                    **(
                        {"qualification_reason": item["qualification_reason"]} if "qualification_reason" in item else {}
                    ),
                    **({"occurred_at": item["occurred_at"]} if "occurred_at" in item else {}),
                    **({"entries": [dict(label) for label in item["entries"]]} if "entries" in item else {}),
                }
                for item in packet["items"]
            ],
            "gaps": list(packet["gaps"]),
            "unmet_needs": list(packet["unmet_needs"]),
            "coverage": packet["coverage"],
            "answerability": packet["answerability"],
        }
        # The canonical compact serializer decides the exposed dict and text alike.
        return json.loads(canonical_render_json(payload))


def compile_recall_packet(
    context: SearchContext,
    result: RetrievalResult,
    storage,
    *,
    storage_reader: RetrievalStorage | None = None,
    diagnostics: RecallDiagnostics | None = None,
    clock: RecallClock | None = None,
) -> RecallPacket:
    compiler = RecallPacketCompiler(
        storage_reader if storage_reader is not None else RetrievalStorage(clock=clock or time),
        diagnostics=diagnostics,
        clock=clock or time,
    )
    return compiler.compile(context, result, storage)


def render_recall_packet_context(
    packet: RecallPacket,
    *,
    installation_id: str,
    session_id: str,
    renderer: RecallPacketRenderer | None = None,
) -> RecallRenderPreparation:
    """Prepare an injection-ready memory context without instruction authority."""
    active = renderer if renderer is not None else RecallPacketRenderer()
    return active.prepare(
        packet, installation_id=installation_id, session_id=session_id, request_id=packet["request_id"]
    )
