"""Recall service that combines curated files, SQLite rows, vector hits, graph evidence, and ranking policy.

Recall is read-oriented: it should explain/filter candidates without mutating memory state."""

from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from .evidence_retrieval import merge_evidence_rankings
from .gating import (
    query_requests_current_state,
    query_tokens,
)
from .freshness import memory_freshness_map
from .graph import entity_distance_scores, metadata_entities, normalize_entity, query_entities as graph_query_entities
from .lifecycle_policy import ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES, ordinary_recall_lifecycle_visible_sql
from .models import RecallItem
from .recall_pipeline import (
    apply_general_policy,
    filter_recall_lifecycle,
    safe_recall_item as _safe_recall_item,
    trim_recall_budget,
)
from .relation_containment import generated_relation_scope_policy
from .schemas import DEFAULT_EVIDENCE_DIVERSITY_DEPTH, MAX_EVIDENCE_DIVERSITY_DEPTH
from .scoring import combine_scores, reciprocal_rank_fusion
from .temporal_query import (
    MAX_PRECEDENCE_MEMORY_IDS,
    query_current_fact_views,
    query_temporal_memory_precedence,
)
from ._internal.recall import orchestrator as _recall_orchestrator
from ._internal.recall import prefetch as _recall_prefetch
from ._internal.recall.deadline import (
    RequestDeadline,
    acquire_until,
    current_request_deadline,
    resolve_foreground_budget_seconds,
    using_request_deadline,
)
from ._internal.recall.sources import SourceCapabilities
from .recall_source_adapters import bind_source_capabilities
from .recall_sqlite_budget import using_request_busy_timeout
from .sqlite_recovery import is_sqlite_lock_contention
from ._internal.recall.request import RecallSearchRequest
from ._internal.recall.tuning import (
    CJK_SCOPE_PRONOUNS as _CJK_SCOPE_PRONOUNS,
    CJK_SCOPE_QUERY_SUBJECT_RE as _CJK_SCOPE_QUERY_SUBJECT_RE,
    ENTITY_SCOPE_STOPWORDS as _ENTITY_SCOPE_STOPWORDS,
    FRESHNESS_ABSOLUTE_BONUS_CAP as _FRESHNESS_ABSOLUTE_BONUS_CAP,
    FRESHNESS_BASE_WEIGHT as _FRESHNESS_BASE_WEIGHT,
    FRESHNESS_HINTS as _FRESHNESS_HINTS,
    FRESHNESS_MAX_WEIGHT as _FRESHNESS_MAX_WEIGHT,
    FRESHNESS_RELATIVE_BONUS_RATIO as _FRESHNESS_RELATIVE_BONUS_RATIO,
    FRESHNESS_STEP_WEIGHT as _FRESHNESS_STEP_WEIGHT,
    TEMPORAL_DURABLE_TYPES as _TEMPORAL_DURABLE_TYPES,
    TEMPORAL_EPISODIC_TYPES as _TEMPORAL_EPISODIC_TYPES,
    TEMPORAL_TEMPORARY_TYPES as _TEMPORAL_TEMPORARY_TYPES,
    cjk_named_actor_from_referential_identity_clause as _cjk_named_actor_from_referential_identity_clause,
    explicit_cjk_query_entities_corroborated_in_item as _explicit_cjk_query_entities_corroborated_in_item,
    is_short_cjk_name as _is_short_cjk_name,
    is_unquoted_cjk_referential_identity_clause as _is_unquoted_cjk_referential_identity_clause,
    normalize_cjk_scope_subject as _normalize_cjk_scope_subject,
)

_RECALL_HIDDEN_LIFECYCLE_VALUES = ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES
_RECALL_HIDDEN_LIFECYCLE_TYPES = set(_RECALL_HIDDEN_LIFECYCLE_VALUES)
_TRACE_SAFE_TEXT_FIELDS = frozenset(
    {
        "id",
        "query_signal_state",
        "recall_mode",
        "reason_code",
        "source",
        "state",
        "status",
        "strategy",
        "warning",
    }
)
_TRACE_SAFE_TEXT_SEQUENCE_FIELDS = frozenset(
    {
        "ids",
        "reason_codes",
        "returned_ids",
    }
)
_TRACE_DROPPED = object()


