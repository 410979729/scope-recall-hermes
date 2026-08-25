"""Dispatcher for Hermes tool calls exposed by Scope Recall.

Handlers translate public tool arguments into provider operations, sanitize errors, and return stable JSON receipts."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from tools.registry import tool_error  # type: ignore[reportMissingImports]

from .capture_filters import (
    CaptureFilterResult,
    sanitize_report_text,
    sanitize_structured_value,
    should_capture_text,
)
from .gating import config_bool
from .graph import clamp_float
from .models import resolve_store_scope_mode
from .scope import accessible_scope_ids as runtime_accessible_scope_ids
from .schemas import (
    DEFAULT_EVIDENCE_DIVERSITY_DEPTH,
    MAX_EVIDENCE_DIVERSITY_DEPTH,
    MAX_MEMORY_ID_LENGTH,
    MAX_MEMORY_IDS_PER_REQUEST,
)
from .tool_validation import validate_tool_arguments
from .experience_preflight import experience_preflight
from .experience_promotion import promote_experiences
from .experience_store import (
    create_playbook,
    experience_stats,
    find_duplicate_playbooks,
    inspect_playbook,
    merge_playbooks,
    record_playbook_feedback,
    review_playbook,
    search_playbooks,
)
from .fact_tooling import (
    execute_maintenance_evolution,
    execute_structured_store,
    execute_structured_update,
    has_structured_fact_hint,
)
from .forgetting import build_forgetting_report, run_forgetting
from .secret_index import build_secret_index
from .temporal_query import query_fact_views
from ._internal.runtime.tool_port import bind_tool_runtime_port

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


TOOL_ALIASES = {
    "lancepro_store": "scope_recall_store",
    "lancepro_search": "scope_recall_search",
    "lancepro_stats": "scope_recall_stats",
}


class _MemoryIdsArgumentError(ValueError):
    """Runtime validation error for direct handler callers that bypass schemas."""

    def __init__(self, message: str, *, field: str, constraint: str) -> None:
        super().__init__(message)
        self.field = field
        self.constraint = constraint


class ScopeRecallToolService:
    """Translate public Hermes tool calls into provider operations.

    Handlers validate user-facing arguments, enforce scope/tool feature flags, sanitize errors, and return stable JSON receipts. They should not bypass provider invariants or write directly to SQLite."""

    def __init__(self, provider: Any) -> None:
        self._port = bind_tool_runtime_port(provider)

    def normalize_tool_name(self, tool_name: str) -> str:
        return TOOL_ALIASES.get(tool_name, tool_name)

    _TRUTH_READ_ONLY_TOOLS = frozenset(
        {
            "scope_recall_search",
            "scope_recall_context",
            "scope_recall_profile",
            "scope_recall_fact",
            "scope_recall_stats",
            "scope_recall_inspect",
            "scope_recall_explain",
            "scope_recall_related",
            "scope_recall_entity",
            "scope_recall_probe",
            "scope_recall_export",
            "scope_recall_hygiene",
            "scope_recall_forgetting_report",
            "scope_recall_playbook_search",
            "scope_recall_playbook_inspect",
            "scope_recall_experience_stats",
            "scope_recall_benchmark",
        }
    )
    _MEMORY_DISPATCH_WRITE_ACTIONS = frozenset(
        {"feedback", "update", "merge", "forget"}
    )

    def _truth_write_blocked_error(self, tool_name: str) -> str:
        del tool_name
        return tool_error(
            "truth_writer_busy: this tool is unavailable because another "
            "Scope Recall process holds the truth-database writer lease. "
            "Recall/search remain available; durable writes must go through "
            "the writer process."
        )

    def _reader_tool_allowed(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name in self._TRUTH_READ_ONLY_TOOLS:
            return True
        if tool_name == "scope_recall_memory":
            action = str((args or {}).get("action") or "").strip().lower()
            action = action.replace("-", "_")
            aliases = {"rate": "feedback", "delete": "forget", "remove": "forget", "get": "inspect"}
            return aliases.get(action, action) == "inspect"
        if tool_name == "scope_recall_reflect":
            return not self._bool_arg(args or {}, "propose_memory", False)
        if tool_name == "scope_recall_experience_preflight":
            return not self._bool_arg(args or {}, "record_run", False)
        return False

    def handle(self, tool_name: str, args: dict[str, Any]) -> str:
        normalized = self.normalize_tool_name(tool_name)
        payload = args or {}
        handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "scope_recall_store": self._handle_store,
            "scope_recall_store_secret_index": self._handle_store_secret_index,
            "scope_recall_search": self._handle_search,
            "scope_recall_context": self._handle_context,
            "scope_recall_memory": self._handle_memory,
            "scope_recall_entity": self._handle_entity,
            "scope_recall_profile": self._handle_profile,
            "scope_recall_probe": self._handle_probe,
            "scope_recall_related": self._handle_related,
            "scope_recall_feedback": self._handle_feedback,
            "scope_recall_forget": self._handle_forget,
            "scope_recall_update": self._handle_update,
            "scope_recall_dedupe": self._handle_dedupe,
            "scope_recall_merge": self._handle_merge,
            "scope_recall_export": self._handle_export,
            "scope_recall_govern": self._handle_govern,
            "scope_recall_hygiene": self._handle_hygiene,
            "scope_recall_repair": self._handle_repair,
            "scope_recall_stats": self._handle_stats,
            "scope_recall_inspect": self._handle_inspect,
            "scope_recall_explain": self._handle_explain,
            "scope_recall_benchmark": self._handle_benchmark,
            "scope_recall_fact": self._handle_fact,
            "scope_recall_evolve": self._handle_evolve,
            "scope_recall_reflect": self._handle_reflect,
            "scope_recall_playbook_create": self._handle_playbook_create,
            "scope_recall_playbook_search": self._handle_playbook_search,
            "scope_recall_playbook_inspect": self._handle_playbook_inspect,
            "scope_recall_experience_preflight": self._handle_experience_preflight,
            "scope_recall_playbook_feedback": self._handle_playbook_feedback,
            "scope_recall_playbook_review": self._handle_playbook_review,
            "scope_recall_experience_stats": self._handle_experience_stats,
            "scope_recall_experience_promote": self._handle_experience_promote,
            "scope_recall_forgetting_report": self._handle_forgetting_report,
            "scope_recall_forgetting_run": self._handle_forgetting_run,
        }
        handler = handlers.get(normalized)
        if self._reader_tool_allowed(normalized, payload):
            return self._invoke_handler(tool_name, normalized, handler, payload)
        with self._port.writer_lifecycle_lock():
            if not self._port.has_positive_write_authority():
                return self._truth_write_blocked_error(normalized)
            return self._invoke_handler(tool_name, normalized, handler, payload)

    def _invoke_handler(
        self,
        tool_name: str,
        normalized: str,
        handler: Callable[[dict[str, Any]], str] | None,
        args: dict[str, Any],
    ) -> str:
        if handler is None:
            return tool_error("unknown scope-recall tool")
        issue = validate_tool_arguments(normalized, args)
        if issue is not None:
            return tool_error(
                issue.public_message(),
                invalid_arguments=True,
                field=issue.field,
                constraint=issue.constraint,
            )
        try:
            return handler(args)
        except Exception as exc:
            self._port.rollback_conn_after_error(f"tool {normalized}")
            safe_error = sanitize_report_text(str(exc))
            logger.warning("Scope Recall tool %s failed: %s", tool_name, safe_error)
            return tool_error(safe_error)

    def _receipt(
        self,
        action: str,
        *,
        target: str = "",
        id: str = "",
        scope_mode: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": action,
            "provider": "scope-recall",
            "at": _now_iso(),
        }
        if target:
            data["target"] = target
        if id:
            data["id"] = id
        if scope_mode:
            data["scope_mode"] = scope_mode
        data.update(
            {key: value for key, value in extra.items() if value not in (None, "", [])}
        )
        return data

    def _handle_store(self, args: dict[str, Any]) -> str:
        """Handle public store calls while enforcing scope and shared-pool write policy.

        This method returns explicit receipts for rejected writes so callers can tell policy denial from storage failure or duplicate detection."""
        content = self._port.clean_text(str(args.get("content") or ""))
        if not content:
            return tool_error("content is required")
        target = str(args.get("target") or "memory").strip().lower()
        requested_scope_mode = (
            str(args.get("scope_mode") or "").strip().lower().replace("-", "_")
        )
        try:
            scope_mode = resolve_store_scope_mode(
                target,
                "tool-store",
                requested_scope_mode or None,
            )
        except ValueError:
            return tool_error(
                "scope mode is incompatible with target",
                invalid_scope_mode=True,
                target=target,
                scope_mode=requested_scope_mode,
            )
        if scope_mode == "shared_pool":
            shared_pool_config = (
                self._port.config_view().get("shared_pool")
                if isinstance(self._port.config_view(), dict)
                else {}
            )
            shared_pool_config = (
                shared_pool_config if isinstance(shared_pool_config, dict) else {}
            )
            allowed_targets = shared_pool_config.get("allowed_targets")
            allowed_target_set = {"memory", "project", "ops"}
            if isinstance(allowed_targets, list):
                configured = {
                    str(item).strip().lower()
                    for item in allowed_targets
                    if str(item).strip()
                }
                if configured:
                    allowed_target_set = configured
            if not self._port.shared_pool_enabled():
                return self._json(
                    {
                        "stored": False,
                        "duplicate": False,
                        "merged": False,
                        "skipped": True,
                        "skip_reason": "shared_pool_disabled",
                        "id": "",
                        "target": target,
                        "scope_mode": scope_mode,
                        "receipt": self._receipt(
                            "shared_pool_write_rejected",
                            target=target,
                            scope_mode=scope_mode,
                            reason="shared_pool_disabled",
                        ),
                    }
                )
            if not self._port.shared_pool_write_enabled():
                return self._json(
                    {
                        "stored": False,
                        "duplicate": False,
                        "merged": False,
                        "skipped": True,
                        "skip_reason": "shared_pool_write_disabled",
                        "id": "",
                        "target": target,
                        "scope_mode": scope_mode,
                        "receipt": self._receipt(
                            "shared_pool_write_rejected",
                            target=target,
                            scope_mode=scope_mode,
                            reason="shared_pool_write_disabled",
                        ),
                    }
                )
            if target not in allowed_target_set:
                return self._json(
                    {
                        "stored": False,
                        "duplicate": False,
                        "merged": False,
                        "skipped": True,
                        "skip_reason": "shared_pool_target_not_allowed",
                        "id": "",
                        "target": target,
                        "scope_mode": scope_mode,
                        "receipt": self._receipt(
                            "shared_pool_write_rejected",
                            target=target,
                            scope_mode=scope_mode,
                            reason="shared_pool_target_not_allowed",
                        ),
                    }
                )
        filter_result = self._storage_filter(content)
        if not filter_result.allowed:
            return self._json(
                {
                    "stored": False,
                    "duplicate": False,
                    "merged": False,
                    "skipped": True,
                    "skip_reason": filter_result.reason,
                    "id": "",
                    "target": target,
                    "scope_mode": scope_mode,
                    "receipt": self._receipt(
                        "rejected_sensitive",
                        target=target,
                        scope_mode=scope_mode,
                        reason=filter_result.reason,
                    ),
                }
            )
        if has_structured_fact_hint(args):
            return self._json(
                execute_structured_store(
                    self._port,
                    args=args,
                    content=content,
                    target=target,
                    scope_mode=scope_mode,
                    metadata=self._store_metadata(args),
                )
            )
        store_kwargs = {
            "content": content,
            "source": "tool-store",
            "target": target,
            "session_id": self._port.session_id(),
            "metadata": self._store_metadata(args),
            "semantic_merge": self._bool_arg(args, "semantic_merge", False),
            "scope_mode": scope_mode,
        }
        recovered = False
        retry_count = 0
        try:
            memory_id, inserted, outcome = self._port.store_now(**store_kwargs)
        except Exception as exc:
            if not self._recoverable_store_error(exc):
                raise
            recovery_payload = dict(
                self._port.recover_sqlite_connection_after_error("scope_recall_store")
            )
            if not recovery_payload.get("recovered"):
                raise
            recovered = True
            retry_count = 1
            memory_id, inserted, outcome = self._port.store_now(**store_kwargs)
        if outcome == "stored_relation_sync_deferred":
            relation_sync_status = "deferred"
        elif outcome == "stored_relation_sync_blocked":  # legacy receipt compatibility
            relation_sync_status = "blocked"
        elif outcome == "stored_relation_sync_disabled":
            relation_sync_status = "disabled"
        elif inserted:
            relation_sync_status = "completed"
        else:
            relation_sync_status = "not_run"
        receipt = self._receipt(
            "promoted" if inserted else outcome,
            target=target,
            id=memory_id,
            scope_mode=scope_mode,
        )
        receipt["relation_sync_status"] = relation_sync_status
        if recovered:
            receipt["recovered"] = True
            receipt["retry_count"] = retry_count
        return self._json(
            {
                "stored": bool(inserted),
                "duplicate": outcome == "duplicate",
                "merged": outcome == "merged",
                "skipped": outcome == "skipped",
                "id": memory_id,
                "target": target,
                "scope_mode": scope_mode,
                "relation_sync_status": relation_sync_status,
                "recovered": recovered,
                "retry_count": retry_count,
                "receipt": receipt,
            }
        )

    def _recoverable_store_error(self, exc: Exception) -> bool:
        if not isinstance(exc, sqlite3.Error):
            return False
        message = str(exc).lower()
        return "locked" in message or "transaction" in message

    def _handle_store_secret_index(self, args: dict[str, Any]) -> str:
        if not config_bool(self._port.config_view(), "secret_index_tools_enabled", False):
            return tool_error(
                "scope_recall_store_secret_index requires secret_index_tools_enabled=true"
            )
        content, metadata = build_secret_index(args)
        target = str(args.get("target") or "ops").strip().lower()
        if target not in {"memory", "project", "ops"}:
            target = "ops"
        scope_mode = self._port.scope_mode_for(target, "secret-index")
        filter_result = self._storage_filter(content)
        if not filter_result.allowed:
            return tool_error(
                "secret index content is not suitable for storage after redaction",
                skipped=True,
                skip_reason=filter_result.reason,
                receipt=self._receipt(
                    "rejected_sensitive",
                    target=target,
                    scope_mode=scope_mode,
                    reason=filter_result.reason,
                ),
            )
        memory_id, inserted, outcome = self._port.store_now(
            content=content,
            source="secret-index",
            target=target,
            session_id=self._port.session_id(),
            metadata=metadata,
            semantic_merge=False,
        )
        return self._json(
            {
                "stored": bool(inserted),
                "duplicate": outcome == "duplicate",
                "merged": outcome == "merged",
                "skipped": outcome == "skipped",
                "id": memory_id,
                "target": target,
                "scope_mode": scope_mode,
                "secret_value_stored": False,
                "vault_ref": metadata.get("vault_ref", ""),
                "receipt": self._receipt(
                    "secret_index_promoted" if inserted else outcome,
                    target=target,
                    id=memory_id,
                    scope_mode=scope_mode,
                    secret_value_stored=False,
                    vault_ref=metadata.get("vault_ref", ""),
                ),
            }
        )

    def _handle_search(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        limit = self._retrieval_limit(args)
        recall_mode = str(args.get("recall_mode") or "advisory").strip().lower()
        if recall_mode not in {"advisory", "strict"}:
            return tool_error("recall_mode must be advisory or strict")
        query_variants = self._query_variants(args)
        recall: Any = self._port.recall_service_view()
        if query_variants:
            results = recall.search_evidence_set(
                query,
                query_variants=query_variants,
                limit=limit,
                per_query_limit=limit,
                diversity_depth=self._evidence_diversity_depth(args),
                recall_mode=recall_mode,
            )
        else:
            results = recall.search_memories(
                query,
                limit=limit,
                recall_mode=recall_mode,
            )
        payload: dict[str, Any] = {
            "count": len(results),
            "results": [self._serialize_recall_item(item) for item in results],
        }
        raw_temporal_diagnostics = getattr(
            recall,
            "last_temporal_query_diagnostics",
            {},
        )
        temporal_diagnostics = self._public_temporal_candidate_diagnostics(
            raw_temporal_diagnostics
        )
        if temporal_diagnostics:
            payload["temporal_candidate_diagnostics"] = temporal_diagnostics
        if self._bool_arg(args, "include_trace", False):
            payload["funnel_trace"] = dict(
                getattr(recall, "last_funnel_trace", {}) or {}
            )
            if query_variants:
                payload["evidence_set_trace"] = dict(
                    getattr(
                        recall,
                        "last_evidence_set_trace",
                        {},
                    )
                    or {}
                )
        return self._json(payload)

    def _handle_context(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        return self._json(
            self._port.context_payload(
                query=query,
                limit=self._limit(args),
                max_chars=max(120, min(4000, int(args.get("max_chars") or 900))),
            )
        )

    def _handle_profile(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args) if args.get("query") else ""
        entity = str(args.get("entity") or "").strip()
        return self._json(
            self._port.profile_payload(
                query=query,
                entity=entity,
                targets=self._targets_arg(args),
                include_general=self._bool_arg(args, "include_general", False),
                include_candidates=self._bool_arg(args, "include_candidates", False),
                include_curated=self._bool_arg(args, "include_curated", True),
                limit=self._limit(args),
                max_chars=max(120, min(4000, int(args.get("max_chars") or 1200))),
            )
        )

    def _handle_probe(self, args: dict[str, Any]) -> str:
        entity = str(args.get("entity") or "").strip()
        if not entity:
            return tool_error("entity is required")
        return self._json(
            self._port.probe_entity(entity=entity, limit=self._limit(args))
        )

    def _handle_related(self, args: dict[str, Any]) -> str:
        entity = str(args.get("entity") or "").strip()
        if not entity:
            return tool_error("entity is required")
        return self._json(
            self._port.related_entities(entity=entity, limit=self._limit(args))
        )

    def _handle_memory(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        aliases = {
            "rate": "feedback",
            "delete": "forget",
            "remove": "forget",
            "get": "inspect",
        }
        action = aliases.get(action, action)
        if action == "inspect":
            return self._handle_inspect(args)
        if action == "feedback":
            return self._handle_feedback(args)
        if action == "update":
            return self._handle_update(args)
        if action == "merge":
            return self._handle_merge(args)
        if action == "forget":
            return self._handle_forget(args)
        return tool_error(
            "action must be one of: inspect, feedback, update, merge, forget"
        )

    def _handle_entity(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        if action == "probe":
            return self._handle_probe(args)
        if action in {"related", "relations"}:
            return self._handle_related(args)
        return tool_error("action must be one of: probe, related")

    def _handle_feedback(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        if not memory_id:
            return tool_error("id is required")
        rating = str(args.get("rating") or "").strip()
        if not rating:
            return tool_error("rating is required")
        return self._json(
            self._port.feedback_memory(
                memory_id=memory_id,
                rating=rating,
                note=self._port.clean_text(str(args.get("note") or "")),
            )
        )

    def _handle_forget(self, args: dict[str, Any]) -> str:
        try:
            ids = self._memory_ids_arg(args)
        except _MemoryIdsArgumentError as exc:
            return tool_error(
                str(exc),
                invalid_arguments=True,
                field=exc.field,
                constraint=exc.constraint,
            )
        if not ids:
            return tool_error(
                "ids are required for scope_recall_forget; search or inspect first, then pass exact ids"
            )
        reason = self._port.clean_text(
            str(args.get("reason") or "scope_recall_forget")
        )
        if self._bool_arg(args, "hard_delete", False):
            if not self._operator_mode_enabled():
                return tool_error(
                    "scope_recall_forget hard_delete requires maintenance_tools_enabled=true"
                )
            blocked_fact_ids = self._port.fact_owned_memory_ids(ids)
            if blocked_fact_ids:
                return self._json(
                    {
                        "archived": 0,
                        "deleted": 0,
                        "ids": [],
                        "blocked_fact_ids": blocked_fact_ids,
                        "hard_delete": True,
                        "error": (
                            "legacy memory hard delete is blocked for fact-owned "
                            "memory; use structured Fact Evolution/review"
                        ),
                        "receipt": self._receipt(
                            "hard_delete_blocked",
                            reason="fact mutation requires structured Fact Evolution/review",
                        ),
                    }
                )
            deleted = self._port.delete_memories(ids)
            return self._json(
                {
                    "archived": 0,
                    "deleted": deleted,
                    "ids": ids,
                    "hard_delete": True,
                    "receipt": self._receipt("hard_delete", reason=reason),
                }
            )
        return self._json(
            self._port.archive_memories(
                ids, reason=reason, actor="scope_recall_forget"
            )
        )

    def _handle_update(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        content = self._port.clean_text(str(args.get("content") or ""))
        if not memory_id:
            return tool_error("id is required")
        if not content:
            return tool_error("content is required")
        filter_result = self._storage_filter(content)
        if not filter_result.allowed:
            return tool_error(
                "content is not suitable for storage",
                skipped=True,
                skip_reason=filter_result.reason,
                receipt=self._receipt(
                    "rejected_sensitive", reason=filter_result.reason
                ),
            )
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        if has_structured_fact_hint(args):
            return self._json(
                execute_structured_update(
                    self._port,
                    args=args,
                    memory_id=memory_id,
                    content=content,
                    target=target,
                    metadata=self._store_metadata(args),
                )
            )
        updated, summary, updated_at = self._port.update_memory(
            memory_id, content, target
        )
        if not updated:
            error = summary or "id not found"
            return self._json(
                {
                    "updated": False,
                    "id": memory_id,
                    "error": error,
                    "blocked_fact_ids": (
                        [memory_id]
                        if "structured Fact Evolution/review" in error
                        else []
                    ),
                }
            )
        row = (
            self._port.query_connection()
            .execute(
                "SELECT source, target, scope_id FROM memories WHERE id = ?",
                (memory_id,),
            )
            .fetchone()
        )
        actual_target = str(row["target"]) if row is not None else (target or "")
        source = str(row["source"]) if row is not None else ""
        if row is not None and str(row["scope_id"]) == str(
            self._port.shared_pool_scope_id()
        ):
            scope_mode = "shared_pool"
        else:
            scope_mode = self._port.scope_mode_for(actual_target, source)
        return self._json(
            {
                "updated": True,
                "id": memory_id,
                "target": actual_target,
                "scope_mode": scope_mode,
                "summary": summary,
                "updated_at": updated_at,
                "receipt": self._receipt(
                    "updated", target=actual_target, id=memory_id, scope_mode=scope_mode
                ),
            }
        )

    def _handle_dedupe(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_dedupe requires maintenance_tools_enabled=true"
            )
        scope_only = self._bool_arg(args, "scope_only", True)
        return self._json(
            self._port.dedupe_memories(
                dry_run=self._bool_arg(args, "dry_run", True),
                scope_only=scope_only,
            )
        )

    def _handle_merge(self, args: dict[str, Any]) -> str:
        target_id = str(args.get("target_id") or "").strip()
        if not target_id:
            return tool_error("target_id is required")
        if len(target_id) > MAX_MEMORY_ID_LENGTH:
            return tool_error(
                f"memory id must not exceed {MAX_MEMORY_ID_LENGTH} characters",
                invalid_arguments=True,
                field="target_id",
                constraint=f"maxLength={MAX_MEMORY_ID_LENGTH}",
            )
        source_ids = args.get("source_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        elif not isinstance(source_ids, list):
            return tool_error(
                "source_ids must be an array of memory ids",
                invalid_arguments=True,
                field="source_ids",
                constraint="type=array",
            )
        if len(source_ids) > MAX_MEMORY_IDS_PER_REQUEST:
            return tool_error(
                f"source_ids must contain at most {MAX_MEMORY_IDS_PER_REQUEST} items",
                invalid_arguments=True,
                field="source_ids",
                constraint=f"maxItems={MAX_MEMORY_IDS_PER_REQUEST}",
            )
        if any(len(str(item).strip()) > MAX_MEMORY_ID_LENGTH for item in source_ids):
            return tool_error(
                f"memory id must not exceed {MAX_MEMORY_ID_LENGTH} characters",
                invalid_arguments=True,
                field="source_ids",
                constraint=f"maxLength={MAX_MEMORY_ID_LENGTH}",
            )
        content_arg = args.get("content")
        content = self._port.clean_text(str(content_arg)) if content_arg else None
        if content is not None:
            filter_result = self._storage_filter(content)
            if not filter_result.allowed:
                return tool_error(
                    "content is not suitable for storage",
                    skipped=True,
                    skip_reason=filter_result.reason,
                    receipt=self._receipt(
                        "rejected_sensitive", reason=filter_result.reason
                    ),
                )
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        payload = self._port.merge_memories(
            target_id, [str(item) for item in source_ids], content, target
        )
        if payload.get("merged"):
            payload["receipt"] = self._receipt(
                "merged",
                target=str(payload.get("target") or target or ""),
                id=str(payload.get("id") or payload.get("target_id") or ""),
                scope_mode=str(payload.get("scope_mode") or ""),
                target_id=str(payload.get("target_id") or ""),
                source_ids=payload.get("source_ids") or [],
                source_candidate_id=str(args.get("source_candidate_id") or ""),
            )
        return self._json(payload)

    def _handle_export(self, args: dict[str, Any]) -> str:
        scope_only = self._bool_arg(args, "scope_only", True)
        if not scope_only and not self._operator_mode_enabled():
            return tool_error(
                "scope_only=false requires maintenance_tools_enabled=true"
            )
        return self._json(
            self._port.export_memories(
                fmt=str(args.get("format") or "jsonl"),
                scope_only=scope_only,
            )
        )

    def _handle_govern(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_govern requires maintenance_tools_enabled=true"
            )
        scope_only = self._bool_arg(args, "scope_only", True)
        return self._json(
            self._port.govern_memories(
                dry_run=self._bool_arg(args, "dry_run", True),
                scope_only=scope_only,
            )
        )

    def _handle_repair(self, args: dict[str, Any]) -> str:
        del args
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_repair requires maintenance_tools_enabled=true"
            )
        return self._json(self._port.repair_vector())

    def _handle_hygiene(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_hygiene requires maintenance_tools_enabled=true"
            )
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        return self._json(self._port.hygiene_report(limit=limit))

    def _handle_forgetting_report(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_forgetting_report requires maintenance_tools_enabled=true"
            )
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        raw_forgetting = self._port.config_view().get("forgetting")
        forgetting_config = (
            raw_forgetting if isinstance(raw_forgetting, dict) else {}
        )
        if not config_bool(forgetting_config, "enabled", True):
            return tool_error("forgetting tools are disabled by forgetting.enabled=false")
        with self._port.query_lock():
            payload = build_forgetting_report(
                self._port.query_connection(),
                accessible_scope_ids=self._port.accessible_scope_ids(),
                limit=limit,
                config=forgetting_config,
            )
        return self._json(payload)

    def _handle_forgetting_run(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_forgetting_run requires maintenance_tools_enabled=true"
            )
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        raw_forgetting = self._port.config_view().get("forgetting")
        forgetting_config = (
            raw_forgetting if isinstance(raw_forgetting, dict) else {}
        )
        if not config_bool(forgetting_config, "enabled", True):
            return tool_error("forgetting tools are disabled by forgetting.enabled=false")
        soft_archive = (
            self._bool_arg(args, "soft_archive", True)
            if "soft_archive" in args
            else None
        )
        with self._port.query_lock():
            payload = run_forgetting(
                self._port.query_connection(),
                accessible_scope_ids=self._port.writable_scope_ids(),
                dry_run=self._bool_arg(args, "dry_run", True),
                hard_delete=self._bool_arg(args, "hard_delete", False),
                soft_archive=soft_archive,
                config=forgetting_config,
                limit=limit,
                vector_store=self._port.vector_store_view(),
            )
        if payload.get("vector_error"):
            self._port.mark_vector_needs_repair(
                str(payload.get("vector_error") or "forgetting vector delete failed")
            )
        return self._json(payload)

    def _handle_stats(self, args: dict[str, Any]) -> str:
        del args
        return self._json(self._port.stats_payload())

    def _handle_inspect(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        if not memory_id:
            return tool_error("id is required")
        return self._json(self._port.inspect_memory(memory_id=memory_id))

    def _handle_explain(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        payload = self._port.explain_query(
            query=query, limit=self._retrieval_limit(args)
        )
        if isinstance(payload, dict):
            payload.setdefault("humanized", True)
        return self._json(payload)

    def _handle_benchmark(self, args: dict[str, Any]) -> str:
        char_limit = int(self._port.config_value("query_char_limit", 1000))
        raw_cases = args.get("cases") or []
        cases: list[dict[str, Any]] = []
        if isinstance(raw_cases, list):
            for raw_case in raw_cases:
                if not isinstance(raw_case, dict):
                    continue
                query = self._port.normalize_query(
                    str(raw_case.get("query") or ""), char_limit
                )
                if not query:
                    continue
                case = dict(raw_case)
                case["query"] = query
                cases.append(case)
        raw_queries = args.get("queries") or []
        if isinstance(raw_queries, str):
            queries = [raw_queries]
        elif isinstance(raw_queries, list):
            queries = [str(query) for query in raw_queries]
        else:
            queries = []
        queries = [
            self._port.normalize_query(query, char_limit) for query in queries
        ]
        queries = [query for query in queries if query]
        if not queries and not cases:
            return tool_error("queries or cases is required")
        return self._json(
            self._port.benchmark_queries(
                queries=queries,
                cases=cases,
                limit=self._retrieval_limit(args),
                auto_explain_on_fail=self._bool_arg(
                    args, "auto_explain_on_fail", False
                ),
                include_trace=self._bool_arg(args, "include_trace", False),
                prompt_budget_chars=max(0, int(args.get("prompt_budget_chars") or 0)),
            )
        )

    def _handle_fact(self, args: dict[str, Any]) -> str:
        raw_config = self._port.config_view().get("temporal_queries")
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        if not config_bool(config, "enabled", False):
            return tool_error(
                "scope_recall_fact requires temporal_queries.enabled=true"
            )
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        subject = self._port.clean_text(str(args.get("subject") or ""))
        predicate = self._port.clean_text(str(args.get("predicate") or ""))
        if not action:
            return tool_error("action is required")
        if not subject:
            return tool_error("subject is required")
        if not predicate:
            return tool_error("predicate is required")
        raw_configured_limit = config.get("current_limit", 50)
        if type(raw_configured_limit) is not int:
            return tool_error("temporal_queries.current_limit must be an integer")
        configured_limit = raw_configured_limit
        if configured_limit < 1 or configured_limit > 100:
            return tool_error(
                "temporal_queries.current_limit must be between 1 and 100"
            )
        raw_requested_limit = (
            configured_limit if args.get("limit") is None else args["limit"]
        )
        if type(raw_requested_limit) is not int:
            return tool_error("limit must be an integer")
        requested_limit = raw_requested_limit
        if requested_limit < 1 or requested_limit > 100:
            return tool_error("limit must be between 1 and 100")
        limit = min(requested_limit, configured_limit)
        with self._port.query_lock():
            views = query_fact_views(
                self._port.query_connection(),
                scope_ids=self._port.accessible_scope_ids(),
                action=action,
                subject=subject,
                predicate=predicate,
                at=args.get("at"),
                known_at=args.get("known_at"),
                timezone_name=str(config.get("timezone") or "UTC"),
                limit=limit,
            )
        return self._json(
            {
                "action": action,
                "count": len(views),
                "limit": limit,
                "facts": [view.as_dict() for view in views],
            }
        )

    def _handle_evolve(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_evolve requires maintenance_tools_enabled=true"
            )
        return self._json(
            execute_maintenance_evolution(self._port, args=args)
        )

    def _handle_reflect(self, args: dict[str, Any]) -> str:
        return self._json(self._port.run_reflection(args=args))

    def _playbook_scope_id(self) -> str:
        return str(self._port.shared_scope_id() or self._port.scope_id())

    def _playbook_shared_scope_id(self) -> str:
        return self._port.shared_pool_scope_id()

    def _playbook_owner_scope_aliases(self) -> list[str]:
        """Return authenticated canonical and legacy owner scopes, excluding pools."""

        scope = self._port.scope_object()
        if scope is None:
            return []
        config = self._port.config_view() if isinstance(self._port.config_view(), dict) else {}
        return runtime_accessible_scope_ids(scope, config)

    def _experience_enabled(self) -> bool:
        raw_config = (
            self._port.config_view().get("experience")
            if isinstance(self._port.config_view(), dict)
            else {}
        )
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        return config_bool(config, "enabled", True)

    def _experience_disabled_error(self) -> str:
        return tool_error("Experience Kernel is disabled")

    def _handle_playbook_create(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_playbook_create requires maintenance_tools_enabled=true"
            )
        payload = args.get("payload")
        if not isinstance(payload, dict):
            return tool_error("payload object is required")
        confidence = args.get("confidence")
        if confidence is None:
            confidence_value = None
        else:
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                return tool_error("confidence must be numeric")
        with self._port.query_lock():
            playbook = create_playbook(
                self._port.query_connection(),
                playbook_id=str(args.get("id") or "").strip() or None,
                scope_id=self._playbook_scope_id(),
                shared_scope_id=self._playbook_shared_scope_id(),
                payload=payload,
                status=str(args.get("status") or "candidate"),
                confidence=confidence_value,
                created_from_episode_id=str(args.get("created_from_episode_id") or ""),
                evidence_anchors=args.get("evidence_anchors")
                if isinstance(args.get("evidence_anchors"), list)
                else [],
                related_skills=args.get("related_skills")
                if isinstance(args.get("related_skills"), list)
                else [],
                environment_constraints=args.get("environment_constraints")
                if isinstance(args.get("environment_constraints"), dict)
                else {},
                metadata=args.get("metadata")
                if isinstance(args.get("metadata"), dict)
                else {},
            )
        return self._json({"created": True, "playbook": playbook})

    def _handle_playbook_search(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        query = self._clean_query(args) if args.get("query") else ""
        with self._port.query_lock():
            results = search_playbooks(
                self._port.query_connection(),
                query=query,
                accessible_scope_ids=self._port.accessible_scope_ids(),
                limit=self._limit(args),
                task_class=str(args.get("task_class") or ""),
                status=str(args.get("status") or ""),
            )
        return self._json({"count": len(results), "results": results})

    def _handle_playbook_inspect(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        playbook_id = str(args.get("id") or "").strip()
        if not playbook_id:
            return tool_error("id is required")
        with self._port.query_lock():
            payload = inspect_playbook(
                self._port.query_connection(),
                playbook_id=playbook_id,
                accessible_scope_ids=self._port.accessible_scope_ids(),
            )
        return self._json(payload)

    def _handle_experience_preflight(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        with self._port.query_lock():
            payload = experience_preflight(
                self._port.query_connection(),
                query=query,
                accessible_scope_ids=self._port.accessible_scope_ids(),
                config=self._port.config_view(),
                limit=self._limit(args),
            )
        return self._json(payload)

    def _handle_playbook_feedback(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        playbook_id = str(args.get("id") or "").strip()
        if not playbook_id:
            return tool_error("id is required")
        outcome = str(args.get("outcome") or "").strip()
        if not outcome:
            return tool_error("outcome is required")
        evidence: list[Any] | None = None
        if "evidence" in args:
            raw_evidence = args.get("evidence")
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, list)
                else ([] if raw_evidence is None else [str(raw_evidence)])
            )
        preconditions_checked: list[Any] | None = None
        if "preconditions_checked" in args:
            raw_preconditions = args.get("preconditions_checked")
            preconditions_checked = (
                raw_preconditions
                if isinstance(raw_preconditions, list)
                else ([] if raw_preconditions is None else [str(raw_preconditions)])
            )
        steps_completed: list[Any] | None = None
        if "steps_completed" in args:
            raw_steps = args.get("steps_completed")
            steps_completed = (
                raw_steps
                if isinstance(raw_steps, list)
                else ([] if raw_steps is None else [str(raw_steps)])
            )
        decision = None if "decision" not in args else str(args.get("decision") or "")
        with self._port.query_lock():
            conn = self._port.query_connection()
            feedback_scope_id = self._playbook_scope_id()
            inspected = inspect_playbook(
                conn,
                playbook_id=playbook_id,
                accessible_scope_ids=self._port.accessible_scope_ids(),
            )
            if inspected.get("found"):
                playbook = (
                    inspected.get("playbook")
                    if isinstance(inspected.get("playbook"), dict)
                    else {}
                )
                owner_scope_id = (
                    str(playbook.get("scope_id") or "")
                    if isinstance(playbook, dict)
                    else ""
                )
                if owner_scope_id and owner_scope_id in set(
                    self._port.writable_scope_ids()
                ):
                    feedback_scope_id = owner_scope_id
            payload = record_playbook_feedback(
                conn,
                playbook_id=playbook_id,
                scope_id=feedback_scope_id,
                outcome=outcome,
                accessible_scope_ids=self._port.accessible_scope_ids(),
                decision=decision,
                evidence=evidence,
                preconditions_checked=preconditions_checked,
                steps_completed=steps_completed,
                outcome_reason=self._port.clean_text(
                    str(args.get("outcome_reason") or "")
                ),
                model_name=str(args.get("model_name") or ""),
                tool_call_count=int(args.get("tool_call_count") or 0),
                token_estimate=int(args.get("token_estimate") or 0),
                run_id=str(args.get("run_id") or ""),
            )
        return self._json(payload)

    def _handle_playbook_review(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_playbook_review requires maintenance_tools_enabled=true"
            )
        action = str(args.get("action") or "").strip().lower()
        if action in {"dedupe", "duplicates", "list_duplicates"}:
            with self._port.query_lock():
                groups = find_duplicate_playbooks(
                    self._port.query_connection(),
                    accessible_scope_ids=self._port.accessible_scope_ids(),
                    owner_scope_aliases=self._playbook_owner_scope_aliases(),
                    status=str(args.get("status") or ""),
                    limit=self._limit(args),
                )
            return self._json(
                {"action": "dedupe", "count": len(groups), "groups": groups}
            )
        playbook_id = str(args.get("id") or args.get("target_id") or "").strip()
        if not playbook_id:
            return tool_error("id is required")
        validated_payload = args.get("validated_payload")
        if not isinstance(validated_payload, dict):
            validated_payload = None
        if action == "merge":
            raw_source_ids = args.get("source_ids") or []
            source_ids = (
                raw_source_ids
                if isinstance(raw_source_ids, list)
                else [str(raw_source_ids)]
            )
            with self._port.query_lock():
                payload = merge_playbooks(
                    self._port.query_connection(),
                    target_id=playbook_id,
                    source_ids=source_ids,
                    accessible_scope_ids=self._port.accessible_scope_ids(),
                    owner_scope_aliases=self._playbook_owner_scope_aliases(),
                    reason=self._port.clean_text(str(args.get("reason") or "")),
                    dry_run=self._bool_arg(args, "dry_run", True),
                    force_cross_class=self._bool_arg(args, "force_cross_class", False),
                    validated_payload=validated_payload,
                )
            return self._json(payload)
        with self._port.query_lock():
            payload = review_playbook(
                self._port.query_connection(),
                playbook_id=playbook_id,
                accessible_scope_ids=self._port.accessible_scope_ids(),
                owner_scope_aliases=self._playbook_owner_scope_aliases(),
                action=action,
                reason=self._port.clean_text(str(args.get("reason") or "")),
                superseded_by=str(args.get("superseded_by") or ""),
                dry_run=self._bool_arg(args, "dry_run", True),
                force_cross_class=self._bool_arg(args, "force_cross_class", False),
                validated_payload=validated_payload,
            )
        return self._json(payload)

    def _handle_experience_stats(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        del args
        with self._port.query_lock():
            payload = experience_stats(
                self._port.query_connection(),
                accessible_scope_ids=self._port.accessible_scope_ids(),
            )
        return self._json(payload)

    def _handle_experience_promote(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error(
                "scope_recall_experience_promote requires maintenance_tools_enabled=true"
            )
        limit_sessions = max(1, min(100, int(args.get("limit_sessions") or 20)))
        with self._port.query_lock():
            payload = promote_experiences(
                self._port.query_connection(),
                accessible_scope_ids=self._port.accessible_scope_ids(),
                scope_id=self._playbook_scope_id(),
                shared_scope_id=self._playbook_shared_scope_id(),
                config=self._port.config_view(),
                limit_sessions=limit_sessions,
                dry_run=self._bool_arg(args, "dry_run", True),
            )
        return self._json(payload)

    def _clean_query(self, args: dict[str, Any]) -> str:
        return self._port.normalize_query(
            str(args.get("query") or ""),
            int(self._port.config_value("query_char_limit", 1000)),
        )

    def _query_variants(self, args: dict[str, Any]) -> list[str]:
        raw = args.get("query_variants")
        if not isinstance(raw, list):
            return []
        variants: list[str] = []
        seen: set[str] = set()
        for value in raw[:7]:
            query = self._port.normalize_query(str(value or ""), 1000).strip()
            normalized = query.casefold()
            if not query or normalized in seen:
                continue
            seen.add(normalized)
            variants.append(query)
        return variants

    def _limit(self, args: dict[str, Any]) -> int:
        return max(1, min(20, int(args.get("limit") or 5)))

    def _retrieval_limit(self, args: dict[str, Any]) -> int:
        if args.get("limit") is None:
            default_limit = (self._port.retrieval_config_view() or {}).get(
                "top_k"
            ) or 5
        else:
            default_limit = args.get("limit")
        return max(1, min(50, int(default_limit or 5)))

    def _evidence_diversity_depth(self, args: dict[str, Any]) -> int:
        """Return the bounded per-query protection depth for evidence fusion."""

        return max(
            1,
            min(
                MAX_EVIDENCE_DIVERSITY_DEPTH,
                int(
                    args.get("evidence_diversity_depth")
                    or DEFAULT_EVIDENCE_DIVERSITY_DEPTH
                ),
            ),
        )

    def _targets_arg(self, args: dict[str, Any]) -> list[str] | None:
        raw_targets = args.get("targets")
        if raw_targets is None:
            return None
        if isinstance(raw_targets, str):
            candidates = [item.strip() for item in raw_targets.split(",")]
        elif isinstance(raw_targets, list):
            candidates = [str(item).strip() for item in raw_targets]
        else:
            candidates = []
        allowed = {"user", "memory", "project", "ops", "general"}
        output: list[str] = []
        for target in candidates:
            normalized = target.lower()
            if normalized in allowed and normalized not in output:
                output.append(normalized)
        return output or None

    def _bool_arg(self, args: dict[str, Any], key: str, default: bool) -> bool:
        value = args.get(key, default)
        if isinstance(value, str):
            if default:
                return value.strip().lower() not in {"0", "false", "no", "off"}
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _operator_mode_enabled(self) -> bool:
        return config_bool(self._port.config_view(), "maintenance_tools_enabled", False)

    def _memory_ids_arg(self, args: dict[str, Any]) -> list[str]:
        raw_ids = args.get("ids")
        if raw_ids is None:
            raw_ids = args.get("id")
        if isinstance(raw_ids, str):
            candidates = [raw_ids]
            field = "id"
        elif isinstance(raw_ids, list):
            if len(raw_ids) > MAX_MEMORY_IDS_PER_REQUEST:
                raise _MemoryIdsArgumentError(
                    f"ids must contain at most {MAX_MEMORY_IDS_PER_REQUEST} items",
                    field="ids",
                    constraint=f"maxItems={MAX_MEMORY_IDS_PER_REQUEST}",
                )
            candidates = [str(item) for item in raw_ids]
            field = "ids"
        else:
            candidates = []
            field = "ids"
        ids: list[str] = []
        seen: set[str] = set()
        for memory_id in candidates:
            memory_id = str(memory_id or "").strip()
            if len(memory_id) > MAX_MEMORY_ID_LENGTH:
                raise _MemoryIdsArgumentError(
                    f"memory id must not exceed {MAX_MEMORY_ID_LENGTH} characters",
                    field=field,
                    constraint=f"maxLength={MAX_MEMORY_ID_LENGTH}",
                )
            if not memory_id or memory_id.startswith("curated:") or memory_id in seen:
                continue
            seen.add(memory_id)
            ids.append(memory_id)
        return ids

    def _storage_filter(self, content: str) -> CaptureFilterResult:
        return should_capture_text(content, self._port.config_view())

    def _serialize_recall_item(self, item: Any) -> dict[str, Any]:
        metadata = item.metadata or {}
        candidate_diagnostics = self._public_temporal_candidate_diagnostics(
            metadata.get("temporal_candidate_diagnostics")
        )
        return {
            "id": item.id,
            "content": item.content,
            "summary": item.summary,
            "source": item.source,
            "target": item.target,
            "score": round(item.score, 4),
            "base_score": self._rounded_metadata(metadata, "base_score"),
            "recency_bonus": self._rounded_metadata(metadata, "recency_bonus"),
            "lexical_score": self._rounded_metadata(metadata, "lexical_score"),
            "vector_score": self._rounded_metadata(metadata, "vector_score"),
            "bm25_score": self._rounded_metadata(metadata, "bm25_score"),
            "memory_type": str(metadata.get("memory_type") or ""),
            "trust": self._rounded_metadata(metadata, "trust"),
            "importance": self._rounded_metadata(metadata, "importance"),
            "evidence_rrf_score": self._rounded_metadata(
                metadata, "evidence_rrf_score"
            ),
            "evidence_query_hits": int(
                metadata.get("evidence_query_hits") or 0
            ),
            "evidence_query_ranks": metadata.get("evidence_query_ranks")
            if isinstance(metadata.get("evidence_query_ranks"), dict)
            else {},
            "entities": metadata.get("entities")
            if isinstance(metadata.get("entities"), list)
            else [],
            "fact_freshness_status": str(
                metadata.get("fact_freshness_status") or "untracked"
            ),
            "needs_live_check": bool(metadata.get("needs_live_check", False)),
            "fact_freshness_penalty": self._rounded_metadata(
                metadata, "fact_freshness_penalty"
            ),
            "freshness_warning": str(metadata.get("freshness_warning") or ""),
            "ranking_warning": str(metadata.get("ranking_warning") or ""),
            "temporal_candidate_diagnostics": candidate_diagnostics,
        }

    @staticmethod
    def _public_temporal_candidate_diagnostics(raw: Any) -> dict[str, Any]:
        """Return bounded diagnostics without exposing raw query tokens."""

        if not isinstance(raw, dict):
            return {}
        return {
            key: raw[key]
            for key in (
                "strategy",
                "candidate_limit",
                "candidate_count",
                "raw_unique_candidate_count",
                "truncated",
                "complete",
                "route_candidate_counts",
                "token_count",
                "covered_token_count",
                "token_coverage_complete",
            )
            if key in raw
        }

    def _store_metadata(self, args: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if args.get("memory_type"):
            metadata["memory_type"] = str(args.get("memory_type"))
        if args.get("importance") is not None:
            metadata["importance"] = clamp_float(args.get("importance"), default=0.5)
        freshness = args.get("freshness")
        if isinstance(freshness, dict):
            allowed_freshness_keys = {
                "fact_key",
                "truth_type",
                "validator_kind",
                "validator_spec",
                "ttl_days",
                "last_checked_at",
                "valid_until",
                "status",
                "stale_reason",
                "superseded_by",
            }
            metadata["freshness"] = {
                str(key): value
                for key, value in freshness.items()
                if str(key) in allowed_freshness_keys
            }
            validator_spec = metadata["freshness"].get("validator_spec")
            if isinstance(validator_spec, dict):
                metadata["freshness"]["validator_spec"] = sanitize_structured_value(
                    validator_spec
                )[0]
        for key in ("entities", "tags"):
            value = args.get(key)
            if isinstance(value, str):
                values = [item.strip() for item in value.split(",")]
            elif isinstance(value, list):
                values = [str(item).strip() for item in value]
            else:
                values = []
            values = [item for item in values if item]
            if values:
                metadata[key] = values
        return metadata

    @staticmethod
    def _rounded_metadata(metadata: dict[str, Any], key: str) -> float:
        return round(float(metadata.get(key) or 0.0), 4)

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False)
