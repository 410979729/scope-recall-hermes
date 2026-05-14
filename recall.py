from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .gating import query_tokens
from .models import RecallItem
from .scoring import combine_scores

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


class RecallService:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def search_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        retrieval_cfg = self.provider._retrieval_config or {}
        candidate_pool = max(limit, int(retrieval_cfg.get("candidate_pool") or limit))
        lexical_candidates = self.provider._search_db_memories(query, limit=candidate_pool)
        vector_candidates = self.provider._search_vector_memories(query, limit=candidate_pool)
        curated_candidates = self.provider._search_curated_memories(query)

        merged: dict[str, RecallItem] = {}
        for item in lexical_candidates + vector_candidates + curated_candidates:
            key = item.id if item.id.startswith("curated:") else self.provider._dedup_key(item.content)
            current = merged.get(key)
            if current is None:
                merged[key] = item
                continue
            meta = dict(current.metadata or {})
            incoming = dict(item.metadata or {})
            meta["lexical_score"] = max(float(meta.get("lexical_score") or 0.0), float(incoming.get("lexical_score") or 0.0))
            meta["vector_score"] = max(float(meta.get("vector_score") or 0.0), float(incoming.get("vector_score") or 0.0))
            preferred = current if current.updated_at >= item.updated_at else item
            preferred.metadata = meta
            preferred.score = self.final_score(meta)
            merged[key] = preferred

        results = list(merged.values())
        min_score = float(retrieval_cfg.get("min_score") or self.provider._config_value("min_score", 0.18))
        filtered: list[RecallItem] = []
        for item in results:
            meta = dict(item.metadata or {})
            base_score = self.final_score(meta)
            meta["base_score"] = base_score
            item.metadata = meta
            item.score = base_score
            if base_score >= min_score:
                filtered.append(item)

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

    def final_score(self, meta: dict[str, Any]) -> float:
        retrieval_cfg = self.provider._retrieval_config or {}
        mode = str(retrieval_cfg.get("mode") or "lexical").lower()
        lexical = float(meta.get("lexical_score") or 0.0)
        vector = float(meta.get("vector_score") or 0.0)
        if mode == "vector":
            return vector
        if mode == "hybrid":
            if lexical > 0.0 and vector <= 0.0:
                return lexical
            if vector > 0.0 and lexical <= 0.0:
                return vector
            return combine_scores(
                {"lexical_score": lexical, "vector_score": vector},
                lexical_weight=float(retrieval_cfg.get("lexical_weight") or 0.45),
                vector_weight=float(retrieval_cfg.get("vector_weight") or 0.55),
            )
        return lexical

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
