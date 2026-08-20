"""Host surface the recall orchestrator calls. Not a one-function wrapper."""

from __future__ import annotations

from typing import Any, Protocol

from ...models import RecallItem


class RecallSearchHost(Protocol):
    """Production search host. Implemented by ``RecallService``.

    The orchestrator must call these attributes on the live host so
    existing monkeypatches on ``RecallService`` still affect real searches.
    """

    provider: Any

    @property
    def last_rejected_candidates(self) -> list[RecallItem]: ...

    @last_rejected_candidates.setter
    def last_rejected_candidates(self, value: list[RecallItem]) -> None: ...

    @property
    def last_funnel_trace(self) -> dict[str, Any]: ...

    @last_funnel_trace.setter
    def last_funnel_trace(self, value: dict[str, Any]) -> None: ...

    @property
    def last_temporal_query_diagnostics(self) -> dict[str, Any]: ...

    @last_temporal_query_diagnostics.setter
    def last_temporal_query_diagnostics(self, value: dict[str, Any]) -> None: ...

    def safe_recall_item(self, item: RecallItem) -> RecallItem: ...

    def sanitize_recall_window(
        self, ranked: list[RecallItem], *, limit: int
    ) -> list[RecallItem]: ...

    def _filter_recall_lifecycle(self, items: list[RecallItem]) -> list[RecallItem]: ...

    def _trace_stage(
        self, items: list[RecallItem], *, raw_count: int | None = None
    ) -> dict[str, Any]: ...

    def _elapsed_ms(self, started_at: float) -> float: ...

    def _temporal_current_candidates(
        self,
        query: str,
        *,
        limit: int,
        candidate_memory_ids: list[str],
    ) -> tuple[list[RecallItem], frozenset[str]] | None: ...

    def _rrf_scores(
        self,
        lexical_candidates: list[RecallItem],
        vector_candidates: list[RecallItem],
        curated_candidates: list[RecallItem],
    ) -> dict[str, float]: ...

    def _preferred_duplicate(
        self, current: RecallItem, incoming: RecallItem
    ) -> RecallItem: ...

    def final_score(self, meta: dict[str, Any]) -> float: ...

    def _apply_general_policy(self, items: list[RecallItem]) -> list[RecallItem]: ...

    def _entity_graph_scores(
        self, query: str, items: list[RecallItem]
    ) -> dict[str, float]: ...

    def _persisted_relation_evidence(
        self, memory_ids: list[str]
    ) -> dict[str, dict[str, Any]]: ...

    def _fact_freshness_evidence(
        self, memory_ids: list[str]
    ) -> dict[str, dict[str, Any]]: ...

    def _relation_rerank_bonus(self, evidence: dict[str, Any]) -> float: ...

    def _current_state_rank(
        self,
        item: RecallItem,
        meta: dict[str, Any],
        *,
        requested: bool,
        intent_matched: bool,
    ) -> int: ...

    def _config_bool(self, value: Any, default: bool = False) -> bool: ...

    def _entity_scope_mismatch(
        self, query: str, item: RecallItem, meta: dict[str, Any]
    ) -> bool: ...

    def _freshness_weight(self, query: str) -> float: ...

    def _timestamp_value(self, raw: str) -> float: ...

    def _recency_bonus(
        self,
        *,
        base_score: float,
        updated_at: str,
        freshness_weight: float,
        oldest: float,
        span: float,
    ) -> float: ...

    def _temporal_decay_multiplier(self, meta: dict[str, Any], updated_at: str) -> float: ...

    def _temporal_policy(self, meta: dict[str, Any], target: str) -> tuple[str, float]: ...
