from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .capture import enqueue_store, flush_writer, shutdown_writer, start_writer, store_now
from .config import load_runtime_config, save_runtime_config
from .embedders import BaseEmbedder
from .gating import clean_text, compact_text, config_bool, dedup_key, normalize_query, should_skip_retrieval
from .migration import migrate_legacy_scope_recall_storage
from .models import RecallItem, RuntimeScope
from .recall import RecallService
from .schemas import (
    SCOPE_RECALL_SEARCH_SCHEMA,
    SCOPE_RECALL_STATS_SCHEMA,
    SCOPE_RECALL_STORE_SCHEMA,
)
from .scope import build_scope_id
from .sql_store import ensure_schema, iter_curated_entries
from .storage_views import search_curated_memories, search_db_memories, search_vector_memories
from .tooling import ScopeRecallToolService
from .vector_runtime import setup_vector_layer
from .vector_store import LanceVectorStore

logger = logging.getLogger(__name__)


class ScopeRecallMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._retrieval_config: dict[str, Any] = {}
        self._vector_config: dict[str, Any] = {}
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._write_queue: queue.Queue[Any] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._session_id = ""
        self._current_turn = 0
        self._scope = RuntimeScope()
        self._scope_id = ""
        self._storage_dir: Path | None = None
        self._db_path: Path | None = None
        self._hermes_home: Path | None = None
        self._plugin_dir = Path(__file__).resolve().parent
        self._last_recall_turns: dict[str, int] = {}
        self._embedder: BaseEmbedder | None = None
        self._vector_store: LanceVectorStore | None = None
        self._vector_enabled = False
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._vector_backend = "lancedb"
        self._migration_info: dict[str, Any] = {"migrated": False}
        self._recall_service = RecallService(self)
        self._tool_service = ScopeRecallToolService(self)

    @property
    def name(self) -> str:
        return "scope-recall"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "auto_recall",
                "description": "Enable current-turn memory recall",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "auto_capture",
                "description": "Capture turns into local memory",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "vector.enabled",
                "description": "Enable LanceDB vector companion layer",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "vector.embedder.provider",
                "description": "Embedding backend for the vector layer (API or local model)",
                "default": "openai-compatible",
                "choices": ["openai-compatible", "openai", "sentence-transformers", "local-hash"],
            },
            {
                "key": "vector.embedder.model",
                "description": "Embedding model name for the selected vector backend",
                "default": "gemini-embedding-001",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        save_runtime_config(values or {}, hermes_home)

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = Path(kwargs.get("hermes_home") or "~/.hermes").expanduser()
        self._hermes_home = hermes_home
        self._storage_dir = hermes_home / "scope-recall"
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._migration_info = migrate_legacy_scope_recall_storage(self._hermes_home, self._storage_dir)
        self._db_path = self._storage_dir / "memory.sqlite3"
        self._config = load_runtime_config(self._plugin_dir, self._storage_dir)
        self._retrieval_config = dict(self._config.get("retrieval") or {})
        self._vector_config = dict(self._config.get("vector") or {})

        self._session_id = session_id
        self._scope = RuntimeScope(
            platform=str(kwargs.get("platform") or "cli"),
            user_id=str(kwargs.get("user_id") or ""),
            chat_id=str(kwargs.get("chat_id") or ""),
            thread_id=str(kwargs.get("thread_id") or ""),
            gateway_session_key=str(kwargs.get("gateway_session_key") or ""),
            agent_identity=str(kwargs.get("agent_identity") or ""),
            agent_workspace=str(kwargs.get("agent_workspace") or ""),
            agent_context=str(kwargs.get("agent_context") or "primary"),
        )
        self._scope_id = build_scope_id(self._scope)
        self._current_turn = 0
        self._last_recall_turns = {}

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        ensure_schema(self._conn)
        setup_vector_layer(self)
        start_writer(self)

    def system_prompt_block(self) -> str:
        suffix = ""
        if self._vector_enabled and self._vector_ready:
            suffix = " Hybrid lexical+vector recall is enabled with a local LanceDB companion index."
        elif self._vector_enabled and not self._vector_ready:
            suffix = f" Vector companion requested but not active ({self._vector_message or self._vector_status})."
        return (
            "# Scope Recall Memory\n"
            "Active. Uses current-turn local recall with conservative gating and strong scope isolation."
            " Built-in curated memory files are read live at recall time, and previous-turn prefetched memory is never injected into a new topic."
            + suffix
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        del message, kwargs
        self._current_turn = int(turn_number or 0)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        del query, session_id
        return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        del session_id
        if not config_bool(self._config, "auto_recall", True):
            return ""
        if self._scope.agent_context != "primary":
            return ""

        query = self._normalize_query(query, int(self._config_value("query_char_limit", 1000)))
        if should_skip_retrieval(query, int(self._config_value("auto_recall_min_length", 15))):
            return ""

        results = self._recall_service.search_memories(query, limit=self._retrieve_limit())
        if not results:
            return ""

        min_repeated = int(self._config_value("auto_recall_min_repeated", 8))
        if min_repeated > 0:
            filtered: list[RecallItem] = []
            for item in results:
                last_turn = self._last_recall_turns.get(item.id, 0)
                if last_turn and (self._current_turn - last_turn) < min_repeated:
                    continue
                filtered.append(item)
            results = filtered
        if not results:
            return ""

        max_items = min(
            int(self._config_value("auto_recall_max_items", 3)),
            int(self._config_value("max_recall_per_turn", 10)),
        )
        max_chars = int(self._config_value("auto_recall_max_chars", 600))
        per_item_chars = int(self._config_value("auto_recall_per_item_max_chars", 180))

        selected: list[RecallItem] = []
        used_chars = 0
        for item in results:
            if len(selected) >= max_items:
                break
            summary = compact_text(item.summary or item.content, per_item_chars)
            if not summary:
                continue
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            if len(summary) > remaining:
                summary = compact_text(summary, remaining)
            if not summary:
                continue
            selected.append(
                RecallItem(
                    id=item.id,
                    content=item.content,
                    summary=summary,
                    source=item.source,
                    target=item.target,
                    score=item.score,
                    updated_at=item.updated_at,
                    metadata=item.metadata or {},
                )
            )
            used_chars += len(summary)
        if not selected:
            return ""

        self._mark_recalled([item.id for item in selected])
        lines = [f"- [{item.target or item.source}] {item.summary}" for item in selected]
        return "## Scope Recall Relevant Memories\n" + "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        del session_id
        if not config_bool(self._config, "auto_capture", True):
            return
        if self._scope.agent_context != "primary":
            return

        clean_user = self._clean_text(user_content)
        clean_assistant = self._clean_text(assistant_content)
        min_capture = int(self._config_value("min_capture_length", 10))

        if len(clean_user) >= min_capture and not self._is_trivial(clean_user):
            enqueue_store(
                self,
                content=clean_user,
                source="turn-user",
                target="general",
                session_id=self._session_id,
            )
        if (
            config_bool(self._config, "capture_assistant", True)
            and len(clean_assistant) >= min_capture
            and not self._is_trivial(clean_assistant)
        ):
            enqueue_store(
                self,
                content=clean_assistant,
                source="turn-assistant",
                target="general",
                session_id=self._session_id,
            )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Observe Hermes curated-memory writes without mirroring them.

        Built-in ``memory`` writes remain authoritative in USER.md/MEMORY.md.
        Scope Recall reads those files live during recall, so copying them into
        the SQLite truth store here would create duplicate/stale entries after
        replace/remove operations. The hook is kept as an explicit no-op so
        Hermes can notify the provider without changing storage ownership.
        """
        del action, target, content, metadata
        if self._scope.agent_context != "primary":
            return
        return

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        del messages
        flush_writer(self, timeout=3.0)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        del parent_session_id, kwargs
        self._session_id = new_session_id
        if reset:
            self._current_turn = 0
            self._last_recall_turns = {}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not config_bool(self._config, "enable_tools", True):
            return []
        if self._scope.agent_context != "primary":
            return []
        return [SCOPE_RECALL_STORE_SCHEMA, SCOPE_RECALL_SEARCH_SCHEMA, SCOPE_RECALL_STATS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        del kwargs
        if self._scope.agent_context != "primary":
            from tools.registry import tool_error

            return tool_error("scope-recall tools are only available in the primary agent context")
        return self._tool_service.handle(tool_name, args)

    def shutdown(self) -> None:
        shutdown_writer(self, timeout=3.0)
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        if self._vector_store is not None:
            self._vector_store.close()

    def flush(self, timeout: float = 2.0) -> bool:
        return flush_writer(self, timeout=timeout)

    def _search_db_memories(self, query: str, *, limit: int) -> List[RecallItem]:
        return search_db_memories(self, query, limit=limit)

    def _store_now(
        self,
        *,
        content: str,
        source: str,
        target: str,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return store_now(
            self,
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata,
        )

    def _search_vector_memories(self, query: str, *, limit: int) -> List[RecallItem]:
        return search_vector_memories(self, query, limit=limit)

    def _search_curated_memories(self, query: str) -> List[RecallItem]:
        return search_curated_memories(self, query)

    def _mark_recalled(self, memory_ids: List[str]) -> None:
        for memory_id in memory_ids:
            self._last_recall_turns[memory_id] = self._current_turn

    def _stats_payload(self) -> Dict[str, Any]:
        conn = self._require_conn()
        with self._lock:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            scoped = conn.execute("SELECT COUNT(*) FROM memories WHERE scope_id = ?", (self._scope_id,)).fetchone()[0]
        vector_path = ""
        vector_table = ""
        vector_embedder: dict[str, Any] = {}
        if self._vector_store is not None:
            vector_path = str(self._vector_store.db_path)
            vector_table = self._vector_store.table_name
        if self._embedder is not None:
            vector_embedder = self._embedder.describe()
        return {
            "provider": self.name,
            "db_path": str(self._db_path) if self._db_path else "",
            "scope_id": self._scope_id,
            "total_memories": total,
            "scope_memories": scoped,
            "curated_memories": len(iter_curated_entries(self._hermes_home)),
            "migration": dict(self._migration_info),
            "vector": {
                "enabled": self._vector_enabled,
                "ready": self._vector_ready,
                "status": self._vector_status,
                "message": self._vector_message,
                "backend": self._vector_backend,
                "path": vector_path,
                "table": vector_table,
                "row_count": self._vector_row_count,
                "unique_id_count": self._vector_unique_id_count,
                "duplicate_row_count": self._vector_duplicate_row_count,
                "sync_mode": str((self._vector_config or {}).get("sync_mode") or "incremental"),
                "embedder": vector_embedder,
                "fallback_embedder": dict(((self._vector_config or {}).get("fallback_embedder") or {})),
            },
            "retrieval": {
                "mode": str((self._retrieval_config or {}).get("mode") or "lexical"),
                "lexical_weight": float((self._retrieval_config or {}).get("lexical_weight") or 1.0),
                "vector_weight": float((self._retrieval_config or {}).get("vector_weight") or 0.0),
            },
        }

    def _retrieve_limit(self) -> int:
        max_items = int(self._config_value("auto_recall_max_items", 3))
        max_per_turn = int(self._config_value("max_recall_per_turn", 10))
        return max(1, min(max_items * 3, max_per_turn * 2, 20))

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Scope Recall is not initialized")
        return self._conn

    def _config_value(self, key: str, default: Any) -> Any:
        return self._config.get(key, default)

    def _is_trivial(self, text: str) -> bool:
        return should_skip_retrieval(text, 0)

    def _vector_text(self, summary: str, content: str) -> str:
        return clean_text(f"{summary}\n{content}")

    def _clean_text(self, text: str) -> str:
        return clean_text(text)

    def _normalize_query(self, query: str, char_limit: int) -> str:
        return normalize_query(query, char_limit)

    def _dedup_key(self, content: str) -> str:
        return dedup_key(content)


def register(ctx) -> None:
    ctx.register_memory_provider(ScopeRecallMemoryProvider())
