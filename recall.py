"""Recall service that combines curated files, SQLite rows, vector hits, graph evidence, and ranking policy.

Recall is read-oriented: it should explain/filter candidates without mutating memory state."""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any

from .capture_filters import redact_secret_like_text, sanitize_structured_value
from .gating import (
    matched_query_intent_terms,
    query_intent_terms,
    query_requests_current_state,
    query_tokens,
)
from .freshness import (
    CURRENT_STATUSES,
    STALE_STATUSES,
    attach_freshness_metadata,
    memory_freshness_map,
)
from .graph import apply_quality_weight, entity_distance_scores, entity_overlap_bonus, metadata_entities, normalize_entity, query_entities as graph_query_entities
from .lifecycle_policy import ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES, ordinary_recall_lifecycle_visible_sql
from .models import RecallItem
from .recall_pipeline import build_search_plan, final_trace_payload, initial_trace, merge_recall_candidates, rank_recall_items
from .scoring import combine_scores, reciprocal_rank_fusion
from .temporal_query import (
    MAX_PRECEDENCE_MEMORY_IDS,
    query_current_fact_views,
    query_temporal_memory_precedence,
)

_FRESHNESS_HINTS = {
    "current",
    "currently",
    "latest",
    "new",
    "newest",
    "now",
    "recent",
    "recently",
    "today",
    "updated",
    "当前",
    "目前",
    "现在",
    "如今",
    "最新",
}

_FRESHNESS_BASE_WEIGHT = 0.03
_FRESHNESS_STEP_WEIGHT = 0.015
_FRESHNESS_MAX_WEIGHT = 0.06
_FRESHNESS_ABSOLUTE_BONUS_CAP = 0.06
_FRESHNESS_RELATIVE_BONUS_RATIO = 0.12

_TEMPORAL_DURABLE_TYPES = {
    "constraint",
    "decision",
    "environment_fact",
    "fact",
    "factual",
    "memory",
    "ops",
    "ops_procedure",
    "preference",
    "procedure",
    "project",
    "project_fact",
    "resource",
    "user_preference",
    "workflow",
}
_TEMPORAL_EPISODIC_TYPES = {"episodic", "summary"}
_TEMPORAL_TEMPORARY_TYPES = {"scratch", "temporary", "temporary_state", "tool_trace"}
_RECALL_HIDDEN_LIFECYCLE_VALUES = ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES
_RECALL_HIDDEN_LIFECYCLE_TYPES = set(_RECALL_HIDDEN_LIFECYCLE_VALUES)


def _safe_recall_item(item: RecallItem) -> RecallItem:
    """Redact legacy sensitive payloads at the model/tool egress boundary."""

    safe_metadata, _ = sanitize_structured_value(item.metadata or {})
    return RecallItem(
        id=item.id,
        content=redact_secret_like_text(item.content),
        summary=redact_secret_like_text(item.summary),
        source=redact_secret_like_text(item.source),
        target=redact_secret_like_text(item.target),
        score=item.score,
        updated_at=item.updated_at,
        metadata=safe_metadata if isinstance(safe_metadata, dict) else {},
    )

_ENTITY_SCOPE_STOPWORDS = {
    "api",
    "base",
    "how",
    "is",
    "url",
    "uri",
    "current",
    "latest",
    "like",
    "likes",
    "our",
    "prod",
    "response",
    "releases",
    "rollout",
    "style",
    "tell",
    "what",
    "when",
    "where",
    "which",
    "who",
    "deploy",
    "deployment",
    "rollback",
    "run",
    "runbook",
    "command",
    "production",
    "worker",
    "queue",
    "drain",
    "server",
    "service",
    "services",
    "systemctl",
    "memory",
    "scope-recall",
    "scope",
    "recall",
    "use",
}

_CJK_SCOPE_QUERY_SUBJECT_RE = re.compile(
    r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,8})"
    r"(?=(?:现在|目前|当前|最近|在哪里|在哪|哪儿|何处|的|是什么|怎么|如何|是否|有没有))"
)
_CJK_SCOPE_PRONOUNS = {"我们", "你们", "他们", "她们", "它们", "大家", "自己"}


