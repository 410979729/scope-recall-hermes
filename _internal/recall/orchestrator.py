"""Unique production recall search orchestration.

``RecallService.search_memories`` / ``_search_memories_internal`` parse
arguments and delegate here once. Pipeline and tuning are the reusable
stages; this module is the only place that sequences lexical, vector,
graph, temporal, freshness, relation, CJK, and degrade/diagnostic work.
"""

from __future__ import annotations

import time
from typing import Any

from . import pipeline as recall_pipeline
from .compiler import CandidateSet, CompilerPolicy, compile_recall_packet
from ...freshness import CURRENT_STATUSES, STALE_STATUSES, attach_freshness_metadata
from ...gating import config_bool, matched_query_intent_terms
from ...graph import apply_quality_weight, entity_overlap_bonus
from ...models import RecallItem
from .ports import RecallSearchHost
from .request import RecallSearchRequest
from .tuning import (
    DEFAULT_ENTITY_DISTANCE_WEIGHT,
    DEFAULT_ENTITY_WEIGHT,
    DEFAULT_METADATA_WEIGHT,
    DEFAULT_MIN_SCORE,
    DEFAULT_VECTOR_ONLY_MIN_SCORE,
    INTENT_UNMATCHED_BM25_FACTOR,
)

REQUIRED_TRACE_STAGES = (
    "lexical",
    "vector",
    "curated",
    "temporal_current",
    "rrf",
    "merge",
    "graph",
    "fact_freshness",
    "compiler",
    "ranked",
)
REQUIRED_FILTER_KEYS = (
    "lifecycle_removed",
    "general_policy_removed",
    "temporal_stale_removed",
    "freshness_strict_excluded",
    "relation_contradiction_suppressed",
    "entity_scope_mismatch",
    "vector_only_below_min_score",
    "below_min_score",
)


def _host_callable(host: Any, name: str, fallback: Any) -> Any:
    """Use an injected host hook when present; otherwise the pipeline fallback."""

    fn = getattr(host, name, None)
    return fn if callable(fn) else fallback


def _deterministic_contradiction_loser_ids(
    items: list[RecallItem],
    relation_evidence: dict[str, dict[str, Any]],
) -> set[str]:
    """Choose a deterministic conflict-free winner set for the recalled graph."""

    by_id = {item.id: item for item in items}
    authoritative: dict[str, bool] = {}
    pairs: set[tuple[str, str]] = set()
    for memory_id, item in by_id.items():
        payload = relation_evidence.get(memory_id) or {}
        incoming = payload.get("incoming")
        incoming = incoming if isinstance(incoming, dict) else {}
        authoritative[memory_id] = bool(
            payload.get("authoritative_loser")
            or incoming.get("supersedes")
            or incoming.get("invalidates")
        )
        for direction in ("outgoing", "incoming"):
            grouped = payload.get(direction)
            grouped = grouped if isinstance(grouped, dict) else {}
            rows = grouped.get("contradicts")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                related_id = str(row.get("id") or "")
                if related_id in by_id and related_id != memory_id:
                    pair: tuple[str, str] = (
                        (memory_id, related_id)
                        if memory_id < related_id
                        else (related_id, memory_id)
                    )
                    pairs.add(pair)

    def _priority(memory_id: str) -> tuple[float, str]:
        item = by_id[memory_id]
        try:
            score = float(item.score)
        except (TypeError, ValueError):
            score = 0.0
        return score, str(item.updated_at or "")

    adjacency = {memory_id: set[str]() for memory_id in by_id}
    forced_losers: set[str] = set()
    for left_id, right_id in pairs:
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)
        left_authoritative = authoritative.get(left_id, False)
        right_authoritative = authoritative.get(right_id, False)
        if left_authoritative != right_authoritative:
            forced_losers.add(left_id if left_authoritative else right_id)

    ordered_ids = sorted(memory_id for memory_id in by_id if memory_id not in forced_losers)
    ordered_ids.sort(key=_priority, reverse=True)
    winners: set[str] = set()
    losers = set(forced_losers)
    for memory_id in ordered_ids:
        if adjacency[memory_id] & winners:
            losers.add(memory_id)
        else:
            winners.add(memory_id)
    return losers


