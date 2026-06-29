from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from tools.registry import tool_error

from .capture_filters import CaptureFilterResult, sanitize_report_text, should_capture_text
from .gating import config_bool
from .graph import clamp_float
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
from .forgetting import build_forgetting_report, run_forgetting
from .secret_index import build_secret_index
from .vector_runtime import mark_vector_needs_repair

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

TOOL_ALIASES = {
    "lancepro_store": "scope_recall_store",
    "lancepro_search": "scope_recall_search",
    "lancepro_stats": "scope_recall_stats",
}


class ScopeRecallToolService:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def normalize_tool_name(self, tool_name: str) -> str:
        return TOOL_ALIASES.get(tool_name, tool_name)

    def handle(self, tool_name: str, args: dict[str, Any]) -> str:
        normalized = self.normalize_tool_name(tool_name)
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
        if handler is None:
            return tool_error(f"unknown scope-recall tool: {tool_name}")
        try:
            return handler(args)
        except Exception as exc:
            safe_error = sanitize_report_text(str(exc))
            logger.warning("Scope Recall tool %s failed: %s", tool_name, safe_error)
            return tool_error(safe_error)

    def _receipt(self, action: str, *, target: str = "", id: str = "", scope_mode: str = "", **extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"action": action, "provider": "scope-recall", "at": _now_iso()}
        if target:
            data["target"] = target
        if id:
            data["id"] = id
        if scope_mode:
            data["scope_mode"] = scope_mode
        data.update({key: value for key, value in extra.items() if value not in (None, "", [])})
        return data

    def _handle_store(self, args: dict[str, Any]) -> str:
        content = self.provider._clean_text(str(args.get("content") or ""))
        if not content:
            return tool_error("content is required")
        target = str(args.get("target") or "memory").strip().lower()
        requested_scope_mode = str(args.get("scope_mode") or "").strip().lower().replace("-", "_")
        if requested_scope_mode in {"shared", "local", "shared_pool"}:
            scope_mode = requested_scope_mode
        else:
            scope_mode = self.provider._scope_mode_for(target, "tool-store")
        if scope_mode == "shared_pool":
            shared_pool_config = self.provider._config.get("shared_pool") if isinstance(self.provider._config, dict) else {}
            shared_pool_config = shared_pool_config if isinstance(shared_pool_config, dict) else {}
            allowed_targets = shared_pool_config.get("allowed_targets")
            allowed_target_set = {"memory", "project", "ops"}
            if isinstance(allowed_targets, list):
                configured = {str(item).strip().lower() for item in allowed_targets if str(item).strip()}
                if configured:
                    allowed_target_set = configured
            if not getattr(self.provider, "_shared_pool_enabled", False):
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
                        "receipt": self._receipt("shared_pool_write_rejected", target=target, scope_mode=scope_mode, reason="shared_pool_disabled"),
                    }
                )
            if not getattr(self.provider, "_shared_pool_write_enabled", False):
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
                        "receipt": self._receipt("shared_pool_write_rejected", target=target, scope_mode=scope_mode, reason="shared_pool_write_disabled"),
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
                        "receipt": self._receipt("shared_pool_write_rejected", target=target, scope_mode=scope_mode, reason="shared_pool_target_not_allowed"),
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
                    "receipt": self._receipt("rejected_sensitive", target=target, scope_mode=scope_mode, reason=filter_result.reason),
                }
            )
        memory_id, inserted, outcome = self.provider._store_now(
            content=content,
            source="tool-store",
            target=target,
            session_id=self.provider._session_id,
            metadata=self._store_metadata(args),
            scope_mode=scope_mode,
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
                "receipt": self._receipt("promoted" if inserted else outcome, target=target, id=memory_id, scope_mode=scope_mode),
            }
        )

    def _handle_store_secret_index(self, args: dict[str, Any]) -> str:
        if not config_bool(self.provider._config, "secret_index_tools_enabled", False):
            return tool_error("scope_recall_store_secret_index requires secret_index_tools_enabled=true")
        content, metadata = build_secret_index(args)
        target = str(args.get("target") or "ops").strip().lower()
        if target not in {"memory", "project", "ops"}:
            target = "ops"
        scope_mode = self.provider._scope_mode_for(target, "secret-index")
        filter_result = self._storage_filter(content)
        if not filter_result.allowed:
            return tool_error(
                "secret index content is not suitable for storage after redaction",
                skipped=True,
                skip_reason=filter_result.reason,
                receipt=self._receipt("rejected_sensitive", target=target, scope_mode=scope_mode, reason=filter_result.reason),
            )
        memory_id, inserted, outcome = self.provider._store_now(
            content=content,
            source="secret-index",
            target=target,
            session_id=self.provider._session_id,
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
        results = self.provider._recall_service.search_memories(query, limit=limit)
        payload: dict[str, Any] = {
            "count": len(results),
            "results": [self._serialize_recall_item(item) for item in results],
        }
        if self._bool_arg(args, "include_trace", False):
            payload["funnel_trace"] = dict(getattr(self.provider._recall_service, "last_funnel_trace", {}) or {})
        return self._json(payload)

    def _handle_context(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        return self._json(
            self.provider._context_payload(
                query=query,
                limit=self._limit(args),
                max_chars=max(120, min(4000, int(args.get("max_chars") or 900))),
            )
        )

    def _handle_profile(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args) if args.get("query") else ""
        entity = str(args.get("entity") or "").strip()
        return self._json(
            self.provider._profile_payload(
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
        return self._json(self.provider._probe_entity(entity=entity, limit=self._limit(args)))

    def _handle_related(self, args: dict[str, Any]) -> str:
        entity = str(args.get("entity") or "").strip()
        if not entity:
            return tool_error("entity is required")
        return self._json(self.provider._related_entities(entity=entity, limit=self._limit(args)))

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
        return tool_error("action must be one of: inspect, feedback, update, merge, forget")

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
            self.provider._feedback_memory(
                memory_id=memory_id,
                rating=rating,
                note=self.provider._clean_text(str(args.get("note") or "")),
            )
        )

    def _handle_forget(self, args: dict[str, Any]) -> str:
        ids = self._memory_ids_arg(args)
        if not ids:
            return tool_error("ids are required for scope_recall_forget; search or inspect first, then pass exact ids")
        reason = self.provider._clean_text(str(args.get("reason") or "scope_recall_forget"))
        if self._bool_arg(args, "hard_delete", False):
            if not self._operator_mode_enabled():
                return tool_error("scope_recall_forget hard_delete requires maintenance_tools_enabled=true")
            deleted = self.provider._delete_memories(ids)
            return self._json({"archived": 0, "deleted": deleted, "ids": ids, "hard_delete": True, "receipt": self._receipt("hard_delete", reason=reason)})
        return self._json(self.provider._archive_memories(ids, reason=reason, actor="scope_recall_forget"))

    def _handle_update(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        content = self.provider._clean_text(str(args.get("content") or ""))
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
                receipt=self._receipt("rejected_sensitive", reason=filter_result.reason),
            )
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        updated, summary, updated_at = self.provider._update_memory(memory_id, content, target)
        if not updated:
            return tool_error("id not found")
        row = self.provider._require_conn().execute(
            "SELECT source, target, scope_id FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        actual_target = str(row["target"]) if row is not None else (target or "")
        source = str(row["source"]) if row is not None else ""
        if row is not None and str(row["scope_id"]) == str(getattr(self.provider, "_shared_pool_scope_id", "") or ""):
            scope_mode = "shared_pool"
        else:
            scope_mode = self.provider._scope_mode_for(actual_target, source)
        return self._json(
            {
                "updated": True,
                "id": memory_id,
                "target": actual_target,
                "scope_mode": scope_mode,
                "summary": summary,
                "updated_at": updated_at,
                "receipt": self._receipt("updated", target=actual_target, id=memory_id, scope_mode=scope_mode),
            }
        )

    def _handle_dedupe(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_dedupe requires maintenance_tools_enabled=true")
        scope_only = self._bool_arg(args, "scope_only", True)
        return self._json(
            self.provider._dedupe_memories(
                dry_run=self._bool_arg(args, "dry_run", True),
                scope_only=scope_only,
            )
        )

    def _handle_merge(self, args: dict[str, Any]) -> str:
        target_id = str(args.get("target_id") or "").strip()
        if not target_id:
            return tool_error("target_id is required")
        source_ids = args.get("source_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        content_arg = args.get("content")
        content = self.provider._clean_text(str(content_arg)) if content_arg else None
        if content is not None:
            filter_result = self._storage_filter(content)
            if not filter_result.allowed:
                return tool_error(
                    "content is not suitable for storage",
                    skipped=True,
                    skip_reason=filter_result.reason,
                    receipt=self._receipt("rejected_sensitive", reason=filter_result.reason),
                )
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        payload = self.provider._merge_memories(target_id, [str(item) for item in source_ids], content, target)
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
            return tool_error("scope_only=false requires maintenance_tools_enabled=true")
        return self._json(
            self.provider._export_memories(
                fmt=str(args.get("format") or "jsonl"),
                scope_only=scope_only,
            )
        )

    def _handle_govern(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_govern requires maintenance_tools_enabled=true")
        scope_only = self._bool_arg(args, "scope_only", True)
        return self._json(
            self.provider._govern_memories(
                dry_run=self._bool_arg(args, "dry_run", True),
                scope_only=scope_only,
            )
        )

    def _handle_repair(self, args: dict[str, Any]) -> str:
        del args
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_repair requires maintenance_tools_enabled=true")
        return self._json(self.provider._repair_vector())

    def _handle_hygiene(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_hygiene requires maintenance_tools_enabled=true")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        return self._json(self.provider._hygiene_report(limit=limit))

    def _handle_forgetting_report(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_forgetting_report requires maintenance_tools_enabled=true")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        with self.provider._lock:
            payload = build_forgetting_report(
                self.provider._require_conn(),
                accessible_scope_ids=self.provider._accessible_scope_ids,
                limit=limit,
            )
        return self._json(payload)

    def _handle_forgetting_run(self, args: dict[str, Any]) -> str:
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_forgetting_run requires maintenance_tools_enabled=true")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        with self.provider._lock:
            payload = run_forgetting(
                self.provider._require_conn(),
                accessible_scope_ids=self.provider._writable_scope_ids,
                dry_run=self._bool_arg(args, "dry_run", True),
                hard_delete=self._bool_arg(args, "hard_delete", False),
                limit=limit,
                vector_store=self.provider._vector_store,
            )
        if payload.get("vector_error"):
            mark_vector_needs_repair(self.provider, str(payload.get("vector_error") or "forgetting vector delete failed"))
        return self._json(payload)

    def _handle_stats(self, args: dict[str, Any]) -> str:
        del args
        return self._json(self.provider._stats_payload())

    def _handle_inspect(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        if not memory_id:
            return tool_error("id is required")
        return self._json(self.provider._inspect_memory(memory_id=memory_id))

    def _handle_explain(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        return self._json(self.provider._explain_query(query=query, limit=self._retrieval_limit(args)))

    def _handle_benchmark(self, args: dict[str, Any]) -> str:
        char_limit = int(self.provider._config_value("query_char_limit", 1000))
        raw_cases = args.get("cases") or []
        cases: list[dict[str, Any]] = []
        if isinstance(raw_cases, list):
            for raw_case in raw_cases:
                if not isinstance(raw_case, dict):
                    continue
                query = self.provider._normalize_query(str(raw_case.get("query") or ""), char_limit)
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
        queries = [self.provider._normalize_query(query, char_limit) for query in queries]
        queries = [query for query in queries if query]
        if not queries and not cases:
            return tool_error("queries or cases is required")
        return self._json(
            self.provider._benchmark_queries(
                queries=queries,
                cases=cases,
                limit=self._retrieval_limit(args),
                auto_explain_on_fail=self._bool_arg(args, "auto_explain_on_fail", False),
                include_trace=self._bool_arg(args, "include_trace", False),
                prompt_budget_chars=max(0, int(args.get("prompt_budget_chars") or 0)),
            )
        )

    def _playbook_scope_id(self) -> str:
        return str(getattr(self.provider, "_shared_scope_id", "") or getattr(self.provider, "_scope_id", ""))

    def _playbook_shared_scope_id(self) -> str:
        return str(getattr(self.provider, "_shared_pool_scope_id", "") or "")

    def _experience_enabled(self) -> bool:
        raw_config = self.provider._config.get("experience") if isinstance(self.provider._config, dict) else {}
        config = dict(raw_config) if isinstance(raw_config, dict) else {}
        return config_bool(config, "enabled", True)

    def _experience_disabled_error(self) -> str:
        return tool_error("Experience Kernel is disabled")

    def _handle_playbook_create(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_playbook_create requires maintenance_tools_enabled=true")
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
        with self.provider._lock:
            playbook = create_playbook(
                self.provider._require_conn(),
                playbook_id=str(args.get("id") or "").strip() or None,
                scope_id=self._playbook_scope_id(),
                shared_scope_id=self._playbook_shared_scope_id(),
                payload=payload,
                status=str(args.get("status") or "candidate"),
                confidence=confidence_value,
                created_from_episode_id=str(args.get("created_from_episode_id") or ""),
                evidence_anchors=args.get("evidence_anchors") if isinstance(args.get("evidence_anchors"), list) else [],
                related_skills=args.get("related_skills") if isinstance(args.get("related_skills"), list) else [],
                environment_constraints=args.get("environment_constraints") if isinstance(args.get("environment_constraints"), dict) else {},
                metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
            )
        return self._json({"created": True, "playbook": playbook})

    def _handle_playbook_search(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        query = self._clean_query(args) if args.get("query") else ""
        with self.provider._lock:
            results = search_playbooks(
                self.provider._require_conn(),
                query=query,
                accessible_scope_ids=self.provider._accessible_scope_ids,
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
        with self.provider._lock:
            payload = inspect_playbook(
                self.provider._require_conn(),
                playbook_id=playbook_id,
                accessible_scope_ids=self.provider._accessible_scope_ids,
            )
        return self._json(payload)

    def _handle_experience_preflight(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        with self.provider._lock:
            payload = experience_preflight(
                self.provider._require_conn(),
                query=query,
                accessible_scope_ids=self.provider._accessible_scope_ids,
                config=self.provider._config,
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
        raw_evidence = args.get("evidence") or []
        evidence = raw_evidence if isinstance(raw_evidence, list) else [str(raw_evidence)]
        raw_preconditions = args.get("preconditions_checked") or []
        preconditions_checked = raw_preconditions if isinstance(raw_preconditions, list) else [str(raw_preconditions)]
        raw_steps = args.get("steps_completed") or []
        steps_completed = raw_steps if isinstance(raw_steps, list) else [str(raw_steps)]
        with self.provider._lock:
            conn = self.provider._require_conn()
            feedback_scope_id = self._playbook_scope_id()
            inspected = inspect_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=self.provider._accessible_scope_ids)
            if inspected.get("found"):
                playbook = inspected.get("playbook") if isinstance(inspected.get("playbook"), dict) else {}
                owner_scope_id = str(playbook.get("scope_id") or "") if isinstance(playbook, dict) else ""
                if owner_scope_id and owner_scope_id in set(getattr(self.provider, "_writable_scope_ids", []) or []):
                    feedback_scope_id = owner_scope_id
            payload = record_playbook_feedback(
                conn,
                playbook_id=playbook_id,
                scope_id=feedback_scope_id,
                outcome=outcome,
                accessible_scope_ids=self.provider._accessible_scope_ids,
                decision=str(args.get("decision") or "guided_reuse"),
                evidence=evidence,
                preconditions_checked=preconditions_checked,
                steps_completed=steps_completed,
                outcome_reason=self.provider._clean_text(str(args.get("outcome_reason") or "")),
                model_name=str(args.get("model_name") or ""),
                tool_call_count=int(args.get("tool_call_count") or 0),
                token_estimate=int(args.get("token_estimate") or 0),
            )
        return self._json(payload)

    def _handle_playbook_review(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_playbook_review requires maintenance_tools_enabled=true")
        action = str(args.get("action") or "").strip().lower()
        if action in {"dedupe", "duplicates", "list_duplicates"}:
            with self.provider._lock:
                groups = find_duplicate_playbooks(
                    self.provider._require_conn(),
                    accessible_scope_ids=self.provider._accessible_scope_ids,
                    status=str(args.get("status") or ""),
                    limit=self._limit(args),
                )
            return self._json({"action": "dedupe", "count": len(groups), "groups": groups})
        playbook_id = str(args.get("id") or args.get("target_id") or "").strip()
        if not playbook_id:
            return tool_error("id is required")
        if action == "merge":
            raw_source_ids = args.get("source_ids") or []
            source_ids = raw_source_ids if isinstance(raw_source_ids, list) else [str(raw_source_ids)]
            with self.provider._lock:
                payload = merge_playbooks(
                    self.provider._require_conn(),
                    target_id=playbook_id,
                    source_ids=source_ids,
                    accessible_scope_ids=self.provider._accessible_scope_ids,
                    reason=self.provider._clean_text(str(args.get("reason") or "")),
                    dry_run=self._bool_arg(args, "dry_run", True),
                )
            return self._json(payload)
        with self.provider._lock:
            payload = review_playbook(
                self.provider._require_conn(),
                playbook_id=playbook_id,
                accessible_scope_ids=self.provider._accessible_scope_ids,
                action=action,
                reason=self.provider._clean_text(str(args.get("reason") or "")),
                superseded_by=str(args.get("superseded_by") or ""),
            )
        return self._json(payload)

    def _handle_experience_stats(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        del args
        with self.provider._lock:
            payload = experience_stats(self.provider._require_conn(), accessible_scope_ids=self.provider._accessible_scope_ids)
        return self._json(payload)

    def _handle_experience_promote(self, args: dict[str, Any]) -> str:
        if not self._experience_enabled():
            return self._experience_disabled_error()
        if not self._operator_mode_enabled():
            return tool_error("scope_recall_experience_promote requires maintenance_tools_enabled=true")
        limit_sessions = max(1, min(100, int(args.get("limit_sessions") or 20)))
        with self.provider._lock:
            payload = promote_experiences(
                self.provider._require_conn(),
                accessible_scope_ids=self.provider._accessible_scope_ids,
                scope_id=self._playbook_scope_id(),
                shared_scope_id=self._playbook_shared_scope_id(),
                config=self.provider._config,
                limit_sessions=limit_sessions,
                dry_run=self._bool_arg(args, "dry_run", True),
            )
        return self._json(payload)

    def _clean_query(self, args: dict[str, Any]) -> str:
        return self.provider._normalize_query(
            str(args.get("query") or ""),
            int(self.provider._config_value("query_char_limit", 1000)),
        )

    def _limit(self, args: dict[str, Any]) -> int:
        return max(1, min(20, int(args.get("limit") or 5)))

    def _retrieval_limit(self, args: dict[str, Any]) -> int:
        if args.get("limit") is None:
            default_limit = (getattr(self.provider, "_retrieval_config", {}) or {}).get("top_k") or 5
        else:
            default_limit = args.get("limit")
        return max(1, min(20, int(default_limit or 5)))

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
        return config_bool(self.provider._config, "maintenance_tools_enabled", False)

    def _memory_ids_arg(self, args: dict[str, Any]) -> list[str]:
        raw_ids = args.get("ids")
        if raw_ids is None:
            raw_ids = args.get("id")
        if isinstance(raw_ids, str):
            candidates = [raw_ids]
        elif isinstance(raw_ids, list):
            candidates = [str(item) for item in raw_ids]
        else:
            candidates = []
        ids: list[str] = []
        seen: set[str] = set()
        for memory_id in candidates:
            memory_id = str(memory_id or "").strip()
            if not memory_id or memory_id.startswith("curated:") or memory_id in seen:
                continue
            seen.add(memory_id)
            ids.append(memory_id)
        return ids

    def _storage_filter(self, content: str) -> CaptureFilterResult:
        return should_capture_text(content, self.provider._config)

    def _serialize_recall_item(self, item: Any) -> dict[str, Any]:
        metadata = item.metadata or {}
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
            "entities": metadata.get("entities") if isinstance(metadata.get("entities"), list) else [],
        }

    def _store_metadata(self, args: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if args.get("memory_type"):
            metadata["memory_type"] = str(args.get("memory_type"))
        if args.get("importance") is not None:
            metadata["importance"] = clamp_float(args.get("importance"), default=0.5)
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
