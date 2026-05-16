from __future__ import annotations

import json
import logging
from typing import Any, Callable

from tools.registry import tool_error

from .gating import should_skip_capture

logger = logging.getLogger(__name__)

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
            "scope_recall_search": self._handle_search,
            "scope_recall_forget": self._handle_forget,
            "scope_recall_update": self._handle_update,
            "scope_recall_dedupe": self._handle_dedupe,
            "scope_recall_merge": self._handle_merge,
            "scope_recall_export": self._handle_export,
            "scope_recall_govern": self._handle_govern,
            "scope_recall_repair": self._handle_repair,
            "scope_recall_stats": self._handle_stats,
        }
        handler = handlers.get(normalized)
        if handler is None:
            return tool_error(f"unknown scope-recall tool: {tool_name}")
        try:
            return handler(args)
        except Exception as exc:
            logger.warning("Scope Recall tool %s failed: %s", tool_name, exc)
            return tool_error(str(exc))

    def _handle_store(self, args: dict[str, Any]) -> str:
        content = self.provider._clean_text(str(args.get("content") or ""))
        if not content:
            return tool_error("content is required")
        target = str(args.get("target") or "memory")
        if should_skip_capture(content, self.provider._config):
            return self._json({"stored": False, "skipped": True, "id": "", "target": target})
        memory_id, inserted, outcome = self.provider._store_now(
            content=content,
            source="tool-store",
            target=target,
            session_id=self.provider._session_id,
        )
        return self._json(
            {
                "stored": bool(inserted),
                "duplicate": outcome == "duplicate",
                "merged": outcome == "merged",
                "skipped": outcome == "skipped",
                "id": memory_id,
                "target": target,
                "scope_mode": self.provider._scope_mode_for(target, "tool-store"),
            }
        )

    def _handle_search(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        limit = self._limit(args)
        results = self.provider._recall_service.search_memories(query, limit=limit)
        return self._json(
            {
                "count": len(results),
                "results": [self._serialize_recall_item(item) for item in results],
            }
        )

    def _handle_forget(self, args: dict[str, Any]) -> str:
        query = self._clean_query(args)
        if not query:
            return tool_error("query is required")
        results = self.provider._recall_service.search_memories(query, limit=self._limit(args))
        ids = []
        seen_ids: set[str] = set()
        for item in results:
            if item.id.startswith("curated:") or item.id in seen_ids:
                continue
            seen_ids.add(item.id)
            ids.append(item.id)
        self.provider._delete_memories(ids)
        return self._json({"deleted": len(ids), "ids": ids})

    def _handle_update(self, args: dict[str, Any]) -> str:
        memory_id = str(args.get("id") or "").strip()
        content = self.provider._clean_text(str(args.get("content") or ""))
        if not memory_id:
            return tool_error("id is required")
        if not content:
            return tool_error("content is required")
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        updated, summary, updated_at = self.provider._update_memory(memory_id, content, target)
        if not updated:
            return tool_error("id not found")
        return self._json({"updated": True, "id": memory_id, "summary": summary, "updated_at": updated_at})

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
        if content is not None and should_skip_capture(content, self.provider._config):
            return tool_error("content is not suitable for storage")
        target_arg = args.get("target")
        target = str(target_arg) if target_arg else None
        return self._json(self.provider._merge_memories(target_id, [str(item) for item in source_ids], content, target))

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

    def _handle_stats(self, args: dict[str, Any]) -> str:
        del args
        return self._json(self.provider._stats_payload())

    def _clean_query(self, args: dict[str, Any]) -> str:
        return self.provider._normalize_query(
            str(args.get("query") or ""),
            int(self.provider._config_value("query_char_limit", 1000)),
        )

    def _limit(self, args: dict[str, Any]) -> int:
        return max(1, min(20, int(args.get("limit") or 5)))

    def _bool_arg(self, args: dict[str, Any], key: str, default: bool) -> bool:
        value = args.get(key, default)
        if isinstance(value, str):
            if default:
                return value.strip().lower() not in {"0", "false", "no", "off"}
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _operator_mode_enabled(self) -> bool:
        return bool(self.provider._config_value("maintenance_tools_enabled", False))

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
        }

    @staticmethod
    def _rounded_metadata(metadata: dict[str, Any], key: str) -> float:
        return round(float(metadata.get(key) or 0.0), 4)

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False)
