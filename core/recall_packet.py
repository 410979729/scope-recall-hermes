"""Typed RecallPacket compiler for the single P08 retrieval pipeline.

The compiler consumes exactly one completed :class:`RetrievalResult` and never
searches, hydrates independently, or re-determines truth.  Release-time
rechecks go through the existing read boundary within the original deadline.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import copy
import hashlib
import json
import threading
import time
import unicodedata
from typing import Literal, Protocol, cast

from ..contracts import Basis, ContractError, RecallItem, RecallPacket, bounded_source_contexts
from .recall import RetrievalPipeline
from .recall_diagnostics import RecallDiagnostics
from .retrieval import AUTOMATIC_PACKET_BUDGET_UNITS, CandidateRef, RetrievedObject, RetrievalResult, SearchContext, SearchLimits
from .retrieval_storage import RetrievalStorage
from .background_context import is_background, mark_background

_BASIS = frozenset({"direct_report", "observed", "derived_summary", "inferred_suggestion", "unknown"})
_AUTHORITY_GAP_PREFIXES = ("sqlite_unavailable",)
_INCOMPLETE_GAP_PREFIXES = ("deadline_exceeded", "sqlite_unavailable", "sqlite_candidate_error")
_MAX_PACKET_ITEM_CHARS = 12000
_MAX_APPLICABILITY_CHARS = 2048
_MAX_ORIGIN_CHARS = 80
_MAX_EVIDENCE_REFS = 32
_MAX_RENDER_PREPARED = 64
_RENDER_CONTEXT_SCHEMA = "scope-recall.recall_context/1.1"
_RESUME_QUERY_MARKERS = ("继续", "接着", "恢复", "resume", "continue")
# Same width as ``RecallDiagnostics.record`` refs (``recall-diag:`` + 16 hex),
# so a packet measured before recording has the bytes of the packet returned.
_DIAGNOSTIC_REF_PLACEHOLDER = "recall-diag:" + ("0" * 16)


class RecallClock(Protocol):
    def monotonic(self) -> float: ...


def canonical_render_json(value: object) -> str:
    """Serialize rendered recall data once, compactly and deterministically."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def isolate_recall_packet(packet: RecallPacket) -> RecallPacket:
    """Return a caller-owned copy that cannot mutate cached compiler state."""
    # Evidence refs and other item fields are nested mutable lists; a shallow
    # item copy would still let a caller mutate the cached packet indirectly.
    return copy.deepcopy(packet)


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
        self,
        packet: RecallPacket,
        *,
        installation_id: str,
        session_id: str,
        request_id: str,
    ) -> RecallRenderPreparation:
        key = (installation_id, session_id, request_id, packet.get("memory_epoch"))
        with self._lock:
            existing = self._prepared.get(key)
            if existing is not None:
                # A repeated automatic callback is acknowledged as a duplicate
                # without producing a second non-empty host injection payload.
                return RecallRenderPreparation(None, None, None)
            context = self._build_context(packet)
            canonical_text = canonical_render_json(context) if context is not None else None
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
            self._prepared[key] = RecallRenderPreparation(
                render_ref,
                copy.deepcopy(context) if context is not None else None,
                canonical_text,
            )
            self._prepared.move_to_end(key)
            while len(self._prepared) > self._max_prepared:
                self._prepared.popitem(last=False)
            return RecallRenderPreparation(
                render_ref,
                copy.deepcopy(context) if context is not None else None,
                canonical_text,
            )

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
                }
                for item in packet["items"]
            ],
            "gaps": list(packet["gaps"]),
            "unmet_needs": list(packet["unmet_needs"]),
            "coverage": packet["coverage"],
            "answerability": packet["answerability"],
        }
        # Reuse the compiler's canonical compact serializer before exposing a
        # dict and canonical text to either host.
        return json.loads(canonical_render_json(payload))