def _content_free_trace(value: Any, *, field: str = "") -> Any:
    """Project diagnostics onto a content-free, fail-closed value grammar.

    Trace callers may add new keys over time, so a blacklist cannot guarantee
    that raw query or candidate text stays private.  Numeric/boolean counters
    and nested structures are safe by construction; string values survive only
    for the fixed diagnostic enum/identifier fields declared above.
    """

    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().casefold()
            cleaned = _content_free_trace(item, field=normalized_key)
            if cleaned is not _TRACE_DROPPED:
                projected[str(key)] = cleaned
        return projected
    if isinstance(value, (list, tuple)):
        projected_items: list[Any] = []
        for item in value:
            cleaned = _content_free_trace(item, field=field)
            if cleaned is not _TRACE_DROPPED:
                projected_items.append(cleaned)
        return projected_items
    if isinstance(value, str):
        if field in _TRACE_SAFE_TEXT_FIELDS or field in _TRACE_SAFE_TEXT_SEQUENCE_FIELDS:
            return value
        return _TRACE_DROPPED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _TRACE_DROPPED


def _sanitize_recall_window(
    ranked: list[RecallItem],
    *,
    limit: int,
) -> list[RecallItem]:
    """Sanitize only items that can cross the recall egress boundary."""

    return [_safe_recall_item(item) for item in trim_recall_budget(ranked, limit=limit)]


