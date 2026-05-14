from __future__ import annotations

import json
import logging
from typing import Any

from tools.registry import tool_error

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
        try:
            if normalized == "scope_recall_store":
                content = self.provider._clean_text(str(args.get("content") or ""))
                if not content:
                    return tool_error("content is required")
                target = str(args.get("target") or "memory")
                memory_id = self.provider._store_now(
                    content=content,
                    source="tool-store",
                    target=target,
                    session_id=self.provider._session_id,
                )
                return json.dumps({"stored": True, "id": memory_id, "target": target}, ensure_ascii=False)

            if normalized == "scope_recall_search":
                query = self.provider._normalize_query(
                    str(args.get("query") or ""),
                    int(self.provider._config_value("query_char_limit", 1000)),
                )
                if not query:
                    return tool_error("query is required")
                limit = max(1, min(20, int(args.get("limit") or 5)))
                results = self.provider._recall_service.search_memories(query, limit=limit)
                payload = {
                    "count": len(results),
                    "results": [
                        {
                            "id": item.id,
                            "content": item.content,
                            "summary": item.summary,
                            "source": item.source,
                            "target": item.target,
                            "score": round(item.score, 4),
                            "base_score": round(float((item.metadata or {}).get("base_score") or 0.0), 4),
                            "recency_bonus": round(float((item.metadata or {}).get("recency_bonus") or 0.0), 4),
                            "lexical_score": round(float((item.metadata or {}).get("lexical_score") or 0.0), 4),
                            "vector_score": round(float((item.metadata or {}).get("vector_score") or 0.0), 4),
                        }
                        for item in results
                    ],
                }
                return json.dumps(payload, ensure_ascii=False)

            if normalized == "scope_recall_stats":
                return json.dumps(self.provider._stats_payload(), ensure_ascii=False)
        except Exception as exc:
            logger.warning("Scope Recall tool %s failed: %s", tool_name, exc)
            return tool_error(str(exc))
        return tool_error(f"unknown scope-recall tool: {tool_name}")
