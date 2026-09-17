"""The one production retrieval pipeline used by automatic and tool recall."""
from __future__ import annotations

from dataclasses import replace
from itertools import islice
import time
import unicodedata
from typing import Protocol

from ..contracts import ContractError
from .background_context import background_candidates, current_task_candidate
from .coverage import note_truncation
from .duplicate_collapse import DistinctContent, note_duplicates
from .events import lexical_terms
from .recall_budget import estimate_tokens, event_admission_order
from .recall_needs import CHOICE_MARKERS, directed_followup_query, evidence_roots, mentions, unmet_needs
from .recall_policy import RecallPolicy, identifiers_compatible, meaningful_query_terms, rrf_score
from .retrieval import (
    CandidateRef,
    CollectionQuery,
    RetrievalResult,
    RetrievedObject,
    SearchContext,
    SearchLimits,
    effective_limits,
    optional_json,
)
from .retrieval_storage import CollectionPage, RetrievalStorage
from .vector_failure import vector_failure_label

#: Ceiling on the candidates hydrated after the deadline is already gone.
#:
#: An optional channel that overruns its allowance used to empty the packet:
#: the lexical, exact and recent candidates were all in hand, and the hydrate
#: loop abandoned every one of them because the clock was gone.  The floor is
#: the caller's own ``max_items`` rather than a fixed number, because hydrating
#: three when the packet holds eight just moves the emptiness one stage later.
#: This ceiling bounds what a large ``max_items`` can cost once the deadline is
#: gone -- each one is a single bounded storage read, and the overrun is still
#: reported, so nothing is hidden.
MINIMUM_HYDRATION_CAP = 16
_CHANNELS = ("exact", "lexical", "recent", "vector")
#: Vector admission reasons that are reported, and how; the rest are silent.
_VECTOR_REJECTION_GAPS = {
    "embedding_space_mismatch": "vector_old_or_mismatched_space",
    "vector_threshold_unconfigured": "vector_threshold_unconfigured",
}
_VECTOR_SILENT_REJECTIONS = frozenset({"vector_below_threshold", "vector_id_missing", "vector_score_invalid"})


class RetrievalClock(Protocol):
    def monotonic(self) -> float: ...


class ChannelBudget:
    """Candidate slots left per channel across follow-up rounds.

    A round may not spend what later rounds need: ``hold`` reserves one slot
    per remaining round in every channel so a directed follow-up always has
    room, even after rejected-by-dedup work consumed the first round.
    """

    def __init__(self, limits: SearchLimits) -> None:
        self.left = {"exact": limits.candidate_pool, "lexical": limits.candidate_pool,
                     "recent": limits.recent_items, "vector": limits.vector_limit}
        self.total = sum(self.left.values())
        self.reserve = dict.fromkeys(_CHANNELS, 0)

    def hold(self, future_rounds: int) -> None:
        self.reserve = {channel: min(future_rounds, self.left[channel]) for channel in _CHANNELS}

    def allowance(self, channel: str, limit: int) -> int:
        free_total = self.total - sum(self.reserve.values())
        return min(limit, max(0, self.left[channel] - self.reserve[channel]), max(0, free_total))

    def spend(self, channel: str, count: int) -> None:
        self.left[channel] -= count
        self.total -= count


def _empty_result(context: SearchContext, gap: str) -> RetrievalResult:
    return RetrievalResult((), (), None, (gap,), "unknown", "unknown", 0, 0, request_id=context.request_id)


def _statement_text(obj: RetrievedObject) -> str:
    """What a claim asserts, for term matching; other kinds match on content."""
    if obj.kind != "claim":
        return obj.content
    payload = optional_json(obj.content)
    if not isinstance(payload, dict):
        return obj.content
    return " ".join(str(payload.get(key, "")) for key in ("subject", "predicate", "value_text", "conditions"))


