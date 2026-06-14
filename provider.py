from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .capture import enqueue_store, flush_writer, shutdown_writer, start_writer
from .capture_filters import sanitize_capture_text, should_capture_text
from .capture_llm import extract_capture_candidates
from .config import load_runtime_config, save_runtime_config
from .journal import append_journal_entry, ensure_journal_schema, run_journal_digest
from .embedders import BaseEmbedder
from .gating import clean_text, compact_text, config_bool, dedup_key, normalize_query, should_skip_retrieval
from .governance import extract_candidates
from .memory_ops import (
    context_payload,
    benchmark_queries,
    dedupe_memories,
    delete_memories,
    explain_query,
    export_memories,
    feedback_memory,
    find_semantic_merge_candidate,
    govern_memories,
    hygiene_report,
    inspect_memory,
    merge_memories,
    probe_entity,
    repair_vector,
    related_entities,
    stats_payload,
    store_memory_now,
    update_memory,
)
from .migration import migrate_legacy_scope_recall_storage
from .models import RecallItem, RuntimeScope, recall_scope_mode
from .recall import RecallService
from .prompting import render_current_turn_recall
from .schemas import (
    SCOPE_RECALL_DEDUPE_SCHEMA,
    SCOPE_RECALL_BENCHMARK_SCHEMA,
    SCOPE_RECALL_CONTEXT_SCHEMA,
    SCOPE_RECALL_EXPLAIN_SCHEMA,
    SCOPE_RECALL_EXPORT_SCHEMA,
    SCOPE_RECALL_FEEDBACK_SCHEMA,
    SCOPE_RECALL_FORGET_SCHEMA,
    SCOPE_RECALL_GOVERN_SCHEMA,
    SCOPE_RECALL_HYGIENE_SCHEMA,
    SCOPE_RECALL_INSPECT_SCHEMA,
    SCOPE_RECALL_MERGE_SCHEMA,
    SCOPE_RECALL_PROBE_SCHEMA,
    SCOPE_RECALL_REPAIR_SCHEMA,
    SCOPE_RECALL_RELATED_SCHEMA,
    SCOPE_RECALL_SEARCH_SCHEMA,
    SCOPE_RECALL_STATS_SCHEMA,
    SCOPE_RECALL_STORE_SCHEMA,
    SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA,
    SCOPE_RECALL_UPDATE_SCHEMA,
)
from .scope import accessible_scope_ids, build_scope_id, build_shared_pool_scope_id, build_shared_scope_id, normalize_scope_identity, writable_scope_ids
from .sql_store import ensure_schema
from .storage_views import search_curated_memories, search_db_memories, search_vector_memories
from .tooling import ScopeRecallToolService
from .vector_runtime import setup_vector_layer

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
        self._shared_scope_id = ""
        self._shared_pool_enabled = False
        self._shared_pool_id = ""
        self._shared_pool_scope_id = ""
        self._accessible_scope_ids: list[str] = []
        self._writable_scope_ids: list[str] = []
        self._storage_dir: Path | None = None
        self._db_path: Path | None = None
        self._hermes_home: Path | None = None
        self._plugin_dir = Path(__file__).resolve().parent
        self._last_recall_turns: dict[str, int] = {}
        self._embedder: BaseEmbedder | None = None
        self._vector_store: Any | None = None
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
        self._journal_digest_thread: threading.Thread | None = None
        self._journal_digest_lock = threading.Lock()
        self._last_journal_digest_started = 0.0

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
                "key": "capture_llm.enabled",
                "description": "Use LLM to extract user+assistant turns into structured memory (requires API key)",
                "default": "false",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_raw_user",
                "description": "Legacy fallback: store whole user turns as local scratch memory when no structured extraction candidate is found",
                "default": "false",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_llm.model",
                "description": "LLM model for capture extraction (OpenAI-compatible)",
                "default": "gpt-4o-mini",
            },
            {
                "key": "vector.enabled",
                "description": "Enable the rebuildable vector companion layer",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "vector.backend",
                "description": "Vector companion backend: LanceDB for ANN search, or sqlite-bruteforce for non-AVX/native-free hosts",
                "default": "lancedb",
                "choices": ["lancedb", "sqlite-bruteforce"],
            },
            {
                "key": "vector.fallback_backend",
                "description": "Safe backend used automatically when LanceDB/PyArrow cannot be imported safely",
                "default": "sqlite-bruteforce",
                "choices": ["sqlite-bruteforce", "disabled"],
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
            {
                "key": "maintenance_tools_enabled",
                "description": "Enable operator-only maintenance tools such as dedupe, governance, and vector repair",
                "default": "false",
                "choices": ["true", "false"],
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
        raw_scope = RuntimeScope(
            platform=str(kwargs.get("platform") or "cli"),
            user_id=str(kwargs.get("user_id") or ""),
            chat_id=str(kwargs.get("chat_id") or ""),
            thread_id=str(kwargs.get("thread_id") or ""),
            gateway_session_key=str(kwargs.get("gateway_session_key") or ""),
            agent_identity=str(kwargs.get("agent_identity") or ""),
            agent_workspace=str(kwargs.get("agent_workspace") or ""),
            agent_context=str(kwargs.get("agent_context") or "primary"),
        )
        self._scope = normalize_scope_identity(raw_scope, self._config)
        self._scope_id = build_scope_id(self._scope, self._config)
        self._shared_scope_id = build_shared_scope_id(self._scope, self._config)
        self._accessible_scope_ids = accessible_scope_ids(self._scope, self._config)
        self._writable_scope_ids = writable_scope_ids(self._scope, self._config)
        raw_shared_pool_config = self._config.get("shared_pool")
        shared_pool_config = raw_shared_pool_config if isinstance(raw_shared_pool_config, dict) else {}
        self._shared_pool_enabled = config_bool(shared_pool_config, "enabled", False)
        self._shared_pool_id = str(shared_pool_config.get("pool_id") or "default") if self._shared_pool_enabled else ""
        self._shared_pool_scope_id = build_shared_pool_scope_id(self._scope, self._shared_pool_id) if self._shared_pool_enabled else ""
        if self._shared_pool_scope_id and self._shared_pool_scope_id not in self._accessible_scope_ids:
            self._accessible_scope_ids.append(self._shared_pool_scope_id)
        self._current_turn = 0
        self._last_recall_turns = {}

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        ensure_schema(self._conn)
        ensure_journal_schema(self._conn)
        setup_vector_layer(self)
        start_writer(self)

    def system_prompt_block(self) -> str:
        suffix = ""
        if self._vector_enabled and self._vector_ready:
            suffix = f" Hybrid lexical+vector recall is enabled with a local {self._vector_backend} companion index."
        elif self._vector_enabled and not self._vector_ready:
            suffix = f" Vector companion requested but not active ({self._vector_message or self._vector_status})."
        return (
            "# Scope Recall Memory\n"
            "Active. Uses current-turn local recall with conservative gating."
            " Durable user/project/ops/memory rows are shared across windows/chats for the same user + agent identity,"
            " while raw general turn captures remain local to the current chat/thread/session."
            " Built-in curated memory files are read live at recall time, and previous-turn prefetched memory is never injected into a new topic."
            " Local entity indexes and trust feedback can refine recall without leaving the SQLite truth boundary."
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
        return render_current_turn_recall(self, query)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        del session_id
        if not config_bool(self._config, "auto_capture", True):
            return
        if self._scope.agent_context != "primary":
            return

        clean_user = sanitize_capture_text(self._clean_text(user_content))
        clean_assistant = sanitize_capture_text(self._clean_text(assistant_content))
        min_capture = int(self._config_value("min_capture_length", 40))
        user_filter = should_capture_text(clean_user, self._config)
        assistant_filter = should_capture_text(clean_assistant, self._config)
        journal_filter_config = dict(self._config)
        journal_filter_config["capture_hard_max_chars"] = -1
        journal_user_filter = should_capture_text(clean_user, journal_filter_config)
        journal_assistant_filter = should_capture_text(clean_assistant, journal_filter_config)

        # Journal-first provenance capture: raw turns go to a staging journal,
        # not durable recall rows or vector indexes. Background journal digest
        # later groups, extracts, and merge-upserts high-density memories.
        raw_journal_cfg = self._config.get("journal")
        journal_cfg = raw_journal_cfg if isinstance(raw_journal_cfg, dict) else {}
        journal_enabled = journal_cfg.get("enabled", True)
        if isinstance(journal_enabled, str):
            journal_enabled = journal_enabled.strip().lower() in {"1", "true", "yes", "on"}
        if journal_enabled and (journal_user_filter.allowed or journal_assistant_filter.allowed):
            journal_appended = False
            with self._lock:
                ensure_journal_schema(self._require_conn())
                if journal_user_filter.allowed and clean_user:
                    journal_appended = bool(
                        append_journal_entry(
                            self._require_conn(),
                            scope=self._scope,
                            scope_id=self._scope_id,
                            shared_scope_id=self._shared_scope_id,
                            session_id=self._session_id,
                            turn_number=self._current_turn,
                            role="user",
                            content=clean_user,
                        )
                    ) or journal_appended
                if journal_assistant_filter.allowed and clean_assistant:
                    journal_appended = bool(
                        append_journal_entry(
                            self._require_conn(),
                            scope=self._scope,
                            scope_id=self._scope_id,
                            shared_scope_id=self._shared_scope_id,
                            session_id=self._session_id,
                            turn_number=self._current_turn,
                            role="assistant",
                            content=clean_assistant,
                        )
                    ) or journal_appended
            if journal_appended:
                self._maybe_start_background_journal_digest()

        # ── LLM semantic extraction (preferred when explicitly enabled) ──
        llm_extracted = False
        capture_llm_config = self._config.get("capture_llm")
        if isinstance(capture_llm_config, dict) and (
            capture_llm_config.get("enabled") in (True, "true", "1", "yes", "on")
        ):
            min_user = int(capture_llm_config.get("min_user_chars", 20))
            min_asst = int(capture_llm_config.get("min_assistant_chars", 30))
            if (
                user_filter.allowed
                and len(clean_user) >= min_user
                and assistant_filter.allowed
                and len(clean_assistant) >= min_asst
            ):
                for candidate in extract_capture_candidates(clean_user, clean_assistant, self._config):
                    if len(candidate.content) < 12:
                        continue
                    enqueue_store(
                        self,
                        content=candidate.content,
                        source="turn-llm-extracted",
                        target=candidate.target,
                        session_id=self._session_id,
                        metadata={
                            "category": candidate.memory_type,
                            "confidence": candidate.confidence,
                            "entities": candidate.entities,
                            "tags": candidate.tags,
                        },
                    )
                    llm_extracted = True

        # ── Regex extraction (legacy hot-path fallback; disabled by default) ──
        extracted = False
        per_turn_cfg = self._config.get("per_turn_extraction") if isinstance(self._config.get("per_turn_extraction"), dict) else {}
        per_turn_regex_enabled = False
        if isinstance(per_turn_cfg, dict):
            per_turn_regex_enabled = config_bool(per_turn_cfg, "enabled", False)
        if not llm_extracted and per_turn_regex_enabled and user_filter.allowed:
            for candidate in extract_candidates(clean_user):
                candidate_min_capture = min(min_capture, 24) if candidate.target in {"user", "ops", "project"} else min_capture
                if len(candidate.content) < candidate_min_capture:
                    continue
                enqueue_store(
                    self,
                    content=candidate.content,
                    source="turn-extracted",
                    target=candidate.target,
                    session_id=self._session_id,
                    metadata={"category": candidate.category, "confidence": candidate.confidence},
                )
                extracted = True

        # ── Raw user capture (last-resort fallback) ──
        if (
            not llm_extracted
            and config_bool(self._config, "capture_raw_user", False)
            and user_filter.allowed
            and len(clean_user) >= min_capture
            and not extracted
        ):
            enqueue_store(
                self,
                content=clean_user,
                source="turn-user",
                target="general",
                session_id=self._session_id,
            )

        # ── Raw assistant capture (legacy, only when LLM not used) ──
        if (
            not llm_extracted
            and config_bool(self._config, "capture_assistant", False)
            and assistant_filter.allowed
            and len(clean_assistant) >= min_capture
        ):
            enqueue_store(
                self,
                content=clean_assistant,
                source="turn-assistant",
                target="general",
                session_id=self._session_id,
            )

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if not messages or not config_bool(self._config, "auto_capture", True):
            return ""
        if self._scope.agent_context != "primary":
            return ""
        journal_config = self._journal_config()
        if not config_bool(journal_config, "enabled", True):
            return ""

        filter_config = dict(self._config)
        filter_config["capture_hard_max_chars"] = -1
        appended = 0
        roles: set[str] = set()
        with self._lock:
            conn = self._require_conn()
            ensure_journal_schema(conn)
            for index, message in enumerate(messages, start=1):
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or message.get("type") or "").strip().lower()
                # Tool traces are handled by on_session_end with explicit
                # provenance. Do not stage raw tool/system wrapper content at
                # compression boundaries.
                if role not in {"user", "assistant"}:
                    continue
                content = sanitize_capture_text(self._clean_text(message.get("content")))
                if not content:
                    continue
                if not should_capture_text(content, filter_config).allowed:
                    continue
                inserted_id = append_journal_entry(
                    conn,
                    scope=self._scope,
                    scope_id=self._scope_id,
                    shared_scope_id=self._shared_scope_id,
                    session_id=self._session_id,
                    turn_number=index,
                    role=role,
                    content=content,
                    metadata={
                        "source": "pre-compression",
                        "compression_boundary": True,
                        "message_index": index,
                    },
                )
                if inserted_id:
                    appended += 1
                    roles.add(role)
        if not appended:
            return ""
        self._maybe_start_background_journal_digest()
        role_label = "/".join(sorted(roles)) if roles else "message"
        plural = "entry" if appended == 1 else "entries"
        return (
            f"Scope Recall staged {appended} sanitized {role_label} compression-boundary journal {plural} "
            "for the normal journal digest/merge-upsert path; raw tool output, wrappers, and secret-like text were filtered."
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
        self._append_session_tool_journal(messages)
        flush_writer(self, timeout=3.0)
        self._run_session_end_journal_digest()

    def _journal_config(self) -> dict[str, Any]:
        raw_journal = self._config.get("journal")
        return raw_journal if isinstance(raw_journal, dict) else {}

    def _append_session_tool_journal(self, messages: List[Dict[str, Any]]) -> None:
        if not messages or self._scope.agent_context != "primary":
            return
        journal_config = self._journal_config()
        if not config_bool(journal_config, "enabled", True):
            return
        with self._lock:
            ensure_journal_schema(self._require_conn())
            for index, message in enumerate(messages, start=1):
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or message.get("type") or "").strip().lower()
                if role != "tool":
                    continue
                content = self._tool_journal_content(message)
                if not content:
                    continue
                append_journal_entry(
                    self._require_conn(),
                    scope=self._scope,
                    scope_id=self._scope_id,
                    shared_scope_id=self._shared_scope_id,
                    session_id=self._session_id,
                    turn_number=index,
                    role="tool",
                    content=content,
                    metadata={
                        "source": "session-end-tool-trace",
                        "tool_name": str(message.get("name") or message.get("tool_name") or ""),
                        "message_index": index,
                    },
                )

    def _tool_journal_content(self, message: Dict[str, Any]) -> str:
        tool_name = str(message.get("name") or message.get("tool_name") or message.get("recipient") or "").strip()
        raw_content = message.get("content")
        if raw_content is None:
            raw_content = message.get("output")
        if raw_content is None:
            raw_content = message.get("result")
        content = clean_text(raw_content)
        if not content:
            return ""
        prefix = f"Tool execution trace ({tool_name})" if tool_name else "Tool execution trace"
        return compact_text(f"{prefix}: {content}", 1800)

    def _coerce_journal_float(self, journal_config: dict[str, Any], key: str, default: float) -> float:
        try:
            return float(journal_config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _background_digest_scope(self) -> RuntimeScope:
        return RuntimeScope(
            platform=self._scope.platform,
            user_id=self._scope.user_id,
            chat_id=self._scope.chat_id,
            thread_id=self._scope.thread_id,
            gateway_session_key=self._scope.gateway_session_key,
            agent_identity=self._scope.agent_identity,
            agent_workspace=self._scope.agent_workspace,
            agent_context="primary",
        )

    def _maybe_start_background_journal_digest(self) -> None:
        if self._hermes_home is None or self._scope.agent_context != "primary":
            return
        journal_config = self._journal_config()
        if not config_bool(journal_config, "enabled", True):
            return
        if not config_bool(journal_config, "background_digest_enabled", True):
            return
        interval_hours = self._coerce_journal_float(journal_config, "digest_interval_hours", 2.0)
        if interval_hours <= 0:
            return
        now = time.time()
        with self._journal_digest_lock:
            if self._journal_digest_thread is not None and self._journal_digest_thread.is_alive():
                return
            if self._last_journal_digest_started and now - self._last_journal_digest_started < interval_hours * 3600:
                return
            self._last_journal_digest_started = now
            if config_bool(journal_config, "background_digest_synchronous", False):
                self._run_background_journal_digest(journal_config)
                return
            thread = threading.Thread(
                target=self._run_background_journal_digest,
                args=(dict(journal_config),),
                name="scope-recall-journal-digest",
                daemon=True,
            )
            self._journal_digest_thread = thread
            thread.start()

    def _run_background_journal_digest(self, journal_config: dict[str, Any]) -> None:
        if self._hermes_home is None:
            return
        try:
            limit_entries = int(journal_config.get("max_entries_per_digest") or 500)
        except (TypeError, ValueError):
            limit_entries = 500
        extractor = str(journal_config.get("extractor") or "llm").strip().lower()
        try:
            run_journal_digest(
                hermes_home=self._hermes_home,
                extractor=extractor,
                scope=self._background_digest_scope(),
                interval_label=f"background-{journal_config.get('digest_interval_hours', 2)}h",
                limit_entries=max(1, limit_entries),
                dry_run=False,
            )
        except Exception:
            logger.exception("Scope Recall background journal digest failed")

    def _run_session_end_journal_digest(self) -> None:
        if self._hermes_home is None or self._scope.agent_context != "primary":
            return
        journal_config = self._journal_config()
        if not config_bool(journal_config, "enabled", True):
            return
        if not config_bool(journal_config, "digest_on_session_end", True):
            return
        try:
            limit_entries = int(journal_config.get("max_entries_per_digest") or 500)
        except (TypeError, ValueError):
            limit_entries = 500
        extractor = str(journal_config.get("extractor") or "llm").strip().lower()
        if extractor == "llm" and not config_bool(journal_config, "allow_session_end_llm", False):
            logger.info("Scope Recall session-end journal digest skipped: llm extractor requires scheduled/background digest")
            return
        try:
            run_journal_digest(
                hermes_home=self._hermes_home,
                extractor=extractor,
                scope=self._scope,
                interval_label="session-end",
                limit_entries=max(1, limit_entries),
                dry_run=False,
            )
        except Exception:
            logger.exception("Scope Recall session-end journal digest failed")

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

    def _schema_config(self) -> dict[str, Any]:
        if self._config:
            return self._config
        hermes_home = self._hermes_home or Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
        storage_dir = hermes_home / "scope-recall"
        config = load_runtime_config(self._plugin_dir, storage_dir)
        self._config = config
        self._retrieval_config = dict(config.get("retrieval") or {})
        self._vector_config = dict(config.get("vector") or {})
        return config

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        config = self._schema_config()
        if not config_bool(config, "enable_tools", True):
            return []
        if self._scope.agent_context != "primary":
            return []
        schemas = [
            SCOPE_RECALL_STORE_SCHEMA,
            SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA,
            SCOPE_RECALL_SEARCH_SCHEMA,
            SCOPE_RECALL_CONTEXT_SCHEMA,
            SCOPE_RECALL_PROBE_SCHEMA,
            SCOPE_RECALL_RELATED_SCHEMA,
            SCOPE_RECALL_FEEDBACK_SCHEMA,
            SCOPE_RECALL_FORGET_SCHEMA,
            SCOPE_RECALL_UPDATE_SCHEMA,
            SCOPE_RECALL_MERGE_SCHEMA,
            SCOPE_RECALL_EXPORT_SCHEMA,
            SCOPE_RECALL_STATS_SCHEMA,
            SCOPE_RECALL_INSPECT_SCHEMA,
            SCOPE_RECALL_EXPLAIN_SCHEMA,
            SCOPE_RECALL_BENCHMARK_SCHEMA,
        ]
        if config_bool(config, "maintenance_tools_enabled", False):
            schemas.extend([SCOPE_RECALL_DEDUPE_SCHEMA, SCOPE_RECALL_GOVERN_SCHEMA, SCOPE_RECALL_REPAIR_SCHEMA, SCOPE_RECALL_HYGIENE_SCHEMA])
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        del kwargs
        if self._scope.agent_context != "primary":
            from tools.registry import tool_error

            return tool_error("scope-recall tools are only available in the primary agent context")
        return self._tool_service.handle(tool_name, args)

    def shutdown(self) -> None:
        shutdown_writer(self, timeout=3.0)
        thread = self._journal_digest_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
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
        allow_duplicate: bool = False,
        semantic_merge: bool = True,
    ) -> tuple[str, bool, str]:
        return store_memory_now(
            self,
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata,
            allow_duplicate=allow_duplicate,
            semantic_merge=semantic_merge,
        )

    def _find_semantic_merge_candidate(self, content: str, target: str) -> tuple[str, str]:
        return find_semantic_merge_candidate(self, content, target)

    def _update_memory(self, memory_id: str, content: str, target: str | None = None) -> tuple[bool, str, str]:
        return update_memory(self, memory_id, content, target)

    def _merge_memories(self, target_id: str, source_ids: list[str], content: str | None = None, target: str | None = None) -> dict[str, Any]:
        return merge_memories(self, target_id, source_ids, content, target)

    def _export_memories(self, *, fmt: str = "jsonl", scope_only: bool = True) -> dict[str, Any]:
        return export_memories(self, fmt=fmt, scope_only=scope_only)

    def _govern_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return govern_memories(self, dry_run=dry_run, scope_only=scope_only)

    def _delete_memories(self, ids: list[str]) -> int:
        return delete_memories(self, ids)

    def _dedupe_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return dedupe_memories(self, dry_run=dry_run, scope_only=scope_only)

    def _repair_vector(self) -> dict[str, Any]:
        return repair_vector(self)

    def _hygiene_report(self, *, limit: int = 200) -> dict[str, Any]:
        return hygiene_report(self, limit=limit)

    def _context_payload(self, *, query: str, limit: int = 5, max_chars: int = 900) -> dict[str, Any]:
        return context_payload(self, query=query, limit=limit, max_chars=max_chars)

    def _probe_entity(self, *, entity: str, limit: int = 10) -> dict[str, Any]:
        return probe_entity(self, entity=entity, limit=limit)

    def _related_entities(self, *, entity: str, limit: int = 12) -> dict[str, Any]:
        return related_entities(self, entity=entity, limit=limit)

    def _feedback_memory(self, *, memory_id: str, rating: str, note: str = "") -> dict[str, Any]:
        return feedback_memory(self, memory_id=memory_id, rating=rating, note=note)

    def _inspect_memory(self, *, memory_id: str) -> dict[str, Any]:
        return inspect_memory(self, memory_id=memory_id)

    def _explain_query(self, *, query: str, limit: int = 5) -> dict[str, Any]:
        return explain_query(self, query=query, limit=limit)

    def _benchmark_queries(self, *, queries: list[str], limit: int = 5) -> dict[str, Any]:
        return benchmark_queries(self, queries=queries, limit=limit)

    def _search_vector_memories(self, query: str, *, limit: int) -> List[RecallItem]:
        return search_vector_memories(self, query, limit=limit)

    def _search_curated_memories(self, query: str) -> List[RecallItem]:
        return search_curated_memories(self, query)

    def _mark_recalled(self, memory_ids: List[str]) -> None:
        for memory_id in memory_ids:
            self._last_recall_turns[memory_id] = self._current_turn

    def _stats_payload(self) -> Dict[str, Any]:
        return stats_payload(self)

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

    def _clean_text(self, text: Any) -> str:
        return clean_text(text)

    def _normalize_query(self, query: str, char_limit: int) -> str:
        return normalize_query(query, char_limit)

    def _dedup_key(self, content: str) -> str:
        return dedup_key(content)

    def _scope_mode_for(self, target: str, source: str = "") -> str:
        return recall_scope_mode(target, source)


def register(ctx) -> None:
    ctx.register_memory_provider(ScopeRecallMemoryProvider())