class RecallService:
    """Read-side retrieval service for current-turn recall.

    The service merges curated file memories, SQLite truth rows, vector hits, relation evidence, and ranking policy. It must stay mutation-free so recall cannot accidentally change durable state."""
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        prefix = f"scope_recall.{id(self)}"
        self._last_rejected_candidates: ContextVar[list[RecallItem] | None] = ContextVar(
            f"{prefix}.rejected", default=None
        )
        self._last_funnel_trace: ContextVar[dict[str, Any] | None] = ContextVar(
            f"{prefix}.funnel_trace", default=None
        )
        self._last_recall_packet: ContextVar[Any] = ContextVar(
            f"{prefix}.recall_packet", default=None
        )
        self._last_evidence_set_trace: ContextVar[dict[str, Any] | None] = ContextVar(
            f"{prefix}.evidence_set_trace", default=None
        )
        self._last_temporal_query_diagnostics: ContextVar[
            dict[str, Any] | None
        ] = ContextVar(f"{prefix}.temporal_diagnostics", default=None)

    def source_capabilities(self) -> SourceCapabilities:
        """Bind Provider/search hooks for this host. Orchestrator sees only the port."""

        return bind_source_capabilities(self)

    @property
    def last_rejected_candidates(self) -> list[RecallItem]:
        return list(self._last_rejected_candidates.get() or [])

    @last_rejected_candidates.setter
    def last_rejected_candidates(self, value: list[RecallItem]) -> None:
        self._last_rejected_candidates.set(list(value or []))

    @property
    def last_funnel_trace(self) -> dict[str, Any]:
        return dict(self._last_funnel_trace.get() or {})

    @last_funnel_trace.setter
    def last_funnel_trace(self, value: dict[str, Any]) -> None:
        sanitized = _content_free_trace(dict(value or {}))
        self._last_funnel_trace.set(sanitized if isinstance(sanitized, dict) else {})

    @property
    def last_recall_packet(self) -> Any:
        """Return the exact active packet from this context's latest search."""

        return self._last_recall_packet.get()

    @last_recall_packet.setter
    def last_recall_packet(self, value: Any) -> None:
        self._last_recall_packet.set(value)

    @property
    def last_evidence_set_trace(self) -> dict[str, Any]:
        return dict(self._last_evidence_set_trace.get() or {})

    @last_evidence_set_trace.setter
    def last_evidence_set_trace(self, value: dict[str, Any]) -> None:
        sanitized = _content_free_trace(dict(value or {}))
        self._last_evidence_set_trace.set(
            sanitized if isinstance(sanitized, dict) else {}
        )

    @property
    def last_temporal_query_diagnostics(self) -> dict[str, Any]:
        return dict(self._last_temporal_query_diagnostics.get() or {})

    @last_temporal_query_diagnostics.setter
    def last_temporal_query_diagnostics(self, value: dict[str, Any]) -> None:
        self._last_temporal_query_diagnostics.set(dict(value or {}))

    def search_evidence_set(
        self,
        query: str,
        *,
        query_variants: list[str],
        limit: int,
        per_query_limit: int | None = None,
        diversity_depth: int = DEFAULT_EVIDENCE_DIVERSITY_DEPTH,
        recall_mode: str = "advisory",
    ) -> list[RecallItem]:
        """Retrieve and fuse a bounded set of explicit query variants.

        Query generation belongs to the caller; Scope Recall only performs
        deterministic, read-only evidence fusion. The ordinary single-query
        path is untouched when no variants are supplied.
        """

        queries: list[str] = []
        seen: set[str] = set()
        for raw_query in [query, *list(query_variants or [])]:
            candidate = str(raw_query or "").strip()
            normalized = candidate.casefold()
            if not candidate or normalized in seen:
                continue
            seen.add(normalized)
            queries.append(candidate[:1000])
            if len(queries) >= 8:
                break
        bounded_limit = max(1, min(50, int(limit or 1)))
        bounded_per_query = max(
            1,
            min(50, int(per_query_limit or bounded_limit)),
        )
        rankings: list[tuple[str, list[RecallItem]]] = []
        query_traces: list[dict[str, Any]] = []
        query_vectors: list[list[float]] = []
        embed_query_variants = getattr(
            self.provider,
            "_embed_query_variants",
            None,
        )
        if callable(embed_query_variants):
            try:
                embedded_raw: Any = embed_query_variants(queries)
                embedded = list(embedded_raw or [])
                if len(embedded) == len(queries):
                    query_vectors = embedded
            except Exception:
                # Batch embedding is an optimization only. The ordinary
                # per-query path remains the compatibility fallback.
                query_vectors = []
        for index, variant in enumerate(queries):
            search_kwargs: dict[str, Any] = {
                "limit": bounded_per_query,
                "recall_mode": recall_mode,
                "sanitize_output": False,
            }
            if query_vectors:
                search_kwargs["query_vector"] = query_vectors[index]
            results = self._search_memories_internal(variant, **search_kwargs)
            rankings.append((variant, results))
            query_traces.append(
                {
                    "query_index": index,
                    "query_length": len(variant),
                    "returned_ids": [item.id for item in results],
                    "funnel_trace": _content_free_trace(
                        dict(self.last_funnel_trace or {})
                    ),
                }
            )
        bounded_diversity_depth = max(
            1,
            min(
                MAX_EVIDENCE_DIVERSITY_DEPTH,
                int(diversity_depth or DEFAULT_EVIDENCE_DIVERSITY_DEPTH),
            ),
        )
        merged_ranked = merge_evidence_rankings(
            rankings,
            limit=bounded_limit,
            diversity_depth=bounded_diversity_depth,
        )
        # Every production ranking above came through the unique orchestrator.
        # Refuse any item whose admission marker was lost or forged during
        # multi-query fusion rather than letting RRF manufacture relevance.
        admission_safe_ranked: list[RecallItem] = []
        admission_egress_rejected = 0
        for item in merged_ranked:
            admission = (item.metadata or {}).get("candidate_admission")
            if isinstance(admission, dict) and bool(admission.get("admitted")):
                admission_safe_ranked.append(item)
            else:
                admission_egress_rejected += 1
        merged_ranked = admission_safe_ranked
        merged = _sanitize_recall_window(merged_ranked, limit=bounded_limit)
        self.last_funnel_trace = (
            dict(query_traces[0]["funnel_trace"] or {})
            if query_traces
            else {}
        )
        self.last_evidence_set_trace = {
            "strategy": "multi_query_rrf_diversity",
            "query_count": len(queries),
            "query_lengths": [len(value) for value in queries],
            "per_query_limit": bounded_per_query,
            "diversity_depth": bounded_diversity_depth,
            "limit": bounded_limit,
            "query_traces": query_traces,
            "returned_ids": [item.id for item in merged],
            "candidate_admission_egress_rejected": admission_egress_rejected,
        }
        return merged

    def search_memories(
        self,
        query: str,
        *,
        limit: int,
        recall_mode: str = "advisory",
    ) -> list[RecallItem]:
        """Search accessible memory sources through the sanitized egress."""

        return self._search_memories_internal(
            query,
            limit=limit,
            recall_mode=recall_mode,
            sanitize_output=True,
        )

    def safe_recall_item(self, item: RecallItem) -> RecallItem:
        """Sanitize one recall item. Honors module-level ``_safe_recall_item`` patches."""

        return _safe_recall_item(item)

    def sanitize_recall_window(
        self, ranked: list[RecallItem], *, limit: int
    ) -> list[RecallItem]:
        """Sanitize the egress window. Honors module-level sanitizer patches."""

        return _sanitize_recall_window(ranked, limit=limit)

    def prefetch_prompt(self, query: str, *, session_id: str = "") -> str:
        """Assemble the current-turn recall plus optional experience packet.

        This is the read-side prefetch owner. Provider keeps a one-line
        Hermes door. Isolation, fail-soft rollback, experience gates,
        zero-write preflight, lock, and packet merge live in the recall
        prefetch module so Provider never calls experience preflight.
        """

        return _recall_prefetch.prefetch_prompt(
            self.provider, query, session_id=session_id
        )

    def _search_memories_internal(
        self,
        query: str,
        *,
        limit: int,
        recall_mode: str = "advisory",
        query_vector: list[float] | None = None,
        sanitize_output: bool = True,
    ) -> list[RecallItem]:
        """Parse once, then delegate to the unique production orchestrator.

        ``advisory`` keeps stale evidence with explicit warnings and penalties;
        ``strict`` excludes stale and expired rows while preserving rejection
        diagnostics in the funnel trace. Collection, ranking, and filter
        counters run only inside ``run_search``.
        """

        request = RecallSearchRequest(
            query=query,
            limit=limit,
            recall_mode=recall_mode,
            query_vector=query_vector,
            sanitize_output=sanitize_output,
        )
        deadline_cm = nullcontext()
        if current_request_deadline() is None:
            if request.deadline_monotonic is not None:
                deadline = RequestDeadline.from_absolute(request.deadline_monotonic)
            else:
                deadline = RequestDeadline.from_budget(
                    resolve_foreground_budget_seconds(
                        getattr(self.provider, "_config", {})
                    )
                )
            deadline_cm = using_request_deadline(deadline)
        with deadline_cm:
            return _recall_orchestrator.run_search(self, request)

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
            with using_request_busy_timeout(conn):
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
        for match in re.finditer(r"\bproject[ \t]+([a-z0-9][a-z0-9_-]{1,40})\b", str(text or ""), flags=re.IGNORECASE):
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

    @staticmethod
    def _query_mentions_scope_entity(query: str, entity: str) -> bool:
        """Match a normalized entity case-insensitively without substring bleed."""

        normalized = str(entity or "").casefold()
        if not normalized:
            return False
        query_text = str(query or "").casefold()
        if re.search(r"[\u4e00-\u9fff]", normalized):
            return normalized in query_text
        return bool(
            re.search(
                rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                query_text,
            )
        )

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
        for subject in _CJK_SCOPE_QUERY_SUBJECT_RE.findall(raw):
            normalized_subject = _normalize_cjk_scope_subject(subject)
            if normalized_subject in _CJK_SCOPE_PRONOUNS:
                continue
            if _is_unquoted_cjk_referential_identity_clause(normalized_subject, raw):
                actor = _cjk_named_actor_from_referential_identity_clause(
                    normalized_subject, raw
                )
                if actor and actor not in _CJK_SCOPE_PRONOUNS:
                    values.append(actor)
                continue
            values.append(normalized_subject)
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
        values.extend(
            match.group(1)
            for match in re.finditer(r"`([\u4e00-\u9fff]{2,12})`", content)
        )
        # Arbitrary CJK prose prefixes are not entity declarations. Deriving a
        # hard scope from the first two characters (for example 当前 from
        # 当前配置) rejects otherwise relevant memories. Explicit backticks and
        # structured/heuristically extracted metadata remain available below.
        return self._scope_entities(values)

    def _entity_scope_mismatch(self, query: str, item: RecallItem, meta: dict[str, Any]) -> bool:
        retrieval_cfg = self.provider._retrieval_config or {}
        enabled = retrieval_cfg.get("entity_scope_filter_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return False

        raw_declared_entities = meta.get("entities")
        if isinstance(raw_declared_entities, list):
            declared_values = [str(value) for value in raw_declared_entities]
        elif raw_declared_entities:
            declared_values = [str(raw_declared_entities)]
        else:
            declared_values = []
        explicit_item_entities = self._explicit_item_scope_entities(item)
        declared_entities = self._scope_entities(declared_values)
        raw_claim = meta.get("claim")
        claim_entities = self._scope_entities(
            [str(raw_claim.get("subject") or "")]
            if isinstance(raw_claim, dict) and raw_claim.get("subject")
            else []
        )
        declared_projects = self._project_entities("\n".join(declared_values))
        if claim_entities:
            structured_entities = claim_entities
        elif declared_projects:
            project_names = {
                project.split(":", 1)[1]
                for project in declared_projects
                if ":" in project
            }
            structured_entities = self._scope_entities(
                [
                    entity
                    for name in sorted(project_names)
                    for entity in (name, f"project {name}")
                ]
            )
        elif len(declared_entities) <= 3:
            # Small lists are explicit tool input. Larger lists are commonly
            # auto-expanded storage metadata, so keep only proper-name evidence
            # also visible in prose instead of treating every keyword as scope.
            structured_entities = declared_entities
        else:
            # The prose proper-name extractor is Latin-oriented, so CJK
            # subjects (小明, 小红 …) vanished from the intersection and junk
            # ASCII keywords became "the subject", which then vetoed queries
            # that named the real CJK subject. Declared short CJK names that
            # appear verbatim in the item's own text are subject evidence.
            item_text = f"{item.content}\n{item.summary}"
            cjk_declared_in_prose = {
                entity
                for entity in declared_entities
                if _is_short_cjk_name(entity) and entity in item_text
            }
            structured_entities = (
                declared_entities & explicit_item_entities
            ) | cjk_declared_in_prose
        query_projects = self._project_entities(query)
        item_projects = self._project_entities("\n".join([item.content, item.summary]))

        if structured_entities:
            # Structured entities own scope regardless of casing. Prose and
            # Project mentions can reveal a conflicting scoped query, but they
            # can never rescue a memory whose declared subject does not match.
            if any(
                self._query_mentions_scope_entity(query, entity)
                for entity in structured_entities
            ):
                return False
            if query_projects & declared_projects:
                return False
            conflicting_prose_entities = explicit_item_entities | item_projects
            if query_projects or any(
                self._query_mentions_scope_entity(query, entity)
                for entity in conflicting_prose_entities
            ):
                return True
            explicit_query_entities = self._explicit_query_scope_entities(query)
            if (
                not claim_entities
                and not declared_projects
                and len(declared_entities) > 3
                and _explicit_cjk_query_entities_corroborated_in_item(
                    explicit_query_entities,
                    f"{item.content}\n{item.summary}",
                )
            ):
                return False
            return bool(explicit_query_entities)

        # Without a structured subject, Project-prefixed entities remain a hard
        # isolation signal and prose proper names are a conservative fallback.
        entity_text = "\n".join(str(entity) for entity in metadata_entities(meta))
        item_projects |= self._project_entities(entity_text)
        if query_projects:
            if not item_projects:
                return False
            return not bool(query_projects & item_projects)

        explicit_query_entities = self._explicit_query_scope_entities(query)
        hard_item_entities = explicit_item_entities
        if any(
            self._query_mentions_scope_entity(query, entity)
            for entity in hard_item_entities
        ):
            return False
        if explicit_query_entities and hard_item_entities:
            return True

        item_scope_entities = self._scope_entities(
            metadata_entities(meta, item.content, item.target)
        )
        candidate_scope_entities = hard_item_entities | item_scope_entities
        if not candidate_scope_entities:
            return False
        for entity in candidate_scope_entities:
            if self._query_mentions_scope_entity(query, entity):
                explicit_query_entities.add(entity)
        if not explicit_query_entities:
            return False
        return not bool(explicit_query_entities & candidate_scope_entities)

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

        Generated edges are admitted only while both endpoint scopes have a
        current containment receipt.  Manual/reviewed edges remain available,
        and an unavailable generated signal never hides the SQLite candidate.
        """
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
        scopes = [
            str(scope_id)
            for scope_id in (
                getattr(self.provider, "_accessible_scope_ids", []) or []
            )
            if str(scope_id)
        ]
        scope_clause = ""
        scope_params: list[str] = []
        if scopes:
            scope_placeholders = ",".join("?" for _ in scopes)
            scope_clause = (
                f" AND s.scope_id IN ({scope_placeholders})"
                f" AND t.scope_id IN ({scope_placeholders})"
            )
            scope_params = [*scopes, *scopes]
        relation_sql = f"""
                    SELECT r.source_memory_id, r.target_memory_id,
                           r.relation_type, r.confidence, r.note,
                           s.scope_id AS source_scope_id,
                           t.scope_id AS target_scope_id
                    FROM memory_relations r
                    JOIN memories s ON s.id = r.source_memory_id
                    JOIN memories t ON t.id = r.target_memory_id
                    WHERE (r.source_memory_id IN ({placeholders}) OR r.target_memory_id IN ({placeholders}))
                      AND {ordinary_recall_lifecycle_visible_sql('s')}
                      AND {ordinary_recall_lifecycle_visible_sql('t')}{scope_clause}
                    """
        relation_params = [*ids, *ids, *scope_params]
        memory_scope_sql = f"""
            SELECT id, scope_id
            FROM memories
            WHERE id IN ({placeholders})
        """
        try:
            lock = getattr(self.provider, "_lock", None)
            deadline = current_request_deadline()
            held = False
            if lock is not None:
                if not acquire_until(lock, deadline):
                    return {}
                held = True
            try:
                conn = self.provider._require_conn()
                with using_request_busy_timeout(conn):
                    memory_scope_rows = conn.execute(memory_scope_sql, ids).fetchall()
                    endpoint_scopes = {
                        str(row[1]) for row in memory_scope_rows if str(row[1] or "")
                    }
                    policy = generated_relation_scope_policy(conn, endpoint_scopes)
                    rows = conn.execute(relation_sql, relation_params).fetchall()
            finally:
                if held and lock is not None:
                    lock.release()
        except sqlite3.Error as exc:
            if is_sqlite_lock_contention(exc):
                return {}
            raise
        except Exception:
            return {}

        memory_scopes = {str(row[0]): str(row[1]) for row in memory_scope_rows}
        for memory_id, scope_id in memory_scopes.items():
            scope_policy = policy.get(scope_id) or {}
            if bool(scope_policy.get("generated_signal_enabled")):
                continue
            payload = _payload(memory_id)
            payload["generated_signal_disabled"] = True
            payload["generated_signal_reason"] = str(
                scope_policy.get("reason_code") or "containment_state_missing"
            )
            payload["relation_scope_state"] = str(
                scope_policy.get("state") or "disabled"
            )

        for row in rows:
            source_id = str(row["source_memory_id"])
            target_id = str(row["target_memory_id"])
            note = str(row["note"] or "").strip().lower()
            if note.startswith("relation-extraction:"):
                source_scope = str(row["source_scope_id"])
                target_scope = str(row["target_scope_id"])
                if source_scope != target_scope:
                    continue
                source_policy = policy.get(source_scope) or {}
                target_policy = policy.get(target_scope) or {}
                if not (
                    bool(source_policy.get("generated_signal_enabled"))
                    and bool(target_policy.get("generated_signal_enabled"))
                ):
                    continue
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
                "generated_signal_disabled": bool(
                    payload.get("generated_signal_disabled")
                ),
                "generated_signal_reason": str(
                    payload.get("generated_signal_reason") or ""
                ),
                "relation_scope_state": str(
                    payload.get("relation_scope_state") or ""
                ),
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
            deadline = current_request_deadline()
            held = False
            if lock is not None:
                if not acquire_until(lock, deadline):
                    return {}
                held = True
            try:
                conn = self.provider._require_conn()
                with using_request_busy_timeout(conn):
                    return memory_freshness_map(conn, memory_ids)
            finally:
                if held and lock is not None:
                    lock.release()
        except sqlite3.Error as exc:
            if is_sqlite_lock_contention(exc):
                return {}
            raise
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
        return filter_recall_lifecycle(items)

    def _apply_general_policy(self, items: list[RecallItem]) -> list[RecallItem]:
        retrieval_cfg = self.provider._retrieval_config or {}
        return apply_general_policy(
            items,
            include_general=str(retrieval_cfg.get("include_general") or "same-scope"),
            general_weight=float(retrieval_cfg.get("general_weight") or 0.35),
            general_min_importance=retrieval_cfg.get("general_min_importance"),
            current_scope_id=str(self.provider._scope_id),
        )

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