class RecallService:
    """Read-side retrieval service for current-turn recall.

    The service merges curated file memories, SQLite truth rows, vector hits, relation evidence, and ranking policy. It must stay mutation-free so recall cannot accidentally change durable state."""
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.last_rejected_candidates: list[RecallItem] = []
        self.last_funnel_trace: dict[str, Any] = {}
        self.last_temporal_query_diagnostics: dict[str, Any] = {}

    def search_memories(
        self,
        query: str,
        *,
        limit: int,
        recall_mode: str = "advisory",
    ) -> list[RecallItem]:
        """Search accessible memory sources and return ranked recall payloads.

        ``advisory`` keeps stale evidence with explicit warnings and penalties;
        ``strict`` excludes stale and expired rows while preserving rejection
        diagnostics in the funnel trace.
        """
        normalized_recall_mode = str(recall_mode or "advisory").strip().lower()
        if normalized_recall_mode not in {"advisory", "strict"}:
            raise ValueError("recall_mode must be advisory or strict")
        started_at = time.perf_counter()
        # Diagnostics describe one search only; never leak a prior temporal
        # query into a later request where the feature gate is disabled.
        self.last_temporal_query_diagnostics = {}
        retrieval_cfg = self.provider._retrieval_config or {}
        plan = build_search_plan(
            limit=limit,
            retrieval_config=retrieval_cfg,
            vector_config=getattr(self.provider, "_vector_config", {}) or {},
        )
        bounded_limit = plan.bounded_limit
        candidate_pool = plan.candidate_pool
        trace: dict[str, Any] = initial_trace(
            query=query,
            plan=plan,
            accessible_scope_count=len(getattr(self.provider, "_accessible_scope_ids", []) or []),
        )
        trace["recall_mode"] = normalized_recall_mode
        intent_terms = query_intent_terms(query)
        current_state_requested = query_requests_current_state(query)

        stage_start = time.perf_counter()
        raw_lexical_candidates = self.provider._search_db_memories(query, limit=candidate_pool)
        lexical_candidates = self._filter_recall_lifecycle(raw_lexical_candidates)
        trace["filters"]["lifecycle_removed"] += max(0, len(raw_lexical_candidates) - len(lexical_candidates))
        trace["stages"]["lexical"] = self._trace_stage(lexical_candidates, raw_count=len(raw_lexical_candidates))
        trace["timings_ms"]["lexical"] = self._elapsed_ms(stage_start)

        stage_start = time.perf_counter()
        raw_vector_candidates = self.provider._search_vector_memories(query, limit=candidate_pool)
        vector_candidates = self._filter_recall_lifecycle(raw_vector_candidates)
        trace["filters"]["lifecycle_removed"] += max(0, len(raw_vector_candidates) - len(vector_candidates))
        trace["stages"]["vector"] = self._trace_stage(vector_candidates, raw_count=len(raw_vector_candidates))
        trace["timings_ms"]["vector"] = self._elapsed_ms(stage_start)

        stage_start = time.perf_counter()
        curated_candidates = self.provider._search_curated_memories(query)
        trace["stages"]["curated"] = self._trace_stage(curated_candidates)
        trace["timings_ms"]["curated"] = self._elapsed_ms(stage_start)

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
        temporal_payload = self._temporal_current_candidates(
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
            trace["stages"]["temporal_current"] = self._trace_stage(
                temporal_candidates
            )

        rrf_by_id = self._rrf_scores(lexical_candidates, vector_candidates, curated_candidates)
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
        merged = merge_recall_candidates(
            all_candidates,
            content_dedup_key=self.provider._dedup_key,
            preferred_duplicate=self._preferred_duplicate,
            final_score=self.final_score,
        )

        trace["stages"]["merge"] = {
            "input_count": len(all_candidates),
            "output_count": len(merged),
            "deduped_count": max(0, len(all_candidates) - len(merged)),
        }
        results = list(merged.values())
        before_lifecycle = len(results)
        results = self._filter_recall_lifecycle(results)
        trace["filters"]["lifecycle_removed"] += max(0, before_lifecycle - len(results))
        before_general = len(results)
        results = self._apply_general_policy(results)
        trace["filters"]["general_policy_removed"] = max(0, before_general - len(results))
        trace["stages"]["candidate_after_policy"] = self._trace_stage(results)
        entity_graph_scores = self._entity_graph_scores(query, results)
        relation_evidence = self._persisted_relation_evidence([item.id for item in results])
        freshness_evidence = self._fact_freshness_evidence([item.id for item in results])
        trace["stages"]["graph"] = {
            "entity_scored_count": len(entity_graph_scores),
            "relation_evidence_count": sum(int((payload or {}).get("count") or 0) for payload in relation_evidence.values()),
        }
        trace["stages"]["fact_freshness"] = {
            "tracked_count": len(freshness_evidence),
            "needs_live_check_count": sum(1 for payload in freshness_evidence.values() if bool(payload.get("needs_live_check"))),
        }
        min_score = float(retrieval_cfg.get("min_score") or self.provider._config_value("min_score", 0.18))
        # Vector-only matches have no lexical evidence, so they must clear a
        # substantially higher bar than the broad vector candidate threshold.
        # This keeps the semantic companion useful for strong hits while
        # preventing mid-confidence neighbor drift from injecting stale topics.
        vector_only_min_score = float(retrieval_cfg.get("vector_only_min_score") or 0.70)
        filtered: list[RecallItem] = []
        rejected: list[RecallItem] = []
        self.last_rejected_candidates = []
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
                meta["bm25_score"] = raw_bm25_score * 0.25
            meta["intent_terms"] = list(intent_terms)
            meta["intent_matched"] = intent_matched
            meta["matched_intent_terms"] = matched_intent_terms
            meta["current_state_requested"] = current_state_requested
            pre_quality_score = self.final_score(meta)
            metadata_weight = float(retrieval_cfg.get("metadata_weight") or 0.08)
            quality_adjusted_score = apply_quality_weight(
                pre_quality_score,
                meta,
                weight=metadata_weight,
            )
            entity_weight = float(retrieval_cfg.get("entity_weight") or 0.06)
            entity_overlap = entity_overlap_bonus(query, meta, weight=entity_weight)
            entity_distance_score = entity_graph_scores.get(item.id, 0.0)
            entity_distance_weight = float(retrieval_cfg.get("entity_distance_weight", 0.04))
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
            relation_rerank_bonus = self._relation_rerank_bonus(relation_payload)
            base_score = max(0.0, min(1.0, quality_adjusted_score + entity_overlap + entity_distance_bonus + relation_rerank_bonus))
            freshness_payload = freshness_evidence.get(item.id)
            fact_freshness_penalty = attach_freshness_metadata(meta, freshness_payload, config=retrieval_cfg)
            meta["current_state_rank"] = self._current_state_rank(
                item,
                meta,
                requested=current_state_requested,
                intent_matched=intent_matched,
            )
            if fact_freshness_penalty > 0.0:
                base_score *= max(0.0, 1.0 - fact_freshness_penalty)
            decay_multiplier = self._temporal_decay_multiplier(meta, item.updated_at)
            policy_class, policy_weight = self._temporal_policy(meta, item.target)
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
                    "relation_contradiction_mode": contradiction_mode,
                    "relation_contradiction_warning": (
                        "contradictory_relation_evidence_present"
                        if has_contradiction and contradiction_mode == "surface"
                        else ""
                    ),
                    "relation_rerank_bonus": relation_rerank_bonus,
                    "relation_rerank_enabled": self._config_bool(retrieval_cfg.get("relation_rerank_enabled"), False),
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
            if has_contradiction and contradiction_mode == "suppress":
                meta["rejected_reason"] = "relation_contradiction_suppressed"
                trace["filters"]["relation_contradiction_suppressed"] += 1
                item.metadata = meta
                rejected.append(item)
                continue
            lexical_score = float(meta.get("lexical_score") or 0.0)
            vector_score = float(meta.get("vector_score") or 0.0)
            if self._entity_scope_mismatch(query, item, meta):
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

        freshness_weight = self._freshness_weight(query)
        timestamps = [self._timestamp_value(item.updated_at) for item in filtered]
        if freshness_weight > 0.0 and timestamps:
            oldest = min(timestamps)
            newest = max(timestamps)
            span = newest - oldest
            for item in filtered:
                bonus = self._recency_bonus(
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

        ranked_rejected = [
            _safe_recall_item(item) for item in rank_recall_items(rejected)
        ]
        self.last_rejected_candidates = ranked_rejected

        ranked = [_safe_recall_item(item) for item in rank_recall_items(filtered)]
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
        returned = ranked[:bounded_limit]
        trace["stages"]["ranked"] = self._trace_stage(ranked)
        trace["final"] = final_trace_payload(returned=returned, ranked_rejected=ranked_rejected)
        trace["timings_ms"]["total"] = self._elapsed_ms(started_at)
        self.last_funnel_trace = trace
        return returned

    def _temporal_current_candidates(
        self,
        query: str,
        *,
        limit: int,
        candidate_memory_ids: list[str],
    ) -> tuple[list[RecallItem], frozenset[str]] | None:
        raw_config = getattr(self.provider, "_config", {})
        temporal_config = (
            dict(raw_config.get("temporal_queries") or {})
            if isinstance(raw_config, dict)
            else {}
        )
        if not self._config_bool(temporal_config.get("enabled"), False):
            return None
        scopes = [
            str(scope_id)
            for scope_id in (
                getattr(self.provider, "_accessible_scope_ids", []) or []
            )
            if str(scope_id)
        ]
        if not scopes:
            raise RuntimeError("temporal recall requires at least one accessible scope")
        configured_limit = self._positive_int(
            temporal_config.get("current_limit"),
            50,
        )
        bounded_limit = min(200, max(1, min(int(limit), configured_limit)))
        timezone_name = str(temporal_config.get("timezone") or "UTC")
        with self.provider._lock:
            conn = self.provider._require_conn()
            precedence = query_temporal_memory_precedence(
                conn,
                scope_ids=scopes,
                memory_ids=candidate_memory_ids[:MAX_PRECEDENCE_MEMORY_IDS],
                timezone_name=timezone_name,
            )
            query_diagnostics: dict[str, Any] = {}
            views = query_current_fact_views(
                conn,
                scope_ids=scopes,
                query=query,
                valid_at=precedence.semantic_at,
                timezone_name=timezone_name,
                limit=bounded_limit,
                diagnostics=query_diagnostics,
            )
        self.last_temporal_query_diagnostics = dict(query_diagnostics)
        candidates: list[RecallItem] = []
        for view in views:
            lexical_score = min(1.0, max(0.0, float(view.score)))
            candidates.append(
                RecallItem(
                    id=view.memory_id,
                    content=view.content,
                    summary=view.summary or view.content,
                    source=view.source,
                    target=view.target,
                    score=lexical_score,
                    updated_at=view.updated_at,
                    metadata={
                        "lexical_score": lexical_score,
                        "vector_score": 0.0,
                        "scope_id": view.scope_id,
                        "importance": view.confidence,
                        "memory_type": "factual",
                        "temporal_fact_current": True,
                        "temporal_authoritative": True,
                        "temporal_claim_id": view.claim_id,
                        "temporal_fact_key": view.fact_key,
                        "temporal_value": view.value,
                        "temporal_status": view.status,
                        "temporal_valid_from": view.valid_from,
                        "temporal_valid_to": view.valid_to,
                        "temporal_recorded_at": view.recorded_at,
                        "temporal_semantic_at": view.semantic_at,
                        "temporal_evidence_count": view.evidence_count,
                        "temporal_confidence": view.confidence,
                        "temporal_score_explain": dict(view.score_explain),
                        "temporal_candidate_diagnostics": dict(query_diagnostics),
                    },
                )
            )
        return candidates, precedence.suppressed_memory_ids

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        return max(1, parsed)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000.0, 3)

    @staticmethod
    def _trace_stage(items: list[RecallItem], *, raw_count: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "count": len(items),
            "ids": [item.id for item in items[:20]],
        }
        if raw_count is not None:
            payload["raw_count"] = raw_count
        return payload

    def _project_entities(self, text: str) -> set[str]:
        output: set[str] = set()
        for match in re.finditer(r"\bproject\s+([a-z0-9][a-z0-9_-]{1,40})\b", str(text or ""), flags=re.IGNORECASE):
            output.add(f"project:{match.group(1).lower()}")
        return output

    def _scope_entities(self, values: list[str]) -> set[str]:
        output: set[str] = set()
        for value in values:
            normalized = normalize_entity(value)
            if not normalized or normalized in _ENTITY_SCOPE_STOPWORDS:
                continue
            minimum_length = 2 if re.search(r"[\u4e00-\u9fff]", normalized) else 3
            if len(normalized) < minimum_length and not normalized.startswith("project:"):
                continue
            output.add(normalized)
        return output

    def _explicit_query_scope_entities(self, query: str) -> set[str]:
        raw = str(query or "")
        values = [
            match.group(0)
            for match in re.finditer(r"\b[A-Z][A-Za-z0-9_.:/#-]{2,63}\b", raw)
        ]
        values.extend(
            match.group(1)
            for match in re.finditer(r"`([\u4e00-\u9fff]{2,12})`", raw)
        )
        values.extend(
            subject
            for subject in _CJK_SCOPE_QUERY_SUBJECT_RE.findall(raw)
            if subject not in _CJK_SCOPE_PRONOUNS
        )
        return self._scope_entities(values)

    def _explicit_item_scope_entities(
        self,
        item: RecallItem,
    ) -> set[str]:
        content = str(item.content or "")
        values = [
            match.group(0)
            for match in re.finditer(r"\b[A-Z][A-Za-z0-9_.:/#-]{2,63}\b", content)
        ]
        for match in re.finditer(r"[\u4e00-\u9fff]{2,24}", content):
            values.append(match.group(0)[:2])
        return self._scope_entities(values)

    def _entity_scope_mismatch(self, query: str, item: RecallItem, meta: dict[str, Any]) -> bool:
        retrieval_cfg = self.provider._retrieval_config or {}
        enabled = retrieval_cfg.get("entity_scope_filter_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return False
        # Project-prefixed entities are a hard isolation signal; generic named
        # entities are a conservative fallback. Do not collapse this to token
        # overlap only, or shared terms like API/deploy/rollback will bleed
        # memories across projects.
        query_projects = self._project_entities(query)
        entity_text = "\n".join(str(entity) for entity in metadata_entities(meta))
        item_projects = self._project_entities("\n".join([entity_text, item.content, item.summary]))
        if query_projects:
            if not item_projects:
                return False
            return not bool(query_projects & item_projects)
        query_lower = str(query or "").lower()
        explicit_item_entities = self._explicit_item_scope_entities(item)
        if explicit_item_entities:
            for entity in explicit_item_entities:
                minimum_length = 2 if re.search(r"[\u4e00-\u9fff]", entity) else 3
                if len(entity) >= minimum_length and entity in query_lower:
                    return False
            explicit_query_entities = self._explicit_query_scope_entities(query)
            if explicit_query_entities:
                return not bool(explicit_query_entities & explicit_item_entities)
        explicit_query_entities = self._explicit_query_scope_entities(query)
        item_scope_entities = self._scope_entities(metadata_entities(meta, item.content, item.target))
        if not item_scope_entities:
            return False
        query_lower = str(query or "").lower()
        for entity in item_scope_entities:
            minimum_length = 2 if re.search(r"[\u4e00-\u9fff]", entity) else 3
            if len(entity) >= minimum_length and entity in query_lower:
                explicit_query_entities.add(entity)
        if not explicit_query_entities:
            return False
        return not bool(explicit_query_entities & item_scope_entities)

    @staticmethod
    def _current_state_rank(
        item: RecallItem,
        meta: dict[str, Any],
        *,
        requested: bool,
        intent_matched: bool,
    ) -> int:
        """Rank relevant present-state evidence above plans and old decisions.

        This is a read-side authority tie-break, not a substitute for Fact
        Evolution.  Structured current claims remain strongest; curated profile
        facts can bridge legacy rows that have not yet acquired claims; plan and
        procedure memories stay available but cannot impersonate current state.
        """

        if not requested or not intent_matched:
            return 0
        if bool(meta.get("temporal_fact_current") or meta.get("temporal_authoritative")):
            return 4
        freshness = meta.get("freshness")
        status = str(freshness.get("status") or "").strip().lower() if isinstance(freshness, dict) else ""
        if status == "current":
            return 4
        if item.source == "builtin-curated" and item.target in {"user", "memory"}:
            return 3
        # Untracked legacy rows, including decisions and procedures, remain
        # searchable but do not gain present-state authority merely because
        # they contain an answer-shaped word.
        return 1

    def _config_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _persisted_relation_evidence(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Collect relation evidence for candidate memories from persisted graph companion rows.

        The evidence enriches ranking and explanations, but absence of graph rows must not hide the underlying SQLite memory."""
        ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
        if not ids or not hasattr(self.provider, "_require_conn"):
            return {}
        placeholders = ",".join("?" for _ in ids)
        evidence: dict[str, dict[str, Any]] = {}

        def _payload(memory_id: str) -> dict[str, Any]:
            payload = evidence.setdefault(
                memory_id,
                {
                    "count": 0,
                    "types": set(),
                    "ids": set(),
                    "outgoing": {},
                    "incoming": {},
                },
            )
            return payload

        def _append(memory_id: str, *, direction: str, relation_type: str, related_id: str, confidence: float) -> None:
            payload = _payload(memory_id)
            payload["count"] = int(payload.get("count") or 0) + 1
            payload["types"].add(relation_type)
            payload["ids"].add(related_id)
            direction_bucket = payload[direction]
            relation_rows = direction_bucket.setdefault(relation_type, [])
            relation_rows.append({"id": related_id, "confidence": confidence})

        id_set = set(ids)
        scopes = [str(scope_id) for scope_id in (getattr(self.provider, "_accessible_scope_ids", []) or []) if str(scope_id)]
        scope_clause = ""
        scope_params: list[str] = []
        if scopes:
            scope_placeholders = ",".join("?" for _ in scopes)
            scope_clause = f" AND s.scope_id IN ({scope_placeholders}) AND t.scope_id IN ({scope_placeholders})"
            scope_params = [*scopes, *scopes]
        relation_sql = f"""
                    SELECT r.source_memory_id, r.target_memory_id, r.relation_type, r.confidence
                    FROM memory_relations r
                    JOIN memories s ON s.id = r.source_memory_id
                    JOIN memories t ON t.id = r.target_memory_id
                    WHERE (r.source_memory_id IN ({placeholders}) OR r.target_memory_id IN ({placeholders}))
                      AND {ordinary_recall_lifecycle_visible_sql('s')}
                      AND {ordinary_recall_lifecycle_visible_sql('t')}{scope_clause}
                    """
        relation_params = [*ids, *ids, *scope_params]
        try:
            lock = getattr(self.provider, "_lock", None)
            if lock is None:
                rows = self.provider._require_conn().execute(relation_sql, relation_params).fetchall()
            else:
                with lock:
                    rows = self.provider._require_conn().execute(relation_sql, relation_params).fetchall()
        except Exception:
            return {}

        for row in rows:
            source_id = str(row["source_memory_id"])
            target_id = str(row["target_memory_id"])
            relation_type = str(row["relation_type"] or "").strip().lower()
            if not relation_type:
                continue
            try:
                confidence = max(0.0, min(1.0, float(row["confidence"] or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            if source_id in id_set:
                _append(source_id, direction="outgoing", relation_type=relation_type, related_id=target_id, confidence=confidence)
            if target_id in id_set:
                _append(target_id, direction="incoming", relation_type=relation_type, related_id=source_id, confidence=confidence)

        normalized: dict[str, dict[str, Any]] = {}
        for memory_id, payload in evidence.items():
            normalized[memory_id] = {
                "count": int(payload.get("count") or 0),
                "types": sorted(payload.get("types") or []),
                "ids": sorted(payload.get("ids") or []),
                "outgoing": payload.get("outgoing") or {},
                "incoming": payload.get("incoming") or {},
            }
        return normalized

    def _fact_freshness_evidence(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        retrieval_cfg = self.provider._retrieval_config or {}
        if not self._config_bool(retrieval_cfg.get("fact_freshness_enabled"), True):
            return {}
        if not memory_ids or not hasattr(self.provider, "_require_conn"):
            return {}
        try:
            lock = getattr(self.provider, "_lock", None)
            if lock is None:
                return memory_freshness_map(self.provider._require_conn(), memory_ids)
            with lock:
                return memory_freshness_map(self.provider._require_conn(), memory_ids)
        except Exception:
            return {}

    def _relation_rerank_bonus(self, evidence: dict[str, Any]) -> float:
        retrieval_cfg = self.provider._retrieval_config or {}
        if not evidence or not self._config_bool(retrieval_cfg.get("relation_rerank_enabled"), False):
            return 0.0
        raw_outgoing = evidence.get("outgoing")
        outgoing = raw_outgoing if isinstance(raw_outgoing, dict) else {}
        raw_incoming = evidence.get("incoming")
        incoming = raw_incoming if isinstance(raw_incoming, dict) else {}

        def _confidence_sum(rows: Any) -> float:
            if not isinstance(rows, list):
                return 0.0
            total = 0.0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    total += max(0.0, min(1.0, float(row.get("confidence") or 0.0)))
                except (TypeError, ValueError):
                    continue
            return total

        def _relation_weight(
            primary_key: str,
            *,
            fallback_key: str | None = "relation_rerank_weight",
            default: float = 0.04,
            maximum: float = 0.12,
        ) -> float:
            raw = retrieval_cfg.get(primary_key)
            if raw is None or raw == "":
                raw = retrieval_cfg.get(fallback_key) if fallback_key else None
            if raw is None or raw == "":
                raw = default
            try:
                return max(0.0, min(maximum, float(raw)))
            except (TypeError, ValueError):
                return max(0.0, min(maximum, default))

        supersedes_boost = _relation_weight("relation_supersedes_boost")
        supports_boost = _relation_weight("relation_supports_boost", maximum=0.08)
        same_topic_boost = _relation_weight("relation_same_topic_boost", fallback_key=None, default=0.01, maximum=0.03)
        superseded_penalty = _relation_weight("relation_superseded_penalty")
        contradiction_mode = str(
            retrieval_cfg.get("relation_contradiction_mode") or "surface"
        ).strip().lower()
        if contradiction_mode not in {"surface", "suppress", "penalize"}:
            contradiction_mode = "surface"
        contradicts_penalty = (
            _relation_weight(
                "relation_contradicts_penalty",
                fallback_key=None,
                default=0.0,
            )
            if contradiction_mode == "penalize"
            else 0.0
        )
        invalidated_penalty = _relation_weight(
            "relation_invalidated_penalty",
            fallback_key="relation_invalidates_penalty",
            default=0.04,
        )
        max_bonus = _relation_weight("relation_rerank_max_bonus", fallback_key=None, default=0.08)
        max_penalty = _relation_weight("relation_rerank_max_penalty", fallback_key=None, default=0.08)

        bonus = 0.0
        bonus += supersedes_boost * _confidence_sum(outgoing.get("supersedes"))
        bonus += supports_boost * _confidence_sum(outgoing.get("supports"))
        bonus += supports_boost * _confidence_sum(incoming.get("supports"))
        for typed_relation in ("depends_on", "affects", "owned_by"):
            bonus += supports_boost * _confidence_sum(outgoing.get(typed_relation))
        bonus += same_topic_boost * (
            _confidence_sum(outgoing.get("same_topic")) + _confidence_sum(incoming.get("same_topic"))
        )
        bonus -= superseded_penalty * _confidence_sum(incoming.get("supersedes"))
        bonus -= invalidated_penalty * _confidence_sum(incoming.get("invalidates"))
        bonus -= contradicts_penalty * (_confidence_sum(outgoing.get("contradicts")) + _confidence_sum(incoming.get("contradicts")))
        return max(-max_penalty, min(max_bonus, bonus))

    def _entity_graph_scores(self, query: str, items: list[RecallItem]) -> dict[str, float]:
        query_entity_values = graph_query_entities(query)
        if not query_entity_values or not items:
            return {}
        memory_entities: dict[str, list[str]] = {}
        relations: dict[str, list[str]] = {}
        for item in items:
            entities = metadata_entities(dict(item.metadata or {}), item.content, item.target)
            if not entities:
                continue
            memory_entities[item.id] = entities
            for entity in entities:
                neighbors = relations.setdefault(entity, [])
                for other in entities:
                    if other != entity:
                        neighbors.append(other)
        return entity_distance_scores(query_entity_values, memory_entities, relations, max_depth=2)

    def _preferred_duplicate(self, current: RecallItem, incoming: RecallItem) -> RecallItem:
        if current.target == "general" and incoming.target != "general":
            return incoming
        if incoming.target == "general" and current.target != "general":
            return current
        return current if current.updated_at >= incoming.updated_at else incoming

    def _rrf_scores(
        self,
        lexical_candidates: list[RecallItem],
        vector_candidates: list[RecallItem],
        curated_candidates: list[RecallItem],
    ) -> dict[str, float]:
        retrieval_cfg = self.provider._retrieval_config or {}
        strategy = str(retrieval_cfg.get("fusion_strategy") or "rrf").strip().lower()
        if strategy not in {"rrf", "reciprocal-rank-fusion"}:
            return {}
        ranked_lists: dict[str, list[str]] = {
            "lexical": [item.id for item in lexical_candidates],
            "vector": [item.id for item in vector_candidates],
            "curated": [item.id for item in curated_candidates],
        }
        bm25_ranked = sorted(
            [item for item in lexical_candidates if float((item.metadata or {}).get("bm25_score") or 0.0) > 0.0],
            key=lambda item: float((item.metadata or {}).get("bm25_score") or 0.0),
            reverse=True,
        )
        if bm25_ranked:
            ranked_lists["bm25"] = [item.id for item in bm25_ranked]
        fused = reciprocal_rank_fusion(
            ranked_lists,
            weights={
                "lexical": float(retrieval_cfg.get("rrf_lexical_weight") or 1.0),
                "vector": float(retrieval_cfg.get("rrf_vector_weight") or 1.0),
                "bm25": float(retrieval_cfg.get("rrf_bm25_weight") or 1.0),
                "curated": float(retrieval_cfg.get("rrf_curated_weight") or 1.25),
            },
            k=int(retrieval_cfg.get("rrf_k") or 60),
            min_signals=int(retrieval_cfg.get("rrf_min_signals") or 2),
        )
        if not fused:
            return {}
        max_score = max(score for _, score in fused) or 1.0
        return {item_id: max(0.0, min(1.0, score / max_score)) for item_id, score in fused}

    def _filter_recall_lifecycle(self, items: list[RecallItem]) -> list[RecallItem]:
        output: list[RecallItem] = []
        for item in items:
            lifecycle = str((item.metadata or {}).get("lifecycle") or "").strip().lower()
            if lifecycle in _RECALL_HIDDEN_LIFECYCLE_TYPES and not (
                lifecycle == "scratch" and item.target == "general"
            ):
                continue
            output.append(item)
        return output

    def _apply_general_policy(self, items: list[RecallItem]) -> list[RecallItem]:
        retrieval_cfg = self.provider._retrieval_config or {}
        mode = str(retrieval_cfg.get("include_general") or "same-scope").strip().lower()
        if mode not in {"same-scope", "never", "always"}:
            mode = "same-scope"
        general_weight = max(0.0, min(1.0, float(retrieval_cfg.get("general_weight") or 0.35)))
        output: list[RecallItem] = []
        for item in items:
            if item.target != "general":
                output.append(item)
                continue
            if mode == "never":
                continue
            scope_id = str((item.metadata or {}).get("scope_id") or "")
            if mode == "same-scope" and scope_id and scope_id != str(self.provider._scope_id):
                continue
            min_general_importance = retrieval_cfg.get("general_min_importance")
            if mode != "always" and min_general_importance is not None:
                try:
                    min_importance = float(min_general_importance)
                except (TypeError, ValueError):
                    min_importance = -1.0
                raw_importance = (item.metadata or {}).get("importance")
                try:
                    importance = float(raw_importance) if raw_importance not in (None, "") else 0.0
                except (TypeError, ValueError):
                    importance = 0.0
                if min_importance >= 0.0 and importance < min_importance:
                    continue
            if general_weight < 1.0:
                meta = dict(item.metadata or {})
                for key in ("lexical_score", "vector_score", "bm25_score", "rrf_score"):
                    meta[key] = float(meta.get(key) or 0.0) * general_weight
                meta["general_weight"] = general_weight
                item.metadata = meta
            output.append(item)
        return output

    def final_score(self, meta: dict[str, Any]) -> float:
        retrieval_cfg = self.provider._retrieval_config or {}
        mode = str(retrieval_cfg.get("mode") or "lexical").lower()
        lexical = float(meta.get("lexical_score") or 0.0)
        vector = float(meta.get("vector_score") or 0.0)
        bm25_weight = float(retrieval_cfg.get("bm25_weight", 0.15))
        bm25 = float(meta.get("bm25_score") or 0.0) if bm25_weight > 0.0 else 0.0
        rrf_score = float(meta.get("rrf_score") or 0.0)
        rrf_weight = max(0.0, min(0.6, float(retrieval_cfg.get("rrf_weight", 0.18))))
        if mode == "vector":
            return vector
        if mode == "hybrid":
            if vector <= 0.0 and (lexical > 0.0 or bm25 > 0.0):
                # Lexical score and normalized BM25 are two views of the same
                # modality. Do not depress them with a missing vector weight.
                base = max(lexical, bm25)
            elif vector > 0.0 and lexical <= 0.0 and bm25 <= 0.0:
                base = vector
            else:
                base = combine_scores(
                    {"lexical_score": lexical, "vector_score": vector, "bm25_score": bm25},
                    lexical_weight=float(retrieval_cfg.get("lexical_weight") or 0.45),
                    vector_weight=float(retrieval_cfg.get("vector_weight") or 0.55),
                    bm25_weight=bm25_weight,
                )
            if rrf_score > 0.0 and rrf_weight > 0.0:
                base = (base * (1.0 - rrf_weight)) + (rrf_score * rrf_weight)
            return max(0.0, min(1.0, base))
        return lexical

    def _temporal_policy(self, meta: dict[str, Any], target: str) -> tuple[str, float]:
        retrieval_cfg = self.provider._retrieval_config or {}
        enabled = retrieval_cfg.get("temporal_policy_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return "disabled", 1.0

        def _configured_set(key: str, defaults: set[str]) -> set[str]:
            raw = retrieval_cfg.get(key)
            if isinstance(raw, list):
                values = {str(item).strip().lower() for item in raw if str(item).strip()}
                return values or set(defaults)
            return set(defaults)

        def _weight(class_name: str, default: float) -> float:
            raw_weights = retrieval_cfg.get("temporal_policy_weights")
            configured = raw_weights.get(class_name) if isinstance(raw_weights, dict) else retrieval_cfg.get(f"temporal_policy_{class_name}_weight")
            try:
                value = float(configured if configured is not None else default)
            except (TypeError, ValueError):
                value = default
            return max(0.0, min(1.0, value))

        memory_type = str(meta.get("memory_type") or meta.get("type") or meta.get("category") or "").strip().lower()
        lifecycle = str(meta.get("lifecycle") or meta.get("tier") or "").strip().lower()
        target_value = str(target or "").strip().lower()
        durable_types = _configured_set("temporal_policy_durable_types", _TEMPORAL_DURABLE_TYPES)
        episodic_types = _configured_set("temporal_policy_episodic_types", _TEMPORAL_EPISODIC_TYPES)
        temporary_types = _configured_set("temporal_policy_temporary_types", _TEMPORAL_TEMPORARY_TYPES)

        if memory_type in episodic_types:
            return "episodic", _weight("episodic", 0.8)
        if memory_type in temporary_types or lifecycle in temporary_types or target_value == "general":
            return "temporary", _weight("temporary", 1.0)
        if memory_type in durable_types:
            return "durable_fact", _weight("durable_fact", 0.25)
        return "default", _weight("default", 1.0)

    def _temporal_decay_multiplier(self, meta: dict[str, Any], updated_at: str) -> float:
        retrieval_cfg = self.provider._retrieval_config or {}
        enabled = retrieval_cfg.get("temporal_decay_enabled", False)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return 1.0
        half_life_days = max(1.0, float(retrieval_cfg.get("temporal_decay_half_life_days") or 180.0))
        floor = max(0.0, min(1.0, float(retrieval_cfg.get("temporal_decay_floor") or 0.65)))
        now_ts = datetime.now(timezone.utc).timestamp()
        created_ts = self._timestamp_value(str(meta.get("created_at") or updated_at))
        updated_ts = self._timestamp_value(updated_at)
        if created_ts <= 0.0 and updated_ts <= 0.0:
            return 1.0
        created_age_days = max(0.0, (now_ts - (created_ts or updated_ts)) / 86400.0)
        updated_age_days = max(0.0, (now_ts - (updated_ts or created_ts)) / 86400.0)
        created_decay = 0.5 ** (created_age_days / (half_life_days * 2.0))
        updated_decay = 0.5 ** (updated_age_days / half_life_days)
        multiplier = updated_decay * 0.7 + created_decay * 0.3
        if not math.isfinite(multiplier):
            return 1.0
        return max(floor, min(1.0, multiplier))

    def _freshness_weight(self, query: str) -> float:
        retrieval_cfg = self.provider._retrieval_config or {}
        configured_hints = retrieval_cfg.get("freshness_hints") or sorted(_FRESHNESS_HINTS)
        hints = {str(token).strip().lower() for token in configured_hints if str(token).strip()}
        query_token_set = set(query_tokens(query or ""))
        hint_hits = len(query_token_set & hints)
        if hint_hits <= 0 and query_requests_current_state(query):
            hint_hits = 1
        if hint_hits <= 0:
            return 0.0
        base_weight = float(retrieval_cfg.get("freshness_base_weight") or _FRESHNESS_BASE_WEIGHT)
        step_weight = float(retrieval_cfg.get("freshness_step_weight") or _FRESHNESS_STEP_WEIGHT)
        max_weight = float(retrieval_cfg.get("freshness_max_weight") or _FRESHNESS_MAX_WEIGHT)
        return min(max_weight, base_weight + step_weight * hint_hits)

    def _recency_bonus(
        self,
        *,
        base_score: float,
        updated_at: str,
        freshness_weight: float,
        oldest: float,
        span: float,
    ) -> float:
        if freshness_weight <= 0.0 or base_score <= 0.0 or span <= 0.0:
            return 0.0
        timestamp = self._timestamp_value(updated_at)
        normalized_recency = max(0.0, min(1.0, (timestamp - oldest) / span))
        relevance_gate = max(0.0, min(1.0, base_score / 0.6))
        raw_bonus = freshness_weight * normalized_recency * relevance_gate
        retrieval_cfg = self.provider._retrieval_config or {}
        absolute_cap = max(
            0.0,
            min(
                0.15,
                float(
                    retrieval_cfg.get("freshness_absolute_bonus_cap")
                    or _FRESHNESS_ABSOLUTE_BONUS_CAP
                ),
            ),
        )
        relative_ratio = max(
            0.0,
            min(
                0.25,
                float(
                    retrieval_cfg.get("freshness_relative_bonus_ratio")
                    or _FRESHNESS_RELATIVE_BONUS_RATIO
                ),
            ),
        )
        return min(raw_bonus, absolute_cap, base_score * relative_ratio)

    def _timestamp_value(self, raw: str) -> float:
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return 0.0