@dataclass(frozen=True)
class RecallPacketCompiler:
    """Compile one retrieval snapshot into the public RecallPacket contract."""

    storage_reader: RetrievalStorage
    diagnostics: RecallDiagnostics | None = None
    clock: RecallClock | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            object.__setattr__(self, "clock", time)

    def _clock(self) -> RecallClock:
        clock = self.clock
        if clock is None:
            return time
        return clock

    @classmethod
    def _rendered_budget_bytes(cls, obj: RetrievedObject) -> int:
        """Return the canonical UTF-8 byte upper bound for one rendered item."""

        return len(canonical_render_json(cls._to_item(obj)).encode("utf-8"))

    @staticmethod
    def _envelope_bytes(packet: RecallPacket, *, include_diagnostic_ref: bool = True) -> int:
        """Count the canonical compact bytes of one assembled packet.

        The canonical compact JSON byte count is a fixed, explainable upper
        bound: a byte-level tokenizer can consume at most one token per UTF-8
        byte.  ``include_diagnostic_ref=False`` measures the same packet with
        the optional lookup ref withheld.
        """

        if include_diagnostic_ref:
            return len(canonical_render_json(packet).encode("utf-8"))
        return len(canonical_render_json(dict(packet, diagnostic_ref=None)).encode("utf-8"))

    @staticmethod
    def _public_marker(value: str) -> str:
        """Keep diagnostics useful without letting refs consume the packet budget."""

        if type(value) is not str:
            return "diagnostic_invalid"
        prefix = value.split(":", 1)[0]
        if prefix in {
            "budget_token_cap",
            "budget_packet_cap",
            "budget_oversized",
            "expandable",
            "stale_candidate",
            "revision_changed",
            "resume_state_unverified",
        }:
            return prefix
        return value[:160]

    @classmethod
    def _public_gaps(cls, values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(cls._public_marker(value) for value in values if value))[:16]

    @classmethod
    def _public_needs(cls, values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(cls._public_marker(value) for value in values if value))[:8]

    @staticmethod
    def _source_order(obj: RetrievedObject) -> dict[str, int]:
        """Read the bounded trusted event order attached during hydration."""

        raw = dict(obj.metadata).get("source_order")
        if type(raw) is not str:
            return {}
        try:
            values = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        order: dict[str, int] = {}
        if not isinstance(values, list):
            return order
        for value in values[:32]:
            if not isinstance(value, list) or len(value) != 3:
                continue
            ref, revision, sequence = value
            if type(ref) is not str or type(revision) is not int or type(sequence) is not int:
                continue
            order[f"{ref}@{revision}"] = sequence
        return order

    @staticmethod
    def _source_texts(obj: RetrievedObject) -> dict[str, str]:
        """Read freshly hydrated source text kept beside trusted ordering."""

        raw = dict(obj.metadata).get("source_texts")
        if type(raw) is not str:
            return {}
        try:
            values = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(values, dict):
            return {}
        return {
            ref: text
            for ref, text in values.items()
            if type(ref) is str and type(text) is str
        }

    @staticmethod
    def _source_mentions_next_step(source_text: str, next_step: object) -> bool:
        if type(next_step) is not str or not next_step.strip():
            return False
        def normalize(value: str) -> str:
            return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        needle = normalize(next_step)
        haystack = normalize(source_text)
        return bool(needle and needle in haystack)

    @classmethod
    def _next_step_provenance_supported(cls, obj: RetrievedObject) -> bool:
        """Whether a resume next step has fresh text support.

        Missing optional order metadata is handled by the compatibility branch
        that preserves the original support refs.  Once trusted order exists,
        however, an unverified next step must be omitted even when the full
        episode happens to fit the packet budget.
        """

        if obj.kind != "episode" or type(obj.content) is not str:
            return True
        try:
            resume = json.loads(obj.content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        if not isinstance(resume, dict) or resume.get("next_step") in (None, "", [], {}):
            return True
        source_order = cls._source_order(obj)
        if not source_order:
            return True
        source_texts = cls._source_texts(obj)
        refs = [ref for ref in resume.get("evidence_refs", ()) if type(ref) is str]
        return any(
            ref in source_order
            and cls._source_mentions_next_step(source_texts.get(ref, ""), resume["next_step"])
            for ref in refs
        )

    @staticmethod
    def _value_refs(value: object) -> tuple[str, ...]:
        refs: list[str] = []

        def collect(item: object) -> None:
            if isinstance(item, dict):
                for ref_key in ("evidence_refs", "next_step_evidence_refs"):
                    raw = item.get(ref_key)
                    if isinstance(raw, list):
                        refs.extend(ref for ref in raw if type(ref) is str)
                for child in item.values():
                    collect(child)
            elif isinstance(item, list):
                for child in item:
                    collect(child)

        collect(value)
        return tuple(dict.fromkeys(refs))

    @classmethod
    def _latest_resume_entry(
        cls,
        value: object,
        source_order: dict[str, int],
    ) -> dict[str, object] | None:
        if not isinstance(value, list):
            return None
        entries = [entry for entry in value if isinstance(entry, dict) and entry]
        if not entries:
            return None
        ranked: list[tuple[int, int, dict[str, object]]] = []
        unranked_entry = False
        for index, entry in enumerate(entries):
            refs = cls._value_refs(entry)
            sequences = [source_order[ref] for ref in refs if ref in source_order]
            if refs and len(sequences) == len(refs):
                ranked.append((max(sequences), -index, entry))
            else:
                # A known sequence cannot establish "latest" while another
                # candidate is missing trusted order.  Carrying the known one
                # would turn incomplete provenance into a current claim.
                unranked_entry = True
        if ranked and not unranked_entry:
            # Source sequence, not the order in a model-generated list, is the
            # authority for choosing the current correction/progress entry.
            return max(ranked, key=lambda item: (item[0], item[1]))[2]
        # Without trusted source order, more than one entry cannot establish a
        # current correction.  A single complete entry remains safe to carry.
        return entries[0] if len(entries) == 1 else None

    @classmethod
    def _compact_episode_variants(cls, obj: RetrievedObject) -> tuple[str, ...]:
        """Select complete resume fields when a full resume cannot fit.

        This is field selection, never string slicing: every retained value and
        evidence reference remains intact JSON.  The current correction and
        resumable next step are preferred over stale history and bookkeeping.
        """

        if obj.kind != "episode" or type(obj.content) is not str:
            return ()
        try:
            resume = json.loads(obj.content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if type(resume) is not dict:
            return ()

        source_order = cls._source_order(obj)
        source_texts = cls._source_texts(obj)
        selected_fields: dict[str, object] = {}
        for key in ("decisions", "verified_progress"):
            selected = cls._latest_resume_entry(resume.get(key), source_order)
            if selected is not None:
                selected_fields[key] = [selected]

        # next_step historically carried only global evidence_refs.  Keep it
        # only when freshly hydrated source text actually contains the
        # proposed step.  Sequence alone is provenance, not semantic support.
        next_step = resume.get("next_step")
        next_refs = [ref for ref in resume.get("evidence_refs", ()) if type(ref) is str]
        supported_next_refs = [
            ref for ref in next_refs
            if ref in source_order
            and cls._source_mentions_next_step(source_texts.get(ref, ""), next_step)
        ]
        if source_order:
            if supported_next_refs and next_step not in (None, "", [], {}):
                selected_fields["next_step"] = next_step
                if resume.get("next_step_basis") not in (None, "", [], {}):
                    selected_fields["next_step_basis"] = resume["next_step_basis"]
                selected_fields["next_step_evidence_refs"] = sorted(
                    supported_next_refs, key=lambda ref: source_order[ref]
                )
        elif next_step not in (None, "", [], {}):
            # Test doubles and older readers may not expose the optional order
            # metadata.  Preserve only the original bounded global refs.
            selected_fields["next_step"] = next_step
            if resume.get("next_step_basis") not in (None, "", [], {}):
                selected_fields["next_step_basis"] = resume["next_step_basis"]
            if next_refs:
                selected_fields["next_step_evidence_refs"] = next_refs

        if "next_step" not in selected_fields:
            open_item = cls._latest_resume_entry(resume.get("open_items"), source_order)
            if open_item is not None:
                selected_fields["open_items"] = [open_item]

        if not selected_fields:
            goal = resume.get("goal")
            if goal not in (None, "", [], {}):
                selected_fields["goal"] = goal
        elif not any(key in selected_fields for key in ("decisions", "verified_progress")):
            goal = resume.get("goal")
            if goal not in (None, "", [], {}):
                selected_fields["goal"] = goal

        if not selected_fields:
            return ()

        # Prefer all independently supported fields first, then progressively
        # select fewer complete fields if the packet budget requires it.  No
        # variant removes a field's evidence_refs after keeping that field.
        variants: list[dict[str, object]] = []
        variants.append(dict(selected_fields))
        for drop in (
            ("goal",),
            ("verified_progress",),
            ("open_items",),
            ("next_step", "next_step_basis", "next_step_evidence_refs"),
        ):
            candidate = {key: value for key, value in selected_fields.items() if key not in drop}
            if candidate and candidate not in variants:
                variants.append(candidate)
        encoded_values: list[str] = []
        original_encoded = canonical_render_json(resume)
        for compact in variants:
            encoded = canonical_render_json(compact)
            if encoded != original_encoded and encoded not in encoded_values:
                encoded_values.append(encoded)
        return tuple(encoded_values)

    @classmethod
    def _compact_episode_content(cls, obj: RetrievedObject) -> str | None:
        variants = cls._compact_episode_variants(obj)
        return variants[0] if variants else None

    @staticmethod
    def _fits_packet_schema(obj: RetrievedObject) -> bool:
        if len(obj.content) > _MAX_PACKET_ITEM_CHARS:
            return False
        if len(obj.applicability) > _MAX_APPLICABILITY_CHARS:
            return False
        if len(obj.origin) > _MAX_ORIGIN_CHARS:
            return False
        if len(obj.evidence_refs) > _MAX_EVIDENCE_REFS or any(
            type(ref) is not str or len(ref) > 240 for ref in obj.evidence_refs
        ):
            return False
        return True

    @staticmethod
    def _effective_limits(context: SearchContext) -> SearchLimits:
        if context.mode != "auto":
            return context.limits
        return SearchLimits(
            max_items=min(context.limits.max_items, 6),
            budget_tokens=min(context.limits.budget_tokens, AUTOMATIC_PACKET_BUDGET_UNITS),
            candidate_pool=context.limits.candidate_pool,
            recent_items=context.limits.recent_items,
            relation_hops=context.limits.relation_hops,
            relation_objects=context.limits.relation_objects,
            vector_limit=context.limits.vector_limit,
            followups=context.limits.followups,
        )

    @staticmethod
    def _authority_unavailable(gaps: tuple[str, ...]) -> bool:
        return any(gap.startswith(prefix) for gap in gaps for prefix in _AUTHORITY_GAP_PREFIXES)

    @staticmethod
    def _critical_read_incomplete(gaps: tuple[str, ...]) -> bool:
        """Identify an unfinished read without treating ordinary vector gaps as faults."""

        return any(gap.startswith(prefix) for gap in gaps for prefix in _INCOMPLETE_GAP_PREFIXES)

    @staticmethod
    def _packet_kind(item: RetrievedObject) -> Literal["event", "episode", "claim", "procedure", "artifact"]:
        if item.kind == "procedure":
            return "procedure"
        if item.kind in {"event", "episode", "claim", "artifact"}:
            return cast(Literal["event", "episode", "claim", "procedure", "artifact"], item.kind)
        return "event"

    @staticmethod
    def _packet_basis(value: str) -> Basis:
        return cast(Basis, value if value in _BASIS else "unknown")

    @staticmethod
    def _to_item(obj: RetrievedObject, *, content: str | None = None) -> RecallItem:
        evidence = list(dict.fromkeys(obj.evidence_refs))
        if content is not None and obj.kind == "episode":
            try:
                selected = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                selected = None
            def collect(value: object, output: list[str]) -> None:
                if isinstance(value, dict):
                    for ref_key in ("evidence_refs", "next_step_evidence_refs"):
                        refs = value.get(ref_key)
                        if isinstance(refs, list):
                            output.extend(ref for ref in refs if type(ref) is str)
                    for child in value.values():
                        collect(child, output)
                elif isinstance(value, list):
                    for child in value:
                        collect(child, output)

            if isinstance(selected, dict):
                preferred_refs: list[str] = []
                # Every retained field must carry all of its refs.  Do not
                # stop after the first field: cross-field packets otherwise
                # become plausible-looking but unprovable.
                collect(selected, preferred_refs)
                evidence = list(dict.fromkeys(preferred_refs))
        if not evidence:
            evidence = [f"{obj.ref}@{obj.revision}"]
        item: RecallItem = {
            "ref": obj.ref,
            "revision": obj.revision,
            "kind": RecallPacketCompiler._packet_kind(obj),
            "content": obj.content if content is None else content,
            "temporal_status": obj.temporal_status,
            "origin": obj.origin,
            "applicability": obj.applicability,
            "evidence_refs": evidence,
            "expandable": bool(obj.expandable),
            "basis": RecallPacketCompiler._packet_basis(obj.basis),
        }
        metadata = dict(obj.metadata)
        # An unpromoted claim now reaches recall, so it has to arrive labelled.
        # temporal_status alone cannot carry this: "historical" is also what a
        # genuinely superseded fact looks like, and the reader would have no way
        # to tell a lead from a retired truth.
        claim_state = metadata.get("state")
        if claim_state in {"proposed", "active", "superseded", "disputed", "retracted"}:
            item["claim_state"] = claim_state
            reason = metadata.get("qualification_reason")
            if reason:
                item["qualification_reason"] = str(reason)[:120]
        raw_contexts = metadata.get("source_contexts")
        parsed_contexts = None
        if raw_contexts:
            try:
                parsed_contexts = json.loads(raw_contexts)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_contexts = None
        contexts = bounded_source_contexts(parsed_contexts)
        if contexts:
            item["source_contexts"] = contexts
        return item

    def _remaining_ms(self, context: SearchContext) -> int | None:
        remaining = context.deadline - self._clock().monotonic()
        if remaining < 0:
            return 0
        return int(remaining * 1000)

    def _recheck(
        self,
        tx,
        context: SearchContext,
        candidate: CandidateRef,
        obj: RetrievedObject,
        gaps: list[str],
    ) -> RetrievedObject | None:
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

    @classmethod
    def _prioritize_resume_evidence(
        cls,
        context: SearchContext,
        verified: list[tuple[CandidateRef, RetrievedObject]],
    ) -> list[tuple[CandidateRef, RetrievedObject]]:
        """Keep a complete current episode ahead of incidental relation hits."""

        if not any(marker in context.query.casefold() for marker in _RESUME_QUERY_MARKERS):
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

    @staticmethod
    def _is_active_direct_claim(obj: RetrievedObject) -> bool:
        """Identify current claims that are backed by direct human evidence."""

        metadata = dict(obj.metadata)
        return (
            obj.kind == "claim"
            and not is_background(obj)
            and obj.temporal_status == "current"
            and obj.origin == "human_direct"
            and obj.basis == "direct_report"
            and metadata.get("state") == "active"
            and bool(obj.evidence_refs)
        )

    @classmethod
    def _prioritize_current_claims(
        cls,
        context: SearchContext,
        verified: list[tuple[CandidateRef, RetrievedObject]],
    ) -> list[tuple[CandidateRef, RetrievedObject]]:
        """Keep a normalized current fact ahead of its raw event under budget.

        The promotion is deliberately narrow.  It applies only to ordinary
        current recall after every candidate has passed the SQLite release
        fence, and only when the claim's exact evidence event is also competing
        for the same packet.  Explicit refs and historical/method queries keep
        their original relevance order.
        """

        if context.mode not in {"auto", "current"}:
            return verified
        if any(marker in context.query.casefold() for marker in _RESUME_QUERY_MARKERS):
            return verified
        if any(candidate.source == "exact_ref" for candidate, _obj in verified):
            return verified

        prioritized: list[tuple[CandidateRef, RetrievedObject]] = []
        claim_event_run: list[tuple[CandidateRef, RetrievedObject]] = []

        def flush_run() -> None:
            if not claim_event_run:
                return
            event_refs = {
                f"{obj.ref}@{obj.revision}"
                for _candidate, obj in claim_event_run
                if obj.kind == "event"
            }
            promoted = [
                pair
                for pair in claim_event_run
                if cls._is_active_direct_claim(pair[1])
                and bool(event_refs.intersection(pair[1].evidence_refs))
            ]
            promoted_keys = {pair[0].key for pair in promoted}
            prioritized.extend(promoted)
            prioritized.extend(
                pair for pair in claim_event_run if pair[0].key not in promoted_keys
            )
            claim_event_run.clear()

        for pair in verified:
            if pair[1].kind in {"event", "claim"}:
                claim_event_run.append(pair)
                continue
            flush_run()
            prioritized.append(pair)
        flush_run()
        return prioritized

    def compile(
        self,
        context: SearchContext,
        result: RetrievalResult,
        storage,
    ) -> RecallPacket:
        if not isinstance(context, SearchContext) or not isinstance(result, RetrievalResult):
            raise ContractError("INPUT_INVALID", "compile_input")
        started = self._clock().monotonic()
        limits = self._effective_limits(context)
        compile_gaps: list[str] = list(result.gaps)
        retrieval_gaps = tuple(result.gaps)
        # Per-candidate rejection diagnostics are deferred until selection
        # ends: a rejected candidate must not charge its markers to the
        # admission of every lower-ranked candidate.
        rejection_gaps: list[str] = []
        rejection_needs: list[str] = []
        stale_drops = 0
        budget_drops = 0
        delivered: list[RecallItem] = []
        delivered_objects: list[RetrievedObject] = []
        unmet_needs = list(result.unmet_needs)
        memory_epoch = result.memory_epoch

        if self._remaining_ms(context) == 0:
            compile_gaps.append("deadline_exceeded_compile")
            packet = self._finalize(
                context,
                result,
                limits,
                memory_epoch,
                (),
                (),
                compile_gaps,
                unmet_needs,
                stale_drops,
                budget_drops,
                retrieval_gaps,
                started,
            )
            return self._bounded_packet(packet, limits.budget_tokens)

        ranked = list(zip(result.candidates, result.items))
        verified: list[tuple[CandidateRef, RetrievedObject]] = []
        release_fence_completed = False
        try:
            with storage.read(context.trusted_context, remaining_seconds=max(context.deadline - self._clock().monotonic(), 0.001)) as tx:
                current_epoch = self.storage_reader.epoch(tx)
                if memory_epoch is not None and current_epoch != memory_epoch:
                    compile_gaps.append("epoch_changed")
                    memory_epoch = current_epoch
                for candidate, item in ranked:
                    if self._remaining_ms(context) == 0:
                        compile_gaps.append("deadline_exceeded_release")
                        break
                    fresh = self._recheck(tx, context, candidate, item, compile_gaps)
                    if fresh is None:
                        stale_drops += 1
                        continue
                    verified.append((candidate, fresh))
                final_epoch = self.storage_reader.epoch(tx)
                if memory_epoch is not None and final_epoch != memory_epoch:
                    compile_gaps.append("epoch_changed_release")
                    memory_epoch = final_epoch
                    stale_drops += len(verified)
                    verified = []
        except ContractError as exc:
            compile_gaps.append(f"sqlite_unavailable:{exc.code}")
            stale_drops += len(verified)
            verified = []
        except Exception as exc:
            compile_gaps.append(f"sqlite_unavailable:{type(exc).__name__}")
            stale_drops += len(verified)
            verified = []

        # One fresh SQLite transaction is the release linearization point.  It
        # rehydrates every object that may be delivered and checks the epoch
        # after those reads, all within the original deadline.
        if verified and memory_epoch is not None and self._remaining_ms(context) != 0:
            try:
                with storage.read(
                    context.trusted_context,
                    remaining_seconds=max(context.deadline - self._clock().monotonic(), 0.001),
                ) as fence_tx:
                    fence_epoch = self.storage_reader.epoch(fence_tx)
                    if fence_epoch != memory_epoch:
                        compile_gaps.append("epoch_changed_release_fence")
                        memory_epoch = fence_epoch
                        stale_drops += len(verified)
                        verified = []
                    else:
                        for candidate, obj in verified:
                            if self._recheck(fence_tx, context, candidate, obj, compile_gaps) is None:
                                compile_gaps.append("release_delete_fence")
                                stale_drops += len(verified)
                                verified = []
                                break
                            if self._remaining_ms(context) == 0:
                                compile_gaps.append("deadline_exceeded_release_fence")
                                stale_drops += len(verified)
                                verified = []
                                break
                        if verified:
                            final_epoch = self.storage_reader.epoch(fence_tx)
                            if final_epoch != memory_epoch:
                                compile_gaps.append("epoch_changed_release_fence")
                                memory_epoch = final_epoch
                                stale_drops += len(verified)
                                verified = []
                            else:
                                release_fence_completed = True
            except ContractError as exc:
                compile_gaps.append(f"sqlite_unavailable:{exc.code}")
                stale_drops += len(verified)
                verified = []
            except Exception as exc:
                compile_gaps.append(f"sqlite_unavailable:{type(exc).__name__}")
                stale_drops += len(verified)
                verified = []

        if verified and (not release_fence_completed or self._remaining_ms(context) == 0):
            compile_gaps.append("deadline_exceeded_release_fence")
            stale_drops += len(verified)
            verified = []

        verified = self._prioritize_resume_evidence(context, verified)
        verified = self._prioritize_current_claims(context, verified)
        # Ambient preferences/task state cannot displace answer evidence.
        verified.sort(key=lambda pair: pair[0].source == "background")

        def admits(
            prospective_items: list[RecallItem],
            prospective_objects: list[RetrievedObject],
            prospective_needs: list[str],
        ) -> bool:
            """Measure the exact packet that would be returned if admitted now."""

            packet, _ = self._assemble(
                context,
                result,
                memory_epoch,
                tuple(prospective_items),
                tuple(prospective_objects),
                compile_gaps,
                [*unmet_needs, *prospective_needs],
                stale_drops,
                budget_drops,
            )
            return self._envelope_bytes(packet) <= limits.budget_tokens

        for _candidate, obj in verified:
            if len(delivered) >= limits.max_items:
                budget_drops += 1
                rejection_gaps.append(f"budget_item_cap:{obj.ref}@{obj.revision}")
                continue
            if not self._fits_packet_schema(obj):
                budget_drops += 1
                rejection_gaps.append(f"budget_oversized:{obj.ref}@{obj.revision}")
                rejection_needs.append(f"expandable:{obj.ref}@{obj.revision}")
                continue
            provenance_content = None
            provenance_gap = False
            if obj.kind == "episode" and not self._next_step_provenance_supported(obj):
                compact_contents = self._compact_episode_variants(obj)
                if not compact_contents:
                    budget_drops += 1
                    rejection_gaps.append(f"resume_state_unverified:{obj.ref}@{obj.revision}")
                    rejection_needs.append("resume_state")
                    continue
                provenance_content = compact_contents[0]
                provenance_gap = True
            item = self._to_item(obj, content=provenance_content)
            admitted_needs: list[str] = ["resume_state"] if provenance_gap else []
            # Test the actual envelope this candidate would ship in.  Deferred
            # rejection markers are excluded on purpose; the final re-check
            # below enforces them against the selected set.
            if not admits([*delivered, item], [*delivered_objects, obj], admitted_needs):
                compact_contents = self._compact_episode_variants(obj)
                compact_item = None
                compact_content = None
                for candidate_content in compact_contents:
                    candidate_item = self._to_item(obj, content=candidate_content)
                    if admits([*delivered, candidate_item], [*delivered_objects, obj], admitted_needs):
                        compact_item = candidate_item
                        compact_content = candidate_content
                        break
                if compact_item is not None and compact_content is not None:
                    delivered.append(compact_item)
                    delivered_objects.append(obj)
                    unmet_needs.extend(admitted_needs)
                    try:
                        compact_payload = json.loads(compact_content)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        compact_payload = {}
                    if "goal" not in compact_payload:
                        # The correction is independently supported, but
                        # omitting the superseded goal leaves a bounded
                        # resume slot for a later focused retrieval.
                        unmet_needs.append("resume_state")
                    if "next_step" in json.loads(obj.content) and "next_step" not in compact_payload:
                        unmet_needs.append("resume_state")
                    continue
                budget_drops += 1
                rejection_gaps.append(f"budget_token_cap:{obj.ref}@{obj.revision}")
                rejection_needs.append(f"expandable:{obj.ref}@{obj.revision}")
                rejection_needs.extend(admitted_needs)
                continue
            delivered.append(item)
            delivered_objects.append(obj)
            unmet_needs.extend(admitted_needs)

        # Selection is over: merge the deferred rejection diagnostics and
        # enforce them against the packet that will actually be returned.
        compile_gaps.extend(rejection_gaps)
        unmet_needs.extend(rejection_needs)

        # Re-check the exact final envelope.  Preference order at the cap:
        # keep everything; withhold only the optional diagnostic lookup ref
        # (the process-local record itself stays); repack one retained episode
        # into fewer complete fields; finally drop the lowest-ranked complete
        # unit.  Content, qualifiers and evidence are never sliced.
        include_diagnostic_ref = True
        while True:
            packet, _ = self._assemble(
                context, result, memory_epoch, tuple(delivered), tuple(delivered_objects),
                compile_gaps, unmet_needs, stale_drops, budget_drops,
            )
            if self._envelope_bytes(packet) <= limits.budget_tokens:
                break
            if self._envelope_bytes(packet, include_diagnostic_ref=False) <= limits.budget_tokens:
                include_diagnostic_ref = False
                break
            if not delivered:
                # Nothing left to trade; ``_bounded_packet`` applies the
                # minimal envelope below.
                break
            repacked = False
            for index in range(len(delivered) - 1, -1, -1):
                candidate_obj = delivered_objects[index]
                if candidate_obj.kind != "episode":
                    continue
                current_content = delivered[index]["content"]
                for compact_content in self._compact_episode_variants(candidate_obj):
                    if compact_content == current_content:
                        continue
                    replacement = self._to_item(candidate_obj, content=compact_content)
                    trial = list(delivered)
                    trial[index] = replacement
                    trial_packet, _ = self._assemble(
                        context, result, memory_epoch, tuple(trial), tuple(delivered_objects),
                        compile_gaps, unmet_needs, stale_drops, budget_drops,
                    )
                    if self._envelope_bytes(trial_packet, include_diagnostic_ref=False) <= limits.budget_tokens:
                        delivered[index] = replacement
                        if "goal" not in json.loads(compact_content) or "next_step" not in json.loads(compact_content):
                            unmet_needs.append("resume_state")
                        repacked = True
                        break
                if repacked:
                    break
            if repacked:
                continue
            delivered.pop()
            dropped_obj = delivered_objects.pop()
            budget_drops += 1
            compile_gaps.append(f"budget_packet_cap:{dropped_obj.ref}@{dropped_obj.revision}")
            unmet_needs.append(f"expandable:{dropped_obj.ref}@{dropped_obj.revision}")

        if delivered and self._remaining_ms(context) == 0:
            compile_gaps.append("deadline_exceeded_compile")
            stale_drops += len(delivered)
            delivered.clear()
            delivered_objects.clear()

        packet = self._finalize(
            context,
            result,
            limits,
            memory_epoch,
            tuple(delivered),
            tuple(delivered_objects),
            compile_gaps,
            unmet_needs,
            stale_drops,
            budget_drops,
            retrieval_gaps,
            started,
            include_diagnostic_ref=include_diagnostic_ref,
        )
        return self._bounded_packet(packet, limits.budget_tokens)

    @staticmethod
    def _bounded_packet(packet: RecallPacket, budget: int) -> RecallPacket:
        """Keep answer evidence and public budget diagnostics under the cap."""
        if len(canonical_render_json(packet).encode("utf-8")) <= budget:
            return isolate_recall_packet(packet)
        without_diagnostic_ref = dict(packet, diagnostic_ref=None)
        if len(canonical_render_json(without_diagnostic_ref).encode("utf-8")) <= budget:
            return isolate_recall_packet(cast(RecallPacket, without_diagnostic_ref))
        minimal = dict(packet, status="unavailable", memory_epoch=None, items=[],
                       gaps=["budget_packet_cap"], diagnostic_ref=None,
                       answerability="unknown", coverage="unknown", unmet_needs=[])
        if len(canonical_render_json(minimal).encode("utf-8")) > budget:
            raise ContractError("INPUT_INVALID", "budget_tokens")
        return isolate_recall_packet(cast(RecallPacket, minimal))

    def _finalize(
        self,
        context: SearchContext,
        result: RetrievalResult,
        limits: SearchLimits,
        memory_epoch: int | None,
        items: tuple[RecallItem, ...],
        delivered_objects: tuple[RetrievedObject, ...],
        compile_gaps: list[str],
        unmet_needs: list[str],
        stale_drops: int,
        budget_drops: int,
        retrieval_gaps: tuple[str, ...],
        started: float,
        *,
        include_diagnostic_ref: bool = True,
    ) -> RecallPacket:
        packet, raw_gaps = self._assemble(
            context, result, memory_epoch, items, delivered_objects,
            compile_gaps, unmet_needs, stale_drops, budget_drops,
        )
        diagnostic_ref = None
        if self.diagnostics is not None:
            # The process-local record is always written; only the public
            # lookup ref is optional when it would displace answer evidence.
            diagnostic_ref = self.diagnostics.record(
                installation_id=context.trusted_context.binding.installation_id,
                session_id=context.trusted_context.session_id,
                request_id=result.request_id or context.request_id,
                memory_epoch=memory_epoch,
                phase="compile",
                status=packet["status"],
                retrieval_gaps=retrieval_gaps,
                compile_gaps=raw_gaps,
                items_delivered=len(items),
                items_dropped_stale=stale_drops,
                items_dropped_budget=budget_drops,
                elapsed_ms=int((self._clock().monotonic() - started) * 1000),
                deadline_remaining_ms=self._remaining_ms(context),
            )
        packet["diagnostic_ref"] = diagnostic_ref if include_diagnostic_ref else None
        return packet

    def _assemble(
        self,
        context: SearchContext,
        result: RetrievalResult,
        memory_epoch: int | None,
        items: tuple[RecallItem, ...],
        delivered_objects: tuple[RetrievedObject, ...],
        compile_gaps: list[str] | tuple[str, ...],
        unmet_needs: list[str] | tuple[str, ...],
        stale_drops: int,
        budget_drops: int,
    ) -> tuple[RecallPacket, tuple[str, ...]]:
        """Build the exact public packet shape without side effects.

        ``diagnostic_ref`` carries a fixed-width placeholder of the real ref
        so the budget can be measured on the packet that will be returned,
        before the diagnostics record exists.  Returns the packet and the raw
        (pre-collapse) gap tuple the diagnostics record needs.
        """

        raw_gaps = tuple(dict.fromkeys(gap for gap in compile_gaps if gap))
        gaps = self._public_gaps(raw_gaps)
        authority_failed = self._authority_unavailable(raw_gaps)
        critical_read_incomplete = self._critical_read_incomplete(raw_gaps)
        had_candidates = bool(result.items)
        answer_objects = tuple(obj for obj in delivered_objects if not is_background(obj))
        pipeline_unmet = RetrievalPipeline._unmet_needs(context.query, answer_objects)
        raw_unmet = list(unmet_needs)
        if items and not answer_objects:
            raw_unmet.append("query_evidence_missing")
        raw_unmet = list(dict.fromkeys([*raw_unmet, *pipeline_unmet, *result.unmet_needs]))
        merged_unmet = list(self._public_needs(raw_unmet))
        if (authority_failed or critical_read_incomplete) and not items:
            status = "unavailable"
        elif not items:
            if had_candidates and (stale_drops or budget_drops):
                status = "partial"
            else:
                status = "no_match"
        elif gaps or merged_unmet or stale_drops or budget_drops:
            status = "partial"
        else:
            status = "ok"

        if status in {"no_match", "unavailable"} or not items or (items and not answer_objects):
            answerability = "unknown"
        elif merged_unmet or stale_drops or budget_drops or gaps:
            answerability = "partial"
        elif result.answerability_hint == "ambiguous":
            answerability = "ambiguous"
        elif result.answerability_hint == "supported" and not merged_unmet:
            answerability = "supported"
        else:
            answerability = "partial"

        coverage = result.coverage
        if gaps or stale_drops or budget_drops or merged_unmet:
            coverage = "partial"
        elif coverage == "unknown" and items and not gaps:
            coverage = "unknown"

        if status in {"no_match", "unavailable"}:
            packet_epoch: int | None = None
        elif items:
            packet_epoch = memory_epoch if memory_epoch is not None else 0
        else:
            packet_epoch = memory_epoch

        packet: RecallPacket = {
            "protocol_version": "1.1",
            "request_id": (result.request_id or context.request_id)[:100],
            "status": status,
            "memory_epoch": packet_epoch,
            "items": list(items),
            "gaps": list(gaps),
            "diagnostic_ref": _DIAGNOSTIC_REF_PLACEHOLDER,
            "answerability": answerability,
            "coverage": coverage,
            "unmet_needs": merged_unmet,
        }
        return packet, raw_gaps


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
        packet,
        installation_id=installation_id,
        session_id=session_id,
        request_id=packet["request_id"],
    )
