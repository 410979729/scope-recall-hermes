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
            for meta_key in ("lexical_score", "vector_score", "base_score", "recency_bonus"):
                meta[meta_key] = max(
                    float(meta.get(meta_key) or 0.0),
                    float(incoming.get(meta_key) or 0.0),
                    float(current_meta.get(meta_key) or 0.0),
                )
            preferred.metadata = meta
            preferred.score = self.final_score(meta)
            merged[item_key] = preferred

        results = list(merged.values())
        results = self._apply_general_policy(results)
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

    def _preferred_duplicate(self, current: RecallItem, incoming: RecallItem) -> RecallItem:
        if current.target == "general" and incoming.target != "general":
            return incoming
        if incoming.target == "general" and current.target != "general":
            return current
        return current if current.updated_at >= incoming.updated_at else incoming

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
                for key in ("lexical_score", "vector_score"):
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
