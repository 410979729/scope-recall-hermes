"""The one production retrieval pipeline used by automatic and tool recall."""
from __future__ import annotations

from dataclasses import replace
from itertools import islice
import json
import re
import time
from typing import Protocol

from ..contracts import ContractError
from .events import lexical_terms
from .recall_policy import RecallPolicy, hard_identifiers, identifiers_compatible, meaningful_query_terms, rrf_score
from .retrieval import CandidateRef, CollectionQuery, RetrievalResult, SearchContext, SearchLimits
from .retrieval_storage import CollectionPage, RetrievalStorage

_CAUSAL_MARKERS = re.compile(r"因为|由于|原因是|because|reason is|due to", re.I)
_REASON_PREDICATES = frozenset({"原因", "理由", "reason", "why", "rationale"})
_COMPARE_MARKERS = ("比较", "对比", "差异", "哪个更", "compare", "versus", " vs ")
_WHY_MARKERS = ("为什么", "为何", "原因", "why", "reason")
_RESUME_MARKERS = ("继续", "接着", "恢复", "resume", "continue")
_AMBIGUOUS_MARKERS = ("哪个", "哪一个", "which", "choose")


class RetrievalClock(Protocol):
    def monotonic(self) -> float: ...


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

    @staticmethod
    def _effective_limits(context: SearchContext) -> SearchLimits:
        if context.mode != "auto":
            return context.limits
        return SearchLimits(
            max_items=min(context.limits.max_items, 6),
            budget_tokens=min(context.limits.budget_tokens, 1200),
            candidate_pool=context.limits.candidate_pool,
            recent_items=context.limits.recent_items,
            relation_hops=context.limits.relation_hops,
            relation_objects=context.limits.relation_objects,
            vector_limit=context.limits.vector_limit,
            followups=context.limits.followups,
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text.encode("utf-8")) // 4)

    @staticmethod
    def _evidence_roots(item: object) -> frozenset[str]:
        roots: set[str] = set()
        if getattr(item, "kind", "") == "event":
            roots.add(str(getattr(item, "ref", "")))
        for ref in getattr(item, "evidence_refs", ()) or ():
            if type(ref) is str and "@" in ref:
                roots.add(ref.split("@", 1)[0])
        return frozenset(roots)

    @staticmethod
    def _comparison_targets(query: str) -> frozenset[str]:
        return hard_identifiers(query)

    @classmethod
    def _comparison_sides(cls, query: str, items: tuple[object, ...]) -> dict[str, frozenset[str]]:
        targets = cls._comparison_targets(query)
        sides: dict[str, frozenset[str]] = {}
        for target in sorted(targets):
            roots: set[str] = set()
            for item in items:
                if target in hard_identifiers(getattr(item, "content", "")):
                    roots.update(cls._evidence_roots(item))
            if roots:
                sides[target] = frozenset(roots)
        return sides

    @staticmethod
    def _primary_root(roots: frozenset[str]) -> str:
        return min(roots) if roots else ""

    @classmethod
    def _comparison_unmet(cls, query: str, items: tuple[object, ...]) -> bool:
        text = query.casefold()
        if not any(marker in text for marker in _COMPARE_MARKERS):
            return False
        targets = cls._comparison_targets(query)
        if len(targets) < 2:
            return True
        sides = cls._comparison_sides(query, items)
        if len(sides) < 2:
            return True
        # One source can explicitly cover both named objects.  Requiring two
        # independent roots would incorrectly turn a direct shared statement
        # into a missing-side result.
        if any(targets <= hard_identifiers(getattr(item, "content", "")) for item in items):
            return False
        primary_roots = tuple(cls._primary_root(roots) for roots in sides.values() if roots)
        return len(set(primary_roots)) < 2

    @staticmethod
    def _reason_window_supported(content: str, targets: frozenset[str], marker: re.Match[str]) -> bool:
        """Accept a causal phrase only when it names the requested target."""

        # Use the complete sentence/clause so a long qualifier cannot be
        # clipped, while a semicolon-separated reason for another object does
        # not become evidence for this target.
        separators = re.compile(r"[。！？!?;；\n]")
        prior = [match.end() for match in separators.finditer(content, 0, marker.start())]
        following = separators.search(content, marker.end())
        start = prior[-1] if prior else 0
        end = following.start() if following else len(content)
        window = content[start:end]
        if re.search(r"未知|不明|不清楚|未(?:知|记录|说明)|没有证据|无证据|不能确定|无法确定|no evidence|unknown|unclear", window, re.I):
            return False
        if not targets:
            return True
        return bool(targets.intersection(hard_identifiers(window)))

    @classmethod
    def _why_unmet(cls, query: str, items: tuple[object, ...]) -> bool:
        text = query.casefold()
        if not any(marker in text for marker in _WHY_MARKERS):
            return False
        topic_terms = set(meaningful_query_terms(query))
        targets = hard_identifiers(query)
        if not topic_terms:
            return True
        for item in items:
            content = getattr(item, "content", "")
            if not topic_terms.intersection(lexical_terms(content)):
                continue
            payload = None
            for key, value in getattr(item, "metadata", ()):
                if key == "payload_json" and type(value) is str:
                    try:
                        payload = json.loads(value)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
            if isinstance(payload, dict) and str(payload.get("predicate", "")).casefold() in {p.casefold() for p in _REASON_PREDICATES}:
                if not re.search(r"未知|不明|不清楚|未(?:知|记录|说明)|没有证据|无证据|不能确定|无法确定|no evidence|unknown|unclear", content, re.I):
                    return False
            for causal in _CAUSAL_MARKERS.finditer(content):
                if cls._reason_window_supported(content, targets, causal):
                    return False
            for key, value in getattr(item, "metadata", ()):
                if key == "basis" and value in {"direct_report", "observed"}:
                    if any(cls._reason_window_supported(content, targets, causal) for causal in _CAUSAL_MARKERS.finditer(content)):
                        return False
        return True

    @classmethod
    def _resume_unmet(cls, items: tuple[object, ...]) -> bool:
        episodes = [item for item in items if getattr(item, "kind", "") == "episode"]
        if not episodes:
            return True
        for item in episodes:
            gaps_meta = {key: value for key, value in getattr(item, "metadata", ())}
            if "gaps" in gaps_meta:
                try:
                    gaps = json.loads(gaps_meta["gaps"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    gaps = ()
                if any(gap in gaps for gap in ("resume_requires_rebuild", "source_version_changed")):
                    continue
            try:
                resume = json.loads(getattr(item, "content", ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if type(resume) is not dict:
                continue
            goal = resume.get("goal")
            if type(goal) is not dict or not goal.get("text") or not goal.get("evidence_refs"):
                continue
            verified = resume.get("verified_progress") or ()
            open_items = resume.get("open_items") or ()
            has_verified = any(
                isinstance(entry, dict) and entry.get("text") and entry.get("evidence_refs")
                for entry in verified
            )
            has_open_item = any(
                isinstance(entry, dict) and entry.get("text") and entry.get("evidence_refs")
                for entry in open_items
            )
            next_step = resume.get("next_step")
            basis = str(resume.get("next_step_basis") or "").casefold()
            has_grounded_next_step = bool(next_step and basis in {"user_requested", "tool_observation", "observed", "direct_report", "evidence"})
            if has_verified or has_open_item or has_grounded_next_step:
                return False
        return True

    @classmethod
    def _unmet_needs(cls, query: str, items: tuple[object, ...]) -> tuple[str, ...]:
        """Describe bounded evidence slots that P09 cannot fill by searching."""
        text = query.casefold()
        needs: list[str] = []
        if any(marker in text for marker in _COMPARE_MARKERS) and cls._comparison_unmet(query, items):
            needs.append("comparison_second_side")
        if any(marker in text for marker in _WHY_MARKERS) and cls._why_unmet(query, items):
            needs.append("reason_evidence")
        if any(marker in text for marker in _RESUME_MARKERS) and cls._resume_unmet(items):
            needs.append("resume_state")
        return tuple(needs)

    @classmethod
    def _directed_followup_query(cls, context: SearchContext, unmet_needs: tuple[str, ...], items: tuple[object, ...]) -> str | None:
        if not unmet_needs or context.limits.followups <= 0:
            return None
        need = unmet_needs[0]
        if need == "comparison_second_side":
            targets = cls._comparison_targets(context.query)
            if len(targets) < 2:
                return None
            sides = cls._comparison_sides(context.query, items)
            missing = [target for target in sorted(targets) if target not in sides]
            if len(missing) == 1:
                return missing[0]
            if len(missing) >= 2:
                return None
            if len(sides) >= 2 and len({cls._primary_root(roots) for roots in sides.values() if roots}) < 2:
                return None
            return None
        if need == "reason_evidence":
            terms = meaningful_query_terms(context.query)
            if not terms:
                return None
            return f"{' '.join(terms[:3])} 原因"
        return None

    def _vector_candidates(self, context: SearchContext, gaps: list[str], *, budget: dict[str, int] | None = None) -> tuple[CandidateRef, ...]:
        if self.vector_port is None or context.limits.vector_limit == 0:
            gaps.append("vector_unavailable")
            return ()
        request_limit = context.limits.vector_limit
        if budget is not None:
            reserve = budget.get("reserve", {}).get("vector", 0)
            request_limit = min(request_limit, max(0, budget.get("vector", 0) - reserve), max(0, budget.get("total", 0) - budget.get("reserve_total", 0)))
            if request_limit <= 0:
                return ()
        remaining = self._remaining(context)
        if remaining <= 0:
            gaps.append("deadline_exceeded_vector")
            return ()
        try:
            raw = tuple(islice(iter(self.vector_port.search(context, limit=request_limit, remaining_seconds=remaining) or ()), request_limit))
            if budget is not None:
                budget["vector"] -= len(raw)
                budget["total"] -= len(raw)
        except Exception as exc:
            gaps.append("vector_unavailable")
            gaps.append(f"vector_error:{type(exc).__name__}")
            return ()
        result = []
        for rank, item in enumerate(tuple(raw or ()), 1):
            if isinstance(item, CandidateRef):
                candidate = item if item.rank == rank else replace(item, rank=rank)
            else:
                try:
                    candidate = CandidateRef(
                        "event" if not hasattr(item, "kind") else item.kind,
                        item.ref,
                        item.revision,
                        "vector",
                        rank=rank,
                        vector_score=getattr(item, "vector_score", getattr(item, "score", None)),
                        vector_id=getattr(item, "vector_id", None),
                        embedding_space=getattr(item, "embedding_space", None),
                    )
                except (AttributeError, ContractError):
                    gaps.append("vector_candidate_invalid")
                    continue
            admitted, reason = self.policy.vector_admission(candidate)
            if admitted:
                result.append(candidate)
            elif reason == "embedding_space_mismatch":
                gaps.append("vector_old_or_mismatched_space")
            elif reason == "vector_threshold_unconfigured":
                gaps.append("vector_threshold_unconfigured")
            elif reason in {"vector_below_threshold", "vector_id_missing", "vector_score_invalid"}:
                continue
            else:
                gaps.append(f"vector_rejected:{reason}")
        return tuple(result)

    def _admit(self, candidate: CandidateRef, context: SearchContext, gaps: list[str]) -> bool:
        if candidate.kind == "event" and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
            return False
        if candidate.source == "vector":
            return self.policy.vector_admission(candidate)[0]
        if candidate.source == "exact_ref":
            return True
        if candidate.source == "relation":
            return True
        return self.policy.lexical_admission(candidate, context.query, exact=False)[0]

    def _hydrate_admit(self, tx, candidate: CandidateRef, context: SearchContext, gaps: list[str]):
        obj = self.storage_reader.hydrate(tx, candidate, context)
        if obj is None:
            return None
        if candidate.source != "exact_ref" and not identifiers_compatible(context.query, obj.content):
            return None
        return obj

    def _collect(self, tx, context: SearchContext, gaps: list[str], *, seen: set[tuple[str, str, int]] | None = None, budget: dict[str, int] | None = None) -> tuple[CandidateRef, ...]:
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return ()
        raw: list[CandidateRef] = []

        def admitted_fused() -> tuple[CandidateRef, ...]:
            # Deadline exits still pass every collected candidate through the
            # same current-source and channel qualification gate.
            admitted = [candidate for candidate in raw if self._admit(candidate, context, gaps)]
            return self._fuse_candidates(admitted, seen)

        def fetch(channel: str, loader, limit: int) -> tuple[CandidateRef, ...]:
            request_limit = limit
            if budget is not None:
                reserve = budget.get("reserve", {}).get(channel, 0)
                request_limit = min(request_limit, max(0, budget.get(channel, 0) - reserve), max(0, budget.get("total", 0) - budget.get("reserve_total", 0)))
                if request_limit <= 0:
                    return ()
            values = tuple(loader(request_limit))
            if budget is not None:
                budget[channel] -= len(values)
                budget["total"] -= len(values)
            return values

        try:
            if self._remaining(context) <= 0:
                gaps.append("deadline_exceeded_collect")
                return ()
            raw.extend(fetch("exact", lambda limit: self.storage_reader.exact(tx, context, limit=limit), context.limits.candidate_pool))
            if self._remaining(context) <= 0:
                gaps.append("deadline_exceeded_collect")
                return admitted_fused()
            raw.extend(fetch("lexical", lambda limit: self.storage_reader.lexical(tx, context, limit=limit), context.limits.candidate_pool))
            if self._remaining(context) <= 0:
                gaps.append("deadline_exceeded_collect")
                return admitted_fused()
            raw.extend(fetch("recent", lambda limit: self.storage_reader.recent(tx, context, limit=limit), context.limits.recent_items))
        except ContractError:
            raise
        except Exception as exc:
            gaps.append(f"sqlite_candidate_error:{type(exc).__name__}")
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return admitted_fused()
        raw.extend(self._vector_candidates(context, gaps, budget=budget))
        return admitted_fused()

    def _fuse_candidates(self, admitted: list[CandidateRef], seen: set[tuple[str, str, int]] | None) -> tuple[CandidateRef, ...]:
        by_key: dict[tuple[str, str, int], list[CandidateRef]] = {}
        for candidate in admitted:
            if seen is not None and candidate.key in seen:
                continue
            by_key.setdefault(candidate.key, []).append(candidate)
        seeds: list[CandidateRef] = []
        for key, signals in sorted(by_key.items()):
            representative = min(signals, key=lambda item: (item.rank, item.source))
            fusion = rrf_score((item.rank for item in signals), k=self.policy.rrf_k)
            seeds.append(replace(representative, fusion_score=fusion))
        return tuple(seeds)

    def _expand(self, tx, context: SearchContext, seeds: tuple[CandidateRef, ...], gaps: list[str]) -> tuple[CandidateRef, ...]:
        if context.limits.relation_hops == 0 or context.limits.relation_objects == 0:
            return seeds
        all_candidates = list(seeds)
        seen = {candidate.key for candidate in seeds}
        frontier = list(seeds)
        inspected = 0
        for _hop in range(context.limits.relation_hops):
            next_frontier: list[CandidateRef] = []
            for seed in sorted(frontier, key=lambda item: item.key):
                if inspected >= context.limits.relation_objects:
                    gaps.append("relation_bound_reached")
                    return tuple(all_candidates)
                if self._remaining(context) <= 0:
                    gaps.append("deadline_exceeded_relation")
                    return tuple(all_candidates)
                for candidate in self.storage_reader.related(tx, seed, limit=context.limits.relation_objects - inspected):
                    inspected += 1
                    bound_reached = inspected >= context.limits.relation_objects
                    if self._remaining(context) <= 0:
                        gaps.append("deadline_exceeded_relation")
                        return tuple(all_candidates)
                    if candidate.key in seen:
                        if bound_reached:
                            gaps.append("relation_bound_reached")
                            return tuple(all_candidates)
                        continue
                    seen.add(candidate.key)
                    if candidate.kind == "event" and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
                        continue
                    obj = self._hydrate_admit(tx, candidate, context, gaps)
                    if obj is None:
                        if bound_reached:
                            gaps.append("relation_bound_reached")
                            return tuple(all_candidates)
                        continue
                    candidate = replace(candidate, fusion_score=rrf_score((candidate.rank + 1,), k=self.policy.rrf_k))
                    all_candidates.append(candidate)
                    next_frontier.append(candidate)
                    if bound_reached:
                        gaps.append("relation_bound_reached")
                        return tuple(all_candidates)
            frontier = next_frontier
            if not frontier:
                break
        return tuple(all_candidates)

    def _rank_hydrated(self, hydrated: list[tuple[CandidateRef, object]]) -> list[tuple[CandidateRef, object]]:
        return sorted(
            hydrated,
            key=lambda pair: (
                -pair[0].fusion_score,
                -(pair[0].vector_score if pair[0].vector_score is not None else -1.0),
                pair[0].kind,
                pair[0].ref,
                pair[0].revision,
            ),
        )

    def _apply_budget(self, ranked: list[tuple[CandidateRef, object]], limits: SearchLimits) -> list[tuple[CandidateRef, object]]:
        kept: list[tuple[CandidateRef, object]] = []
        total_tokens = 0
        for candidate, item in ranked:
            if len(kept) >= limits.max_items:
                break
            tokens = self._estimate_tokens(getattr(item, "content", ""))
            if kept and total_tokens + tokens > limits.budget_tokens:
                break
            total_tokens += tokens
            kept.append((candidate, item))
        return kept

    def search(self, context: SearchContext) -> RetrievalResult:
        if not isinstance(context, SearchContext):
            raise ContractError("INPUT_INVALID", "search_context")
        limits = self._effective_limits(context)
        working = replace(context, limits=limits) if limits != context.limits else context
        gaps: list[str] = []
        if self._remaining(working) <= 0:
            return RetrievalResult((), (), None, ("deadline_exceeded",), "unknown", "unknown", 0, 0, request_id=working.request_id)
        try:
            with self.storage.read(working.trusted_context, remaining_seconds=max(self._remaining(working), 0.001)) as tx:
                epoch = self.storage_reader.epoch(tx)
                hydrated: list[tuple[CandidateRef, object]] = []
                seen_keys: set[tuple[str, str, int]] = set()
                seed_count = 0
                rounds = 0
                max_rounds = min(2, 1 + working.limits.followups)
                channel_budget = {
                    "exact": working.limits.candidate_pool,
                    "lexical": working.limits.candidate_pool,
                    "recent": working.limits.recent_items,
                    "vector": working.limits.vector_limit,
                }
                channel_budget["total"] = sum(channel_budget.values())
                follow_query: str | None = None

                while rounds < max_rounds:
                    if self._remaining(working) <= 0:
                        gaps.append("deadline_exceeded_followup" if rounds else "deadline_exceeded")
                        break
                    round_context = replace(working, query=follow_query) if follow_query else working
                    future_rounds = max(0, max_rounds - rounds - 1)
                    round_budget = dict(channel_budget)
                    round_budget["reserve"] = {
                        key: min(future_rounds, channel_budget.get(key, 0))
                        for key in ("exact", "lexical", "recent", "vector")
                    }
                    round_budget["reserve_total"] = sum(round_budget["reserve"].values())
                    seeds = self._collect(tx, round_context, gaps, seen=seen_keys, budget=round_budget)
                    for key in ("exact", "lexical", "recent", "vector", "total"):
                        channel_budget[key] = round_budget[key]
                    seed_count += len(seeds)
                    for candidate in seeds:
                        seen_keys.add(candidate.key)
                    for candidate in seeds:
                        if self._remaining(working) <= 0:
                            gaps.append("deadline_exceeded_hydrate")
                            break
                        if candidate.key in {pair[0].key for pair in hydrated}:
                            continue
                        obj = self._hydrate_admit(tx, candidate, round_context, gaps)
                        if obj is not None:
                            hydrated.append((candidate, obj))
                    rounds += 1
                    if rounds >= max_rounds:
                        break
                    provisional_items = tuple(pair[1] for pair in hydrated)
                    unmet = self._unmet_needs(working.query, provisional_items)
                    if not unmet:
                        break
                    if self._remaining(working) <= 0:
                        gaps.append("deadline_exceeded_followup")
                        break
                    follow_query = self._directed_followup_query(working, unmet, provisional_items)
                    if follow_query is None:
                        break

                valid_seeds = tuple(candidate for candidate, _obj in hydrated)
                expanded = self._expand(tx, working, valid_seeds, gaps)
                already = {candidate.key for candidate, _obj in hydrated}
                for candidate in expanded:
                    if candidate.key in already:
                        continue
                    if self._remaining(working) <= 0:
                        gaps.append("deadline_exceeded_hydrate")
                        break
                    obj = self._hydrate_admit(tx, candidate, working, gaps)
                    if obj is not None:
                        hydrated.append((candidate, obj))
                        already.add(candidate.key)

                ranked = self._apply_budget(self._rank_hydrated(hydrated), limits)
                candidates = tuple(pair[0] for pair in ranked)
                items = tuple(pair[1] for pair in ranked)
                unmet_needs = self._unmet_needs(working.query, items)
                ambiguous = len(items) > 1 and any(marker in working.query.casefold() for marker in _AMBIGUOUS_MARKERS)
                if not items:
                    answerability = "unknown"
                elif unmet_needs:
                    answerability = "partial"
                elif ambiguous:
                    answerability = "ambiguous"
                else:
                    answerability = "supported"
                coverage = "partial" if gaps else "unknown"
                return RetrievalResult(
                    candidates,
                    items,
                    epoch,
                    tuple(dict.fromkeys(gaps)),
                    coverage,
                    answerability,
                    seed_count,
                    len(hydrated),
                    request_id=working.request_id,
                    unmet_needs=unmet_needs,
                )
        except ContractError as exc:
            if exc.code == "DEADLINE_EXCEEDED":
                return RetrievalResult((), (), None, ("deadline_exceeded",), "unknown", "unknown", 0, 0, request_id=working.request_id)
            return RetrievalResult((), (), None, (f"sqlite_unavailable:{exc.code}",), "unknown", "unknown", 0, 0, request_id=working.request_id)
        except Exception as exc:
            return RetrievalResult((), (), None, (f"sqlite_unavailable:{type(exc).__name__}",), "unknown", "unknown", 0, 0, request_id=working.request_id)

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