class RetrievalPipeline:
    """Collect, hydrate, admit, and rank candidates in one read-only pass."""

    def __init__(self, storage, *, vector_port=None, policy: RecallPolicy | None = None, clock: RetrievalClock | None = None, storage_reader: RetrievalStorage | None = None):
        self.storage = storage
        self.vector_port = vector_port
        self.policy = policy if policy is not None else RecallPolicy(vector_threshold=None)
        self.clock = clock if clock is not None else time
        self.storage_reader = storage_reader if storage_reader is not None else RetrievalStorage(clock=self.clock)
        if hasattr(self.storage_reader, "clock"):
            self.storage_reader.clock = self.clock

    def _remaining(self, context: SearchContext) -> float:
        return context.deadline - self.clock.monotonic()

    # -- candidate channels ---------------------------------------------------

    def _vector_candidates(self, context: SearchContext, gaps: list[str], *, budget: ChannelBudget | None = None) -> tuple[CandidateRef, ...]:
        if self.vector_port is None or context.limits.vector_limit == 0:
            gaps.append("vector_unavailable")
            return ()
        limit = context.limits.vector_limit if budget is None else budget.allowance("vector", context.limits.vector_limit)
        if limit <= 0:
            return ()
        remaining = self._remaining(context)
        if remaining <= 0:
            gaps.append("deadline_exceeded_vector")
            return ()
        # Keep part of the caller's deadline for SQLite hydration and the
        # packet's final authority checks if the optional vector call times out.
        vector_context = replace(context, deadline=context.deadline - remaining * 0.25)
        try:
            raw = tuple(islice(iter(self.vector_port.search(vector_context, limit=limit, remaining_seconds=remaining * 0.75) or ()), limit))
        except Exception as exc:
            # The class alone does not say what went wrong; see core/vector_failure.py.
            gaps.append("vector_unavailable")
            gaps.append(f"vector_error:{vector_failure_label(exc)}")
            return ()
        if budget is not None:
            budget.spend("vector", len(raw))
        admitted = []
        for rank, candidate in enumerate(raw, 1):
            if candidate.rank != rank:
                candidate = replace(candidate, rank=rank)
            accepted, reason = self.policy.vector_admission(candidate)
            if accepted:
                admitted.append(candidate)
                continue
            gap = _VECTOR_REJECTION_GAPS.get(reason)
            if gap is None and reason not in _VECTOR_SILENT_REJECTIONS:
                gap = f"vector_rejected:{reason}"
            if gap is not None:
                gaps.append(gap)
        return tuple(admitted)

    def _admit(self, candidate: CandidateRef, context: SearchContext) -> bool:
        if candidate.kind == "event" and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
            return False
        if candidate.source == "vector":
            return self.policy.vector_admission(candidate)[0]
        if candidate.source in {"exact_ref", "relation"}:
            return True
        return self.policy.lexical_admission(candidate, context.query, exact=False)[0]

    def _fuse_candidates(self, admitted: list[CandidateRef], seen: set[tuple[str, str, int]] | None) -> tuple[CandidateRef, ...]:
        by_key: dict[tuple[str, str, int], list[CandidateRef]] = {}
        for candidate in admitted:
            if seen is not None and candidate.key in seen:
                continue
            by_key.setdefault(candidate.key, []).append(candidate)
        seeds: list[CandidateRef] = []
        for _key, signals in sorted(by_key.items()):
            representative = min(signals, key=lambda item: (item.rank, item.source))
            fusion = rrf_score((item.rank for item in signals), k=self.policy.rrf_k)
            seeds.append(replace(representative, fusion_score=fusion))
        return tuple(seeds)

    def _collect(self, tx, context: SearchContext, gaps: list[str], *, seen: set[tuple[str, str, int]], budget: ChannelBudget) -> tuple[CandidateRef, ...]:
        """One round of exact, lexical, recent and vector candidates, fused by identity."""
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return ()
        raw: list[CandidateRef] = []

        def admitted() -> tuple[CandidateRef, ...]:
            # Deadline exits still pass every collected candidate through the
            # same current-source and channel qualification gate.
            return self._fuse_candidates([candidate for candidate in raw if self._admit(candidate, context)], seen)

        channels = (
            ("exact", self.storage_reader.exact, context.limits.candidate_pool),
            ("lexical", self.storage_reader.lexical, context.limits.candidate_pool),
            ("recent", self.storage_reader.recent, context.limits.recent_items),
        )
        try:
            for channel, loader, limit in channels:
                if self._remaining(context) <= 0:
                    gaps.append("deadline_exceeded_collect")
                    return admitted()
                allowance = budget.allowance(channel, limit)
                if allowance <= 0:
                    continue
                values = tuple(loader(tx, context, limit=allowance))
                budget.spend(channel, len(values))
                raw.extend(values)
        except ContractError:
            raise
        except Exception as exc:
            gaps.append(f"sqlite_candidate_error:{type(exc).__name__}")
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return admitted()
        raw.extend(self._vector_candidates(context, gaps, budget=budget))
        return admitted()

    # -- hydration and admission ----------------------------------------------

    def _hydrate_admit(self, tx, candidate: CandidateRef, context: SearchContext, *, original_query: str | None = None) -> RetrievedObject | None:
        obj = self.storage_reader.hydrate(tx, candidate, context)
        if obj is None:
            return None
        # The current message already supplies this text. Older copies add no
        # information to automatic context and can crowd out its actual evidence.
        if context.mode == "auto" and obj.kind == "event":
            query = context.query if original_query is None else original_query
            if unicodedata.normalize("NFKC", obj.content).strip() == unicodedata.normalize("NFKC", query).strip():
                return None
        if candidate.source != "exact_ref" and not identifiers_compatible(context.query, obj.content):
            return None
        return obj

    def _hydrate_all(self, tx, hydrated: list[tuple[CandidateRef, RetrievedObject]], candidates, context: SearchContext,
                     gaps: list[str], *, floor: int, original_query: str | None = None) -> None:
        """Hydrate in rank order; once the deadline is gone, only up to ``floor`` items."""
        known = {candidate.key for candidate, _obj in hydrated}
        for candidate in candidates:
            if self._remaining(context) <= 0 and len(hydrated) >= floor:
                gaps.append("deadline_exceeded_hydrate")
                break
            if candidate.key in known:
                continue
            obj = self._hydrate_admit(tx, candidate, context, original_query=original_query)
            if obj is not None:
                hydrated.append((candidate, obj))
                known.add(candidate.key)

    def _expand(self, tx, context: SearchContext, seeds: tuple[CandidateRef, ...], gaps: list[str]) -> tuple[CandidateRef, ...]:
        """Bounded relation hops out of the seeds; every inspected object counts."""
        limits = context.limits
        if limits.relation_hops == 0 or limits.relation_objects == 0:
            return seeds
        all_candidates = list(seeds)
        seen = {candidate.key for candidate in seeds}
        frontier = list(seeds)
        inspected = 0

        def stopped(gap: str) -> tuple[CandidateRef, ...]:
            gaps.append(gap)
            return tuple(all_candidates)

        for _hop in range(limits.relation_hops):
            next_frontier: list[CandidateRef] = []
            for seed in sorted(frontier, key=lambda item: item.key):
                if inspected >= limits.relation_objects:
                    return stopped("relation_bound_reached")
                if self._remaining(context) <= 0:
                    return stopped("deadline_exceeded_relation")
                for candidate in self.storage_reader.related(tx, seed, limit=limits.relation_objects - inspected):
                    inspected += 1
                    bound_reached = inspected >= limits.relation_objects
                    if self._remaining(context) <= 0:
                        return stopped("deadline_exceeded_relation")
                    if candidate.key in seen:
                        if bound_reached:
                            return stopped("relation_bound_reached")
                        continue
                    seen.add(candidate.key)
                    if candidate.kind == "event" and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
                        continue
                    if self._hydrate_admit(tx, candidate, context) is None:
                        if bound_reached:
                            return stopped("relation_bound_reached")
                        continue
                    candidate = replace(candidate, fusion_score=rrf_score((candidate.rank + 1,), k=self.policy.rrf_k))
                    all_candidates.append(candidate)
                    next_frontier.append(candidate)
                    if bound_reached:
                        return stopped("relation_bound_reached")
            frontier = next_frontier
            if not frontier:
                break
        return tuple(all_candidates)

    # -- ranking and budget ---------------------------------------------------

    def _rank_hydrated(self, hydrated: list[tuple[CandidateRef, RetrievedObject]], context: SearchContext | None = None) -> list[tuple[CandidateRef, RetrievedObject]]:
        ranked = sorted(
            hydrated,
            key=lambda pair: (
                -pair[0].fusion_score,
                -(pair[0].vector_score if pair[0].vector_score is not None else -1.0),
                pair[0].kind,
                pair[0].ref,
                pair[0].revision,
            ),
        )
        # Only candidates that already passed hydration, scope, time and source
        # admission reach this point; scores never create permission or facts.
        # Explicit historical lookups keep their original ordering.
        if context is None or context.mode in {"history", "as_of"}:
            return ranked
        query_terms = set(meaningful_query_terms(context.query))
        if not query_terms:
            return ranked
        matched = {candidate.key: query_terms.intersection(lexical_terms(_statement_text(obj))) for candidate, obj in ranked}
        selected: list[tuple[CandidateRef, RetrievedObject]] = []
        covered: set[str] = set()
        selected_roots: set[str] = set()

        def score(pair):
            candidate, obj = pair
            hits = matched[candidate.key]
            roots = evidence_roots(obj)
            # A small, bounded diversity bonus favours another part of the
            # question over repeats from one source. RRF remains the base.
            relevance = len(hits) / len(query_terms)
            additional = len(hits - covered) / len(query_terms)
            repeated = bool(roots and roots <= selected_roots and not (hits - covered))
            return (candidate.source == "exact_ref",
                    candidate.fusion_score + .008 * relevance + .008 * additional - .006 * repeated)

        while ranked:
            if self._remaining(context) <= 0:
                selected.extend(ranked)
                break
            pair = ranked.pop(max(range(len(ranked)), key=lambda index: score(ranked[index])))
            selected.append(pair)
            covered.update(matched[pair[0].key])
            selected_roots.update(evidence_roots(pair[1]))
        return selected

    def _apply_budget(self, ranked: list[tuple[CandidateRef, RetrievedObject]], limits: SearchLimits) -> list[tuple[CandidateRef, RetrievedObject]]:
        kept: list[tuple[CandidateRef, RetrievedObject]] = []
        oversized: list[tuple[CandidateRef, RetrievedObject]] = []
        total_tokens = 0
        for candidate, item in event_admission_order(ranked, limits):
            if len(kept) >= limits.max_items:
                break
            tokens = estimate_tokens(getattr(item, "content", ""))
            if tokens > limits.budget_tokens:
                # Never charge an undeliverable item against all later hits.
                oversized.append((candidate, item))
                continue
            if kept and total_tokens + tokens > limits.budget_tokens:
                # One large candidate must not starve smaller useful evidence
                # later in the ranking. The compiler budgets the whole packet.
                continue
            total_tokens += tokens
            kept.append((candidate, item))
        # An item that alone exceeds the whole budget cannot fit in what is
        # left of it, so it is worth returning only when there is nothing else
        # to show: the expandable hint is a fallback, not a suffix.
        return (kept or oversized)[:limits.max_items]

    # -- the search itself ----------------------------------------------------

    def search(self, context: SearchContext) -> RetrievalResult:
        if not isinstance(context, SearchContext):
            raise ContractError("INPUT_INVALID", "search_context")
        limits = effective_limits(context)
        working = replace(context, limits=limits) if limits != context.limits else context
        if self._remaining(working) <= 0:
            return _empty_result(working, "deadline_exceeded")
        gaps: list[str] = []
        try:
            with self.storage.read(working.trusted_context, remaining_seconds=max(self._remaining(working), 0.001)) as tx:
                epoch = self.storage_reader.epoch(tx)
                hydrated, seed_count = self._collect_rounds(tx, working, gaps)
                self._hydrate_related(tx, working, hydrated, gaps)
                ranked, query_items = self._select(tx, working, hydrated, gaps)
                return self._result(working, epoch, ranked, query_items, gaps, seed_count, len(hydrated))
        except ContractError as exc:
            if exc.code == "DEADLINE_EXCEEDED":
                return _empty_result(working, "deadline_exceeded")
            return _empty_result(working, f"sqlite_unavailable:{exc.code}")
        except Exception as exc:
            return _empty_result(working, f"sqlite_unavailable:{type(exc).__name__}")

    def _collect_rounds(self, tx, working: SearchContext, gaps: list[str]) -> tuple[list[tuple[CandidateRef, RetrievedObject]], int]:
        """The first round plus at most one directed follow-up for an open need."""
        hydrated: list[tuple[CandidateRef, RetrievedObject]] = []
        seen: set[tuple[str, str, int]] = set()
        seed_count = 0
        # Enough to fill the packet the caller asked for, and no more.
        floor = min(working.limits.max_items, MINIMUM_HYDRATION_CAP)
        max_rounds = min(2, 1 + working.limits.followups)
        budget = ChannelBudget(working.limits)
        follow_query: str | None = None
        for round_index in range(max_rounds):
            if self._remaining(working) <= 0:
                gaps.append("deadline_exceeded_followup" if round_index else "deadline_exceeded")
                break
            round_context = replace(working, query=follow_query) if follow_query else working
            budget.hold(max_rounds - round_index - 1)
            seeds = self._collect(tx, round_context, gaps, seen=seen, budget=budget)
            seed_count += len(seeds)
            seen.update(candidate.key for candidate in seeds)
            self._hydrate_all(tx, hydrated, seeds, round_context, gaps, floor=floor, original_query=working.query)
            if round_index + 1 >= max_rounds:
                break
            items = tuple(obj for _candidate, obj in hydrated)
            needs = unmet_needs(working.query, items)
            if not needs:
                break
            if self._remaining(working) <= 0:
                gaps.append("deadline_exceeded_followup")
                break
            follow_query = directed_followup_query(working, needs, items)
            if follow_query is None:
                break
            if "resume_state" in needs:
                task = current_task_candidate(tx, working, self.storage_reader, self.clock)
                if task is not None and task[0].key not in {candidate.key for candidate, _obj in hydrated}:
                    # This task answers an explicit resume request.
                    hydrated.append((replace(task[0], source="relation", lexical_score=1.0), task[1]))
        return hydrated, seed_count

    def _hydrate_related(self, tx, working: SearchContext, hydrated: list[tuple[CandidateRef, RetrievedObject]], gaps: list[str]) -> None:
        seeds = tuple(candidate for candidate, _obj in hydrated)
        known = {candidate.key for candidate in seeds}
        expanded = [candidate for candidate in self._expand(tx, working, seeds, gaps) if candidate.key not in known]
        floor = min(working.limits.max_items, MINIMUM_HYDRATION_CAP)
        self._hydrate_all(tx, hydrated, expanded, working, gaps, floor=floor)

    def _select(self, tx, working: SearchContext, hydrated: list[tuple[CandidateRef, RetrievedObject]], gaps: list[str]):
        """Rank, fold duplicate bodies, fit the item budget, then add background."""
        # Distinct *content*, not distinct rows: candidates are already unique
        # by (kind, ref, revision), but a corpus where a legacy import
        # re-delivered the same bodies under fresh identities let one document
        # hold four of six slots.  One filter spans both passes so background
        # cannot repeat the evidence either.
        distinct = DistinctContent()
        ranked = self._apply_budget(distinct.filtered(self._rank_hydrated(hydrated, working)), working.limits)
        query_items = tuple(obj for _candidate, obj in ranked)
        if working.mode == "auto" and self._remaining(working) > 0:
            background = background_candidates(tx, working, self.storage_reader, self.clock, gaps)
            present = {candidate.key for candidate, _obj in ranked}
            # Query evidence comes first; background shares the final byte
            # budget and can only consume remaining packet slots.
            ranked.extend(pair for pair in background if pair[0].key not in present and distinct.admits(pair[1]))
            note_truncation(gaps, "packet_slots", considered=working.limits.max_items, available=len(ranked))
            ranked = ranked[:working.limits.max_items]
        note_duplicates(gaps, distinct.collapsed)
        return ranked, query_items

    @staticmethod
    def _result(working: SearchContext, epoch: int, ranked, query_items: tuple[RetrievedObject, ...], gaps: list[str],
                seed_count: int, hydrated_count: int) -> RetrievalResult:
        items = tuple(obj for _candidate, obj in ranked)
        needs = unmet_needs(working.query, query_items)
        if items and not query_items:
            needs = (*needs, "query_evidence_missing")
        if not query_items:
            answerability = "unknown"
        elif needs:
            answerability = "partial"
        elif len(query_items) > 1 and mentions(working.query, CHOICE_MARKERS):
            answerability = "ambiguous"
        else:
            answerability = "supported"
        return RetrievalResult(
            tuple(candidate for candidate, _obj in ranked),
            items,
            epoch,
            tuple(dict.fromkeys(gaps)),
            "partial" if gaps else "unknown",
            answerability,
            seed_count,
            hydrated_count,
            request_id=working.request_id,
            unmet_needs=needs,
        )

    def collection(self, context: SearchContext, query: CollectionQuery, cursor=None) -> CollectionPage:
        if not isinstance(context, SearchContext) or not isinstance(query, CollectionQuery):
            raise ContractError("INPUT_INVALID", "collection_request")
        remaining = self._remaining(context)
        if remaining <= 0:
            raise ContractError("DEADLINE_EXCEEDED", "collection")
        with self.storage.read(context.trusted_context, remaining_seconds=remaining) as tx:
            return self.storage_reader.collection(tx, context, query, cursor)


def recall(context: SearchContext, *, storage, vector_port=None, policy: RecallPolicy | None = None, clock=None) -> RetrievalResult:
    """Small functional entry point for adapters that do not retain a service."""
    return RetrievalPipeline(storage, vector_port=vector_port, policy=policy, clock=clock).search(context)