def run_search(host: RecallSearchHost, request: RecallSearchRequest) -> list[RecallItem]:
    """Run the single ordinary-search orchestration and return ranked items.

    Diagnostics are written onto ``host`` exactly once. Callers must not
    re-run lexical/vector/curated collection after this returns.
    """

    query = request.query
    limit = request.limit
    query_vector = request.query_vector
    sanitize_output = request.sanitize_output
    normalized_recall_mode = recall_pipeline.normalize_recall_mode(request.recall_mode)
    started_at = time.perf_counter()
    # Diagnostics describe one search only; never leak a prior temporal
    # query into a later request where the feature gate is disabled.
    host.last_temporal_query_diagnostics = {}
    retrieval_cfg = host.provider._retrieval_config or {}
    plan = recall_pipeline.build_search_plan(
        limit=limit,
        retrieval_config=retrieval_cfg,
        vector_config=getattr(host.provider, "_vector_config", {}) or {},
    )
    bounded_limit = plan.bounded_limit
    candidate_pool = plan.candidate_pool
    vector_depth = plan.vector_top_k
    trace: dict[str, Any] = recall_pipeline.initial_trace(
        query=query,
        plan=plan,
        accessible_scope_count=len(getattr(host.provider, "_accessible_scope_ids", []) or []),
    )
    trace["recall_mode"] = normalized_recall_mode
    query_stage = recall_pipeline.normalize_query(query)
    intent_terms = query_stage["intent_terms"]
    current_state_requested = query_stage["current_state_requested"]

    stage_start = time.perf_counter()
    raw_lexical_candidates = host.provider._search_db_memories(query, limit=candidate_pool)
    lexical_candidates = host._filter_recall_lifecycle(raw_lexical_candidates)
    trace["filters"]["lifecycle_removed"] += max(0, len(raw_lexical_candidates) - len(lexical_candidates))
    trace["stages"]["lexical"] = host._trace_stage(lexical_candidates, raw_count=len(raw_lexical_candidates))
    trace["timings_ms"]["lexical"] = host._elapsed_ms(stage_start)

    effective_query_vector = query_vector
    stage_start = time.perf_counter()
    if effective_query_vector is None:
        raw_vector_candidates = host.provider._search_vector_memories(
            query,
            limit=vector_depth,
        )
    else:
        raw_vector_candidates = host.provider._search_vector_memories_with_vector(
            effective_query_vector,
            limit=vector_depth,
        )
    vector_candidates = host._filter_recall_lifecycle(raw_vector_candidates)
    trace["filters"]["lifecycle_removed"] += max(0, len(raw_vector_candidates) - len(vector_candidates))
    trace["stages"]["vector"] = host._trace_stage(vector_candidates, raw_count=len(raw_vector_candidates))
    trace["timings_ms"]["vector"] = host._elapsed_ms(stage_start)

    stage_start = time.perf_counter()
    curated_candidates = host.provider._search_curated_memories(query)
    trace["stages"]["curated"] = host._trace_stage(curated_candidates)
    trace["timings_ms"]["curated"] = host._elapsed_ms(stage_start)

    temporal_candidates: list[RecallItem] = []
    temporal_memory_ids = list(
        dict.fromkeys(
            item.id
            for item in (
                lexical_candidates + vector_candidates + curated_candidates
            )
            if item.id
        )
    )
    temporal_payload = host._temporal_current_candidates(
        query,
        limit=candidate_pool,
        candidate_memory_ids=temporal_memory_ids,
    )
    if temporal_payload is not None:
        temporal_candidates, suppressed_memory_ids = temporal_payload
        current_memory_ids = {item.id for item in temporal_candidates}
        before_temporal_filter = (
            len(lexical_candidates)
            + len(vector_candidates)
            + len(curated_candidates)
        )
        lexical_candidates = [
            item
            for item in lexical_candidates
            if item.id not in suppressed_memory_ids
            and item.id not in current_memory_ids
        ]
        vector_candidates = [
            item
            for item in vector_candidates
            if item.id not in suppressed_memory_ids
            and item.id not in current_memory_ids
        ]
        curated_candidates = [
            item
            for item in curated_candidates
            if item.id not in suppressed_memory_ids
            and item.id not in current_memory_ids
        ]
        after_temporal_filter = (
            len(lexical_candidates)
            + len(vector_candidates)
            + len(curated_candidates)
        )
        trace["filters"]["temporal_stale_removed"] = max(
            0,
            before_temporal_filter - after_temporal_filter,
        )
        trace["stages"]["temporal_current"] = host._trace_stage(
            temporal_candidates
        )

    rrf_by_id = host._rrf_scores(
        lexical_candidates,
        vector_candidates,
        curated_candidates,
    )
    trace["stages"]["rrf"] = {"count": len(rrf_by_id), "ids": sorted(rrf_by_id)[:20]}
    for item in lexical_candidates + vector_candidates + curated_candidates:
        if item.id in rrf_by_id:
            item.metadata = dict(item.metadata or {})
            item.metadata["rrf_score"] = rrf_by_id[item.id]

    all_candidates = (
        lexical_candidates
        + vector_candidates
        + curated_candidates
        + temporal_candidates
    )
    merged = recall_pipeline.merge_recall_candidates(
        all_candidates,
        content_dedup_key=host.provider._dedup_key,
        preferred_duplicate=host._preferred_duplicate,
        final_score=host.final_score,
    )

    trace["stages"]["merge"] = {
        "input_count": len(all_candidates),
        "output_count": len(merged),
        "deduped_count": max(0, len(all_candidates) - len(merged)),
    }
    results = list(merged.values())
    before_lifecycle = len(results)
    results = host._filter_recall_lifecycle(results)
    trace["filters"]["lifecycle_removed"] += max(0, before_lifecycle - len(results))
    before_general = len(results)
    results = host._apply_general_policy(results)
    trace["filters"]["general_policy_removed"] = max(0, before_general - len(results))
    trace["stages"]["candidate_after_policy"] = host._trace_stage(results)
    entity_graph_scores = host._entity_graph_scores(query, results)
    relation_evidence = host._persisted_relation_evidence([item.id for item in results])
    contradiction_loser_ids = _deterministic_contradiction_loser_ids(
        results,
        relation_evidence,
    )
    freshness_evidence = host._fact_freshness_evidence([item.id for item in results])
    trace["stages"]["graph"] = {
        "entity_scored_count": len(entity_graph_scores),
        "relation_evidence_count": sum(int((payload or {}).get("count") or 0) for payload in relation_evidence.values()),
        "generated_signal_disabled_count": sum(
            1
            for payload in relation_evidence.values()
            if bool((payload or {}).get("generated_signal_disabled"))
        ),
    }
    trace["stages"]["fact_freshness"] = {
        "tracked_count": len(freshness_evidence),
        "needs_live_check_count": sum(1 for payload in freshness_evidence.values() if bool(payload.get("needs_live_check"))),
    }
    min_score = float(retrieval_cfg.get("min_score") or host.provider._config_value("min_score", DEFAULT_MIN_SCORE))
    # Vector-only matches have no lexical evidence, so they must clear a
    # substantially higher bar than the broad vector candidate threshold.
    # This keeps the semantic companion useful for strong hits while
    # preventing mid-confidence neighbor drift from injecting stale topics.
    vector_only_min_score = float(retrieval_cfg.get("vector_only_min_score") or DEFAULT_VECTOR_ONLY_MIN_SCORE)
    filtered: list[RecallItem] = []
    rejected: list[RecallItem] = []
    host.last_rejected_candidates = []
    for item in results:
        meta = dict(item.metadata or {})
        raw_bm25_score = float(meta.get("bm25_score") or 0.0)
        matched_intent_terms = matched_query_intent_terms(
            query,
            f"{item.summary}\n{item.content}",
        )
        intent_matched = bool(matched_intent_terms)
        if intent_terms and raw_bm25_score > 0.0 and not intent_matched:
            meta["bm25_pre_intent_score"] = raw_bm25_score
            meta["bm25_score"] = raw_bm25_score * INTENT_UNMATCHED_BM25_FACTOR
        meta["intent_terms"] = list(intent_terms)
        meta["intent_matched"] = intent_matched
        meta["matched_intent_terms"] = matched_intent_terms
        meta["current_state_requested"] = current_state_requested
        pre_quality_score = host.final_score(meta)
        metadata_weight = float(retrieval_cfg.get("metadata_weight") or DEFAULT_METADATA_WEIGHT)
        quality_adjusted_score = apply_quality_weight(
            pre_quality_score,
            meta,
            weight=metadata_weight,
        )
        entity_weight = float(retrieval_cfg.get("entity_weight") or DEFAULT_ENTITY_WEIGHT)
        entity_overlap = entity_overlap_bonus(query, meta, weight=entity_weight)
        entity_distance_score = entity_graph_scores.get(item.id, 0.0)
        entity_distance_weight = float(retrieval_cfg.get("entity_distance_weight", DEFAULT_ENTITY_DISTANCE_WEIGHT))
        entity_distance_bonus = entity_distance_score * entity_distance_weight
        relation_payload = relation_evidence.get(item.id, {})
        relation_types = [
            str(value).strip().lower()
            for value in (relation_payload.get("types") or [])
            if str(value).strip()
        ]
        contradiction_mode = str(
            retrieval_cfg.get("relation_contradiction_mode") or "surface"
        ).strip().lower()
        if contradiction_mode not in {"surface", "suppress", "penalize"}:
            contradiction_mode = "surface"
        has_contradiction = "contradicts" in relation_types
        incoming_relations = relation_payload.get("incoming")
        incoming_relations = (
            incoming_relations if isinstance(incoming_relations, dict) else {}
        )
        contradiction_related_ids: set[str] = set()
        for grouped_relations in (
            relation_payload.get("outgoing"),
            relation_payload.get("incoming"),
        ):
            if not isinstance(grouped_relations, dict):
                continue
            contradiction_rows = grouped_relations.get("contradicts")
            if not isinstance(contradiction_rows, list):
                continue
            for contradiction_row in contradiction_rows:
                if not isinstance(contradiction_row, dict):
                    continue
                related_id = str(contradiction_row.get("id") or "").strip()
                if related_id and related_id != item.id:
                    contradiction_related_ids.add(related_id)
        authoritative_contradiction_loser = host._config_bool(
            relation_payload.get("authoritative_loser"),
            False,
        ) or bool(
            incoming_relations.get("supersedes")
            or incoming_relations.get("invalidates")
        )
        contradiction_loser = item.id in contradiction_loser_ids
        relation_rerank_bonus = host._relation_rerank_bonus(relation_payload)
        base_score = max(0.0, min(1.0, quality_adjusted_score + entity_overlap + entity_distance_bonus + relation_rerank_bonus))
        freshness_payload = freshness_evidence.get(item.id)
        fact_freshness_penalty = attach_freshness_metadata(meta, freshness_payload, config=retrieval_cfg)
        meta["current_state_rank"] = host._current_state_rank(
            item,
            meta,
            requested=current_state_requested,
            intent_matched=intent_matched,
        )
        if fact_freshness_penalty > 0.0:
            base_score *= max(0.0, 1.0 - fact_freshness_penalty)
        decay_multiplier = host._temporal_decay_multiplier(meta, item.updated_at)
        policy_class, policy_weight = host._temporal_policy(meta, item.target)
        decay_weight = 0.0
        pre_decay_score = base_score
        try:
            existing_recency_bonus = float(meta.get("recency_bonus") or 0.0)
        except (TypeError, ValueError):
            existing_recency_bonus = 0.0
        if decay_multiplier < 1.0:
            base_decay_weight = max(0.0, min(1.0, float(retrieval_cfg.get("temporal_decay_weight") or 0.0)))
            decay_weight = max(0.0, min(1.0, base_decay_weight * policy_weight))
            base_score *= (1.0 - decay_weight) + decay_weight * decay_multiplier
        meta.update(
            {
                "pre_quality_score": pre_quality_score,
                "quality_weight_applied": quality_adjusted_score - pre_quality_score,
                "metadata_weight": metadata_weight,
                "entity_overlap_bonus": entity_overlap,
                "entity_distance_score": entity_distance_score,
                "entity_distance_weight": entity_distance_weight,
                "entity_distance_bonus": entity_distance_bonus,
                "relation_evidence_count": int(relation_payload.get("count") or 0),
                "relation_evidence_types": relation_types,
                "relation_evidence_ids": relation_payload.get("ids") or [],
                "relation_contradiction_ids": sorted(contradiction_related_ids),
                "relation_signal_disabled": bool(
                    relation_payload.get("generated_signal_disabled")
                ),
                "relation_signal_reason": str(
                    relation_payload.get("generated_signal_reason") or ""
                ),
                "relation_scope_state": str(
                    relation_payload.get("relation_scope_state") or ""
                ),
                "relation_contradiction_mode": contradiction_mode,
                "relation_contradiction_authoritative_loser": authoritative_contradiction_loser,
                "relation_contradiction_loser": contradiction_loser,
                "relation_contradiction_warning": (
                    "contradictory_relation_evidence_present"
                    if has_contradiction and contradiction_mode == "surface"
                    else ""
                ),
                "relation_rerank_bonus": relation_rerank_bonus,
                "relation_rerank_enabled": host._config_bool(retrieval_cfg.get("relation_rerank_enabled"), False),
                "fact_freshness_penalty": fact_freshness_penalty,
                "pre_decay_score": pre_decay_score,
                "temporal_decay_multiplier": decay_multiplier,
                "temporal_decay_weight": decay_weight,
                "temporal_policy_class": policy_class,
                "temporal_policy_weight": policy_weight,
                "base_score": base_score,
                "recency_bonus": existing_recency_bonus,
                "final_score": base_score,
                "min_score": min_score,
                "vector_only_min_score": vector_only_min_score,
                "rejected_reason": "",
            }
        )
        meta.setdefault("general_weight", 1.0)
        item.metadata = meta
        item.score = base_score
        freshness_status = str(
            meta.get("fact_freshness_status") or "untracked"
        ).strip().lower()
        if normalized_recall_mode == "strict" and (
            freshness_status in STALE_STATUSES or freshness_status == "expired"
        ):
            meta["rejected_reason"] = "freshness_strict_excluded"
            trace["filters"]["freshness_strict_excluded"] += 1
            item.metadata = meta
            rejected.append(item)
            continue
        if (
            has_contradiction
            and contradiction_mode == "suppress"
            and contradiction_loser
        ):
            meta["rejected_reason"] = "relation_contradiction_suppressed"
            trace["filters"]["relation_contradiction_suppressed"] += 1
            item.metadata = meta
            rejected.append(item)
            continue
        lexical_score = float(meta.get("lexical_score") or 0.0)
        vector_score = float(meta.get("vector_score") or 0.0)
        if host._entity_scope_mismatch(query, item, meta):
            meta["rejected_reason"] = "entity_scope_mismatch"
            trace["filters"]["entity_scope_mismatch"] += 1
            item.metadata = meta
            rejected.append(item)
            continue
        if lexical_score <= 0.0 and vector_score > 0.0 and base_score < vector_only_min_score:
            meta["rejected_reason"] = "vector_only_below_min_score"
            trace["filters"]["vector_only_below_min_score"] += 1
            item.metadata = meta
            rejected.append(item)
            continue
        if base_score >= min_score:
            filtered.append(item)
        else:
            meta["rejected_reason"] = "below_min_score"
            trace["filters"]["below_min_score"] += 1
            item.metadata = meta
            rejected.append(item)

    freshness_weight = host._freshness_weight(query)
    timestamps = [host._timestamp_value(item.updated_at) for item in filtered]
    if freshness_weight > 0.0 and timestamps:
        oldest = min(timestamps)
        newest = max(timestamps)
        span = newest - oldest
        for item in filtered:
            bonus = host._recency_bonus(
                base_score=float((item.metadata or {}).get("base_score") or item.score),
                updated_at=item.updated_at,
                freshness_weight=freshness_weight,
                oldest=oldest,
                span=span,
            )
            item.metadata = dict(item.metadata or {})
            item.metadata["recency_bonus"] = bonus
            item.score += bonus
            item.metadata["final_score"] = item.score

    safe_recall_item = _host_callable(host, "safe_recall_item", recall_pipeline.safe_recall_item)
    sanitize_recall_window = _host_callable(
        host, "sanitize_recall_window", recall_pipeline.sanitize_recall_window
    )
    ranked_rejected = [
        safe_recall_item(item) for item in recall_pipeline.rank_recall_items(rejected)
    ]
    host.last_rejected_candidates = ranked_rejected

    ranked = recall_pipeline.rank_recall_items(filtered)
    current_positions = [
        index
        for index, item in enumerate(ranked)
        if str(
            (item.metadata or {}).get("fact_freshness_status") or ""
        ).strip().lower()
        in CURRENT_STATUSES
    ]
    ranking_warnings: list[dict[str, str]] = []
    for index, item in enumerate(ranked):
        status = str(
            (item.metadata or {}).get("fact_freshness_status") or ""
        ).strip().lower()
        if status not in STALE_STATUSES or not any(
            current_index > index for current_index in current_positions
        ):
            continue
        item.metadata = dict(item.metadata or {})
        item.metadata["ranking_warning"] = "stale_result_ranked_above_current"
        ranking_warnings.append(
            {
                "id": item.id,
                "warning": "stale_result_ranked_above_current",
            }
        )
    if ranking_warnings:
        trace["ranking_warnings"] = ranking_warnings
    compiler_started_at = time.perf_counter()
    provider_config = getattr(host.provider, "_config", {})
    raw_compiler_cfg = (
        provider_config.get("recall_compiler", {})
        if isinstance(provider_config, dict)
        else {}
    )
    compiler_cfg = raw_compiler_cfg if isinstance(raw_compiler_cfg, dict) else {}
    current_truth_enabled = config_bool(
        compiler_cfg, "current_truth_enabled", True
    )
    budgeter_enabled = config_bool(compiler_cfg, "budgeter_enabled", False)
    renderer_enabled = config_bool(compiler_cfg, "renderer_enabled", True)
    try:
        token_budget = int(compiler_cfg.get("token_budget") or 320)
    except (TypeError, ValueError):
        token_budget = 320
    try:
        per_item_token_budget = int(
            compiler_cfg.get("per_item_token_budget") or 96
        )
    except (TypeError, ValueError):
        per_item_token_budget = 96

    # Both the compatibility output and the candidate compiler consume this
    # one typed set.  Neither branch can collect lexical/vector candidates.
    candidate_set = CandidateSet.from_items(ranked)
    legacy_packet = compile_recall_packet(
        candidate_set,
        CompilerPolicy(
            limit=bounded_limit,
            token_budget=token_budget,
            per_item_token_budget=per_item_token_budget,
            current_truth_enabled=False,
            evidence_order_enabled=False,
            diversity_enabled=False,
            budgeter_enabled=False,
            annotations_enabled=False,
        ),
    )
    shadow_packet = compile_recall_packet(
        candidate_set,
        CompilerPolicy(
            limit=bounded_limit,
            token_budget=token_budget,
            per_item_token_budget=per_item_token_budget,
        ),
    )
    active_packet = compile_recall_packet(
        candidate_set,
        CompilerPolicy(
            limit=bounded_limit,
            token_budget=token_budget,
            per_item_token_budget=per_item_token_budget,
            current_truth_enabled=current_truth_enabled,
            evidence_order_enabled=renderer_enabled,
            diversity_enabled=renderer_enabled,
            budgeter_enabled=budgeter_enabled,
            annotations_enabled=renderer_enabled,
        ),
    )
    active_items = active_packet.as_recall_items()
    returned = (
        sanitize_recall_window(active_items, limit=bounded_limit)
        if sanitize_output
        else active_items[:bounded_limit]
    )
    legacy_top_five = [item.item.id for item in legacy_packet.items[:5]]
    shadow_top_five = {item.item.id for item in shadow_packet.items[:5]}
    trace["stages"]["compiler"] = {
        **shadow_packet.aggregate_metrics(),
        "active_current_truth_removed": min(
            active_packet.current_truth_removed, 1000
        ),
        "same_candidate_set": (
            legacy_packet.candidate_fingerprint
            == shadow_packet.candidate_fingerprint
            == active_packet.candidate_fingerprint
        ),
        "top5_overlap_count": sum(
            1 for memory_id in legacy_top_five if memory_id in shadow_top_five
        ),
        "current_truth_enabled": current_truth_enabled,
        "budgeter_enabled": budgeter_enabled,
        "renderer_enabled": renderer_enabled,
    }
    trace["timings_ms"]["compiler"] = round(
        (time.perf_counter() - compiler_started_at) * 1000.0, 3
    )
    trace["stages"]["ranked"] = host._trace_stage(ranked)
    trace["final"] = recall_pipeline.final_trace_payload(returned=returned, ranked_rejected=ranked_rejected)
    trace["timings_ms"]["total"] = host._elapsed_ms(started_at)
    host.last_funnel_trace = trace
    return returned
