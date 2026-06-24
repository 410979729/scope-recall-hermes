from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .gating import query_tokens
from .graph import apply_quality_weight, entity_distance_scores, entity_overlap_bonus, metadata_entities, query_entities as graph_query_entities
from .models import RecallItem
from .scoring import combine_scores, reciprocal_rank_fusion

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
}

_FRESHNESS_BASE_WEIGHT = 0.22
_FRESHNESS_STEP_WEIGHT = 0.1
_FRESHNESS_MAX_WEIGHT = 0.42

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


class RecallService:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.last_rejected_candidates: list[RecallItem] = []

    def search_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        retrieval_cfg = self.provider._retrieval_config or {}
        candidate_pool = max(limit, int(retrieval_cfg.get("candidate_pool") or limit))
        lexical_candidates = self.provider._search_db_memories(query, limit=candidate_pool)
        vector_candidates = self.provider._search_vector_memories(query, limit=candidate_pool)
        curated_candidates = self.provider._search_curated_memories(query)
        rrf_by_id = self._rrf_scores(lexical_candidates, vector_candidates, curated_candidates)
        for item in lexical_candidates + vector_candidates + curated_candidates:
            if item.id in rrf_by_id:
                item.metadata = dict(item.metadata or {})
                item.metadata["rrf_score"] = rrf_by_id[item.id]

        merged: dict[str, RecallItem] = {}
        for item in lexical_candidates + vector_candidates + curated_candidates:
            if item.id.startswith("curated:"):
                item_key = item.id
            else:
                dedup_class = "scratch" if item.target == "general" else "durable"
                item_key = f"{dedup_class}:{self.provider._dedup_key(item.content)}"
            current = merged.get(item_key)
            if current is None:
                merged[item_key] = item
                continue
            incoming = dict(item.metadata or {})
            preferred = self._preferred_duplicate(current, item)
            other = item if preferred is current else current
            meta = dict(preferred.metadata or {})
            for meta_key, value in dict(other.metadata or {}).items():
                meta.setdefault(meta_key, value)
            current_meta = dict(current.metadata or {})
            for meta_key in ("lexical_score", "vector_score", "base_score", "recency_bonus", "rrf_score"):
                meta[meta_key] = max(
                    float(meta.get(meta_key) or 0.0),
                    float(incoming.get(meta_key) or 0.0),
                    float(current_meta.get(meta_key) or 0.0),
                )
            preferred.metadata = meta
            preferred.score = self.final_score(meta)
            merged[item_key] = preferred

        results = list(merged.values())
        results = self._filter_recall_lifecycle(results)
        results = self._apply_general_policy(results)
        entity_graph_scores = self._entity_graph_scores(query, results)
        relation_evidence = self._persisted_relation_evidence([item.id for item in results])
        min_score = float(retrieval_cfg.get("min_score") or self.provider._config_value("min_score", 0.18))
        # Vector-only matches have no lexical evidence, so they must clear a
        # substantially higher bar than the broad vector candidate threshold.
        # This keeps the semantic companion useful for strong hits while
        # preventing mid-confidence neighbor drift from injecting stale topics.
        vector_only_min_score = float(retrieval_cfg.get("vector_only_min_score") or 0.68)
        filtered: list[RecallItem] = []
        rejected: list[RecallItem] = []
        self.last_rejected_candidates = []
        for item in results:
            meta = dict(item.metadata or {})
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
            relation_rerank_bonus = self._relation_rerank_bonus(relation_payload)
            base_score = max(0.0, min(1.0, quality_adjusted_score + entity_overlap + entity_distance_bonus + relation_rerank_bonus))
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
                    "relation_evidence_types": relation_payload.get("types") or [],
                    "relation_evidence_ids": relation_payload.get("ids") or [],
                    "relation_rerank_bonus": relation_rerank_bonus,
                    "relation_rerank_enabled": self._config_bool(retrieval_cfg.get("relation_rerank_enabled"), False),
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
            lexical_score = float(meta.get("lexical_score") or 0.0)
            vector_score = float(meta.get("vector_score") or 0.0)
            if lexical_score <= 0.0 and vector_score > 0.0 and base_score < vector_only_min_score:
                meta["rejected_reason"] = "vector_only_below_min_score"
                item.metadata = meta
                rejected.append(item)
                continue
            if base_score >= min_score:
                filtered.append(item)
            else:
                meta["rejected_reason"] = "below_min_score"
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

        ranked_rejected = sorted(
            rejected,
            key=lambda item: (
                item.score,
                float((item.metadata or {}).get("base_score") or 0.0),
                item.updated_at,
                item.id,
            ),
            reverse=True,
        )
        self.last_rejected_candidates = ranked_rejected

        return sorted(
            filtered,
            key=lambda item: (
                item.score,
                float((item.metadata or {}).get("base_score") or 0.0),
                item.updated_at,
                item.id,
            ),
            reverse=True,
        )[:limit]

    def _config_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _persisted_relation_evidence(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
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

        try:
            lock = getattr(self.provider, "_lock", None)
            if lock is None:
                rows = self.provider._require_conn().execute(
                    f"""
                    SELECT source_memory_id, target_memory_id, relation_type, confidence
                    FROM memory_relations
                    WHERE source_memory_id IN ({placeholders}) OR target_memory_id IN ({placeholders})
                    """,
                    [*ids, *ids],
                ).fetchall()
            else:
                with lock:
                    rows = self.provider._require_conn().execute(
                        f"""
                        SELECT source_memory_id, target_memory_id, relation_type, confidence
                        FROM memory_relations
                        WHERE source_memory_id IN ({placeholders}) OR target_memory_id IN ({placeholders})
                        """,
                        [*ids, *ids],
                    ).fetchall()
        except Exception:
            return {}

        id_set = set(ids)
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

        supersedes_boost = max(0.0, min(0.5, float(retrieval_cfg.get("relation_supersedes_boost") or retrieval_cfg.get("relation_rerank_weight") or 0.04)))
        supports_boost = max(0.0, min(0.5, float(retrieval_cfg.get("relation_supports_boost") or retrieval_cfg.get("relation_rerank_weight") or 0.04)))
        superseded_penalty = max(0.0, min(0.5, float(retrieval_cfg.get("relation_superseded_penalty") or 0.0)))
        contradicts_penalty = max(0.0, min(0.5, float(retrieval_cfg.get("relation_contradicts_penalty") or 0.0)))

        bonus = 0.0
        bonus += supersedes_boost * _confidence_sum(outgoing.get("supersedes"))
        bonus += supports_boost * _confidence_sum(outgoing.get("supports"))
        bonus += supports_boost * _confidence_sum(incoming.get("supports"))
        bonus -= superseded_penalty * _confidence_sum(incoming.get("supersedes"))
        bonus -= contradicts_penalty * (_confidence_sum(outgoing.get("contradicts")) + _confidence_sum(incoming.get("contradicts")))
        return max(-0.5, min(0.5, bonus))

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
            if lifecycle in {"superseded", "obsolete", "rejected", "archived"}:
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
            if bm25 > 0.0 and lexical <= 0.0 and vector <= 0.0:
                base = bm25
            elif lexical > 0.0 and vector <= 0.0 and bm25 <= 0.0:
                base = lexical
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
        return freshness_weight * normalized_recency * relevance_gate

    def _timestamp_value(self, raw: str) -> float:
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return 0.0
