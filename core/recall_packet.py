"""Typed RecallPacket compiler for the single P08 retrieval pipeline.

The compiler consumes exactly one completed :class:`RetrievalResult` and never
searches, hydrates independently, or re-determines truth.  Release-time
rechecks go through the existing read boundary within the original deadline.

``compile`` reads top-down: release (verify, then fence) -> order -> admit ->
fit -> finalize.  Everything the public packet is computed from lives in one
:class:`_Draft`; :func:`assemble_packet` is the one place that knows the
status, answerability and coverage vocabulary.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import copy
import hashlib
import json
import threading
import time
from typing import Literal, Protocol, cast

from ..contracts import Basis, ContractError, RecallItem, RecallPacket, bounded_source_contexts
from .background_context import is_background, mark_background
from .recall_budget import canonical_render_json, estimate_tokens, event_admission_order
from .recall_diagnostics import RecallDiagnostics
from .recall_needs import RESUME_MARKERS, mentions, unmet_needs
from .resume_compaction import compact_episode_variants, next_step_provenance_supported, resume_evidence_refs, resume_fields
from .retrieval import CandidateRef, RetrievalResult, RetrievedObject, SearchContext, SearchLimits, effective_limits, optional_json
from .retrieval_storage import RetrievalStorage

_BASIS = frozenset({"direct_report", "observed", "derived_summary", "inferred_suggestion", "unknown"})
_CLAIM_STATES = frozenset({"proposed", "active", "superseded", "disputed", "retracted"})
_AUTHORITY_GAP_PREFIXES = ("sqlite_unavailable",)
_INCOMPLETE_GAP_PREFIXES = ("deadline_exceeded", "sqlite_unavailable", "sqlite_candidate_error")
#: Markers published as their bare prefix, so diagnostics stay useful without
#: refs consuming the packet budget.  Everything else passes through intact.
_BARE_MARKER_PREFIXES = frozenset({
    "budget_token_cap", "budget_packet_cap", "budget_oversized", "expandable",
    "stale_candidate", "revision_changed", "resume_state_unverified",
})
_MAX_PACKET_ITEM_CHARS = 12000
_MAX_APPLICABILITY_CHARS = 2048
_MAX_ORIGIN_CHARS = 80
_MAX_OCCURRED_AT_CHARS = 64
_MAX_EVIDENCE_REFS = 32
_MAX_RENDER_PREPARED = 64
_RENDER_CONTEXT_SCHEMA = "scope-recall.recall_context/1.1"
# Same width as ``RecallDiagnostics.record`` refs (``recall-diag:`` + 16 hex),
# so a packet measured before recording has the bytes of the packet returned.
_DIAGNOSTIC_REF_PLACEHOLDER = "recall-diag:" + ("0" * 16)

PacketKind = Literal["event", "episode", "claim", "procedure", "artifact"]
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


def packet_kind(obj: RetrievedObject) -> PacketKind:
    if obj.kind in {"event", "episode", "claim", "procedure", "artifact"}:
        return cast(PacketKind, obj.kind)
    return "event"


def packet_item(obj: RetrievedObject, *, content: dict | None = None) -> RecallItem:
    """The public item for one released object.

    ``content`` replaces an episode resume with a compact field selection; the
    item then carries every ref of every retained field, so a cross-field
    packet stays provable rather than merely plausible-looking.
    """
    evidence = list(dict.fromkeys(obj.evidence_refs)) if content is None else list(resume_evidence_refs(content))
    if not evidence:
        evidence = [f"{obj.ref}@{obj.revision}"]
    item: RecallItem = {
        "ref": obj.ref,
        "revision": obj.revision,
        "kind": packet_kind(obj),
        "content": obj.content if content is None else canonical_render_json(content),
        "temporal_status": obj.temporal_status,
        "origin": obj.origin,
        "applicability": obj.applicability,
        "evidence_refs": evidence,
        "expandable": bool(obj.expandable),
        "basis": cast(Basis, obj.basis if obj.basis in _BASIS else "unknown"),
    }
    metadata = dict(obj.metadata)
    # An unpromoted claim reaches recall labelled: temporal_status alone cannot
    # tell a lead from a retired truth, since both look "historical".
    if metadata.get("state") in _CLAIM_STATES:
        item["claim_state"] = metadata["state"]
        if metadata.get("qualification_reason"):
            item["qualification_reason"] = str(metadata["qualification_reason"])[:120]
    contexts = bounded_source_contexts(optional_json(metadata.get("source_contexts")))
    if contexts:
        item["source_contexts"] = contexts
    occurred = metadata.get("occurred_at")
    if type(occurred) is str and 0 < len(occurred) <= _MAX_OCCURRED_AT_CHARS:
        item["occurred_at"] = occurred
    return item


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
    if not mentions(context.query, RESUME_MARKERS):
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
    if context.mode not in {"auto", "current"} or mentions(context.query, RESUME_MARKERS):
        return verified
    if any(candidate.source == "exact_ref" for candidate, _obj in verified):
        return verified

    prioritized: list[Pair] = []
    run: list[Pair] = []

    def flush_run() -> None:
        event_refs = {f"{obj.ref}@{obj.revision}" for _candidate, obj in run if obj.kind == "event"}
        promoted = [pair for pair in run
                    if is_active_direct_claim(pair[1]) and event_refs.intersection(pair[1].evidence_refs)]
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

    def note_epoch(self, epoch: int, gap: str) -> bool:
        """Record an epoch move; ``True`` means the verified set is no longer released."""
        if self.memory_epoch is None or epoch == self.memory_epoch:
            return False
        self.gaps.append(gap)
        self.memory_epoch = epoch
        return True

    def discard(self, verified: list[Pair], gap: str | None = None) -> list[Pair]:
        if gap is not None:
            self.gaps.append(gap)
        self.stale_drops += len(verified)
        return []

    def deliver(self, item: RecallItem, obj: RetrievedObject, needs: list[str]) -> None:
        self.items.append(item)
        self.objects.append(obj)
        self.unmet_needs.extend(needs)


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

    if (authority_failed or read_incomplete) and not items:
        status = "unavailable"
    elif not items:
        status = "partial" if result.items and dropped else "no_match"
    elif gaps or needs or dropped:
        status = "partial"
    else:
        status = "ok"

    if status in {"no_match", "unavailable"} or not answer_objects:
        answerability = "unknown"
    elif needs or dropped or gaps:
        answerability = "partial"
    elif result.answerability_hint in {"ambiguous", "supported"}:
        answerability = result.answerability_hint
    else:
        answerability = "partial"

    coverage = "partial" if gaps or dropped or needs else result.coverage

    if status in {"no_match", "unavailable"}:
        packet_epoch: int | None = None
    elif items:
        packet_epoch = draft.memory_epoch if draft.memory_epoch is not None else 0
    else:
        packet_epoch = draft.memory_epoch

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
    minimal = dict(packet, status="unavailable", memory_epoch=None, items=[],
                   gaps=["budget_packet_cap"], diagnostic_ref=None,
                   answerability="unknown", coverage="unknown", unmet_needs=[])
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

    def __post_init__(self) -> None:
        if self.clock is None:
            object.__setattr__(self, "clock", time)

    def compile(self, context: SearchContext, result: RetrievalResult, storage) -> RecallPacket:
        if not isinstance(context, SearchContext) or not isinstance(result, RetrievalResult):
            raise ContractError("INPUT_INVALID", "compile_input")
        started = self.clock.monotonic()
        limits = effective_limits(context)
        draft = _Draft(context, result, result.memory_epoch, gaps=list(result.gaps), unmet_needs=list(result.unmet_needs))
        if self._remaining_ms(context) == 0:
            draft.gaps.append("deadline_exceeded_compile")
            return self._finalize(draft, limits, started)
        verified = self._verify(storage, draft, list(zip(result.candidates, result.items)))
        verified = self._fence(storage, draft, verified)
        self._admit(draft, self._ordered(context, verified, limits), limits)
        include_diagnostic_ref = self._fit(draft, limits)
        if draft.items and self._remaining_ms(context) == 0:
            draft.gaps.append("deadline_exceeded_compile")
            draft.stale_drops += len(draft.items)
            draft.items.clear()
            draft.objects.clear()
        return self._finalize(draft, limits, started, include_diagnostic_ref=include_diagnostic_ref)

    # -- release --------------------------------------------------------------

    def _remaining_ms(self, context: SearchContext) -> int:
        return max(0, int((context.deadline - self.clock.monotonic()) * 1000))

    def _read(self, storage, context: SearchContext):
        return storage.read(context.trusted_context, remaining_seconds=max(context.deadline - self.clock.monotonic(), 0.001))

    def _recheck(self, tx, context: SearchContext, candidate: CandidateRef, obj: RetrievedObject, gaps: list[str]) -> RetrievedObject | None:
        if self._remaining_ms(context) == 0:
            gaps.append("deadline_exceeded_release")
            return None
        fresh = self.storage_reader.hydrate(tx, candidate, context)
        if fresh is None:
            gaps.append(f"stale_candidate:{candidate.ref}@{candidate.revision}")
            return None
        if (fresh.ref, fresh.revision) != (obj.ref, obj.revision):
            gaps.append(f"revision_changed:{obj.ref}@{obj.revision}")
            return None
        return mark_background(fresh) if candidate.source == "background" else fresh

    def _verify(self, storage, draft: _Draft, ranked: list[Pair]) -> list[Pair]:
        """Rehydrate every candidate in one fresh read; an epoch move empties it."""
        context = draft.context
        verified: list[Pair] = []
        try:
            with self._read(storage, context) as tx:
                draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed")
                for candidate, item in ranked:
                    if self._remaining_ms(context) == 0:
                        draft.gaps.append("deadline_exceeded_release")
                        break
                    fresh = self._recheck(tx, context, candidate, item, draft.gaps)
                    if fresh is None:
                        draft.stale_drops += 1
                        continue
                    verified.append((candidate, fresh))
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release"):
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
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release_fence"):
                    return draft.discard(verified)
                for candidate, obj in verified:
                    if self._recheck(tx, context, candidate, obj, draft.gaps) is None:
                        return draft.discard(verified, "release_delete_fence")
                    if self._remaining_ms(context) == 0:
                        return draft.discard(verified, "deadline_exceeded_release_fence")
                if draft.note_epoch(self.storage_reader.epoch(tx), "epoch_changed_release_fence"):
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

    def _fits(self, draft: _Draft, limits: SearchLimits, *, items=None, objects=None, extra_needs=(), diagnostic_ref: bool = True) -> bool:
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
                if self._fits(draft, limits, items=[*draft.items, item], objects=[*draft.objects, obj], extra_needs=needs):
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
        diagnostic lookup ref (the process-local record itself stays); repack
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

    def _finalize(self, draft: _Draft, limits: SearchLimits, started: float, *, include_diagnostic_ref: bool = True) -> RecallPacket:
        packet, raw_gaps = assemble_packet(draft)
        packet["diagnostic_ref"] = _DIAGNOSTIC_REF_PLACEHOLDER if include_diagnostic_ref and self.diagnostics else None
        packet = bounded_packet(packet, limits.budget_tokens)
        measured = canonical_render_json(packet)
        diagnostic_ref = None
        if self.diagnostics is not None:
            # The process-local record is always written; only the public
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
                elapsed_ms=int((self.clock.monotonic() - started) * 1000),
                deadline_remaining_ms=self._remaining_ms(context),
            )
        packet["diagnostic_ref"] = diagnostic_ref if packet.get("diagnostic_ref") else None
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

    def prepare(self, packet: RecallPacket, *, installation_id: str, session_id: str, request_id: str) -> RecallRenderPreparation:
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
                digest = hashlib.sha256(json.dumps(
                    {"installation_id": installation_id, "session_id": session_id,
                     "request_id": request_id[:100], "counter": self._counter},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()[:16]
                render_ref = f"recall-render:{digest}"
            prepared = RecallRenderPreparation(render_ref, context, canonical_render_json(context) if context is not None else None)
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
                    **({"source_contexts": [dict(context) for context in item["source_contexts"]]}
                       if "source_contexts" in item else {}),
                    **({"occurred_at": item["occurred_at"]} if "occurred_at" in item else {}),
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
    return active.prepare(packet, installation_id=installation_id, session_id=session_id, request_id=packet["request_id"])
