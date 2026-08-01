"""Hermes MemoryProvider implementation for Scope Recall.

The provider owns runtime lifecycle, scope resolution, vector setup, journal capture, and tool registration while delegating domain logic to smaller modules."""

from __future__ import annotations

import logging
import json
import os
import queue
import sqlite3
import threading
import time
import weakref
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .capture import enqueue_store, flush_writer, shutdown_writer, start_writer
from .candidate_extraction import extract_candidates_from_packet
from .candidate_store import store_event_candidates
from .capture_filters import contains_secret_like_text, redact_secret_like_text, sanitize_capture_text, sanitize_report_text, should_capture_text
from .capture_llm import extract_capture_candidates
from .config import load_runtime_config, save_runtime_config
from .event_digest import MemoryEvent, build_evidence_packet
from .journal import append_journal_entry, ensure_journal_schema, run_journal_digest
from .maintenance_lease import (
    MaintenanceLeaseError,
    activation_lease_status,
    ensure_activation_guard_triggers,
    install_activation_lease_authorizer,
)
from .embedders import BaseEmbedder
from .gating import clean_text, compact_text, config_bool, dedup_key, normalize_query, should_skip_retrieval
from .governance import extract_candidates
from .memory_ops import (
    context_payload,
    profile_payload,
    archive_memories,
    benchmark_queries,
    dedupe_memories,
    delete_memories,
    explain_query,
    export_memories,
    feedback_memory,
    fact_owned_memory_ids,
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
from .provider_schemas import build_config_schema, build_tool_schemas
from .scope import accessible_scope_ids, build_scope_id, build_shared_pool_scope_id, build_shared_scope_id, normalize_scope_identity, writable_scope_ids
from .source_isolation import scope_is_memory_isolated
from .sql_store import ensure_schema

from .storage_views import search_curated_memories, search_db_memories, search_vector_memories
from .tooling import ScopeRecallToolService
from .truth_connection import connect_truth_database
from .vector_bootstrap import bootstrap_fresh_vector_companion
from .vector_runtime import setup_vector_layer
from .experience_preflight import experience_preflight
from .experience_promotion import promote_experiences
from .experience_store import backfill_skill_anchors
from .freshness import backfill_untracked_memory_freshness

logger = logging.getLogger(__name__)

SQLITE_BUSY_TIMEOUT_SECONDS = 10.0
STARTUP_FRESHNESS_BACKFILL_LIMIT = 500

DEFAULT_TOOL_TRACE_SKIP_NAMES = {"todo", "skill_view", "skills_list"}
DEFAULT_TOOL_TRACE_SKIP_NAME_FRAGMENTS = {"session_messages"}
_PROVIDER_REGISTRY_LOCK = threading.RLock()
_PROVIDER_REGISTRY: weakref.WeakSet[Any] = weakref.WeakSet()


def _is_sqlite_lock_contention(exc: sqlite3.OperationalError) -> bool:
    """Return whether one SQLite operational failure is safe to defer at startup."""

    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(exc).strip().lower()
    return "database is locked" in message or "database table is locked" in message


class ScopeRecallMemoryProvider(MemoryProvider):
    """Hermes memory-provider runtime for Scope Recall.

    This class is the lifecycle boundary: it opens SQLite truth, configures scope visibility, starts background capture/digest work, and exposes tool schemas. Domain decisions live in helper modules so startup and shutdown remain auditable."""
    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._retrieval_config: dict[str, Any] = {}
        self._vector_config: dict[str, Any] = {}
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._write_queue: queue.Queue[Any] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        # Queue-order receipts: a flush acknowledges every write job before its
        # marker and must report whether any of those jobs failed.  Keep only
        # the exception class for diagnostics so provider/auth text cannot leak
        # through the stats tool.
        self._writer_failed_writes = 0
        self._writer_reported_failures = 0
        self._writer_last_error_type = ""
        self._stop = threading.Event()
        self._maintenance_stop = threading.Event()
        self._session_id = ""
        self._current_turn = 0
        self._scope = RuntimeScope()
        self._scope_id = ""
        self._shared_scope_id = ""
        self._shared_pool_enabled = False
        self._shared_pool_write_enabled = False
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
        self._vector_lock = threading.RLock()
        self._vector_enabled = False
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._vector_backend = "lancedb"
        self._migration_info: dict[str, Any] = {"migrated": False}
        self._freshness_backfill: dict[str, Any] = {
            "apply": True,
            "eligible": 0,
            "inserted": 0,
            "truncated": False,
            "ids": [],
        }
        self._recall_service = RecallService(self)
        self._tool_service = ScopeRecallToolService(self)
        self._shutdown_requested = threading.Event()
        self._writer_lifecycle_lock = threading.RLock()
        self._journal_digest_thread: threading.Thread | None = None
        self._journal_digest_lock = threading.RLock()
        self._last_journal_digest_started = 0.0
        self._last_journal_digest_finished = 0.0
        self._last_journal_digest_status = "never_run"
        self._last_journal_digest_error = ""
        self._journal_digest_consecutive_failures = 0
        self._last_event_digest_report: dict[str, Any] = {
            "enabled": False,
            "dry_run": True,
            "write_candidates": False,
            "events_seen": 0,
            "candidates_proposed": 0,
            "candidates_rejected": 0,
            "rejection_reasons": {},
        }

    @property
    def name(self) -> str:
        return "scope-recall"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return build_config_schema()

    def save_config(
        self,
        values: Dict[str, Any],
        hermes_home: str,
        *,
        activation_lease_token: str = "",
    ) -> None:
        save_runtime_config(values or {}, hermes_home)
        self._bootstrap_storage(
            hermes_home,
            activation_lease_token=activation_lease_token,
        )

    def _bootstrap_storage(
        self,
        hermes_home: str | os.PathLike[str],
        *,
        activation_lease_token: str = "",
    ) -> None:
        """Create the empty SQLite truth/journal schema during `hermes memory setup`.

        Gateway sessions can lazily construct agents only after the first user
        message. Bootstrapping here gives operators an immediate, visible setup
        artifact instead of a false-negative "no database yet" verification gap.
        """
        storage_dir = Path(hermes_home).expanduser() / "scope-recall"
        storage_dir.mkdir(parents=True, exist_ok=True)
        db_path = storage_dir / "memory.sqlite3"
        conn = connect_truth_database(
            db_path,
            mode="rwc",
            check_same_thread=False,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        try:
            install_activation_lease_authorizer(
                conn,
                db_path,
                lease_token=activation_lease_token,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            ensure_schema(conn, commit=False)
            ensure_journal_schema(conn, commit=False)
            ensure_activation_guard_triggers(
                conn,
                db_path,
                lease_token=activation_lease_token,
            )
            conn.commit()
            runtime_config = load_runtime_config(self._plugin_dir, storage_dir)
            self._bootstrap_vector_companion(
                storage_dir,
                runtime_config,
                truth_conn=conn,
            )
            conn.commit()
        finally:
            conn.close()

    def _bootstrap_vector_companion(
        self,
        storage_dir: Path,
        runtime_config: dict[str, Any],
        *,
        truth_conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        """Delegate fresh primary/fallback selection to the bootstrap policy."""

        result = bootstrap_fresh_vector_companion(
            storage_dir,
            runtime_config,
            truth_conn=truth_conn,
        )
        self._last_vector_bootstrap = result
        return result

    def _open_runtime_connection(self) -> sqlite3.Connection:
        """Open and fully configure one live provider-owned SQLite connection."""

        if self._db_path is None:
            raise RuntimeError("Scope Recall database path is not initialized")
        lease_status = activation_lease_status(self._db_path)
        if lease_status["status"] == "stale":
            raise MaintenanceLeaseError(
                "stale activation maintenance lease blocks startup; inspect with "
                "python scripts/recover.activation_lease.py --dry-run"
            )
        if lease_status["status"] == "active":
            raise MaintenanceLeaseError(
                "Scope Recall startup is blocked by an active activation maintenance lease"
            )
        conn = connect_truth_database(
            self._db_path,
            mode="rwc",
            check_same_thread=False,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        try:
            install_activation_lease_authorizer(conn, self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            ensure_schema(conn, commit=False)
            ensure_journal_schema(conn, commit=False)
            ensure_activation_guard_triggers(conn, self._db_path)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
            raise
        return conn

    def initialize(self, session_id: str, **kwargs) -> None:
        self._shutdown_requested.clear()
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
        self._shared_pool_write_enabled = self._shared_pool_enabled and config_bool(shared_pool_config, "write_enabled", False)
        self._shared_pool_id = str(shared_pool_config.get("pool_id") or "default") if self._shared_pool_enabled else ""
        self._shared_pool_scope_id = build_shared_pool_scope_id(self._scope, self._shared_pool_id) if self._shared_pool_enabled else ""
        if self._shared_pool_scope_id and self._shared_pool_scope_id not in self._accessible_scope_ids:
            self._accessible_scope_ids.append(self._shared_pool_scope_id)
        if self._shared_pool_write_enabled and self._shared_pool_scope_id and self._shared_pool_scope_id not in self._writable_scope_ids:
            self._writable_scope_ids.append(self._shared_pool_scope_id)
        self._current_turn = 0
        self._last_recall_turns = {}

        conn = self._open_runtime_connection()
        self._conn = conn
        try:
            self._freshness_backfill = backfill_untracked_memory_freshness(
                conn,
                apply=True,
                limit=STARTUP_FRESHNESS_BACKFILL_LIMIT,
            )
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_lock_contention(exc):
                self._conn = None
                conn.close()
                raise
            self._rollback_conn_after_error("startup freshness backfill contention")
            self._freshness_backfill = {
                "apply": True,
                "status": "deferred_error",
                "error_type": type(exc).__name__,
            }
            logger.warning(
                "Scope Recall startup freshness backfill deferred after SQLite lock contention"
            )
        except BaseException:
            self._conn = None
            conn.close()
            raise
        try:
            backfill_skill_anchors(conn)
        except Exception:
            self._rollback_conn_after_error("skill-anchor backfill")
            logger.exception("Scope Recall skill-anchor backfill failed")
        ensure_journal_schema(conn, commit=False)
        ensure_activation_guard_triggers(conn, self._db_path)
        conn.commit()
        setup_vector_layer(self)
        start_writer(self)
        self._register_provider_instance()

    def _register_provider_instance(self) -> None:
        """Register this live provider for same-process SQLite lock recovery."""
        with _PROVIDER_REGISTRY_LOCK:
            _PROVIDER_REGISTRY.add(self)

    def _unregister_provider_instance(self) -> None:
        with _PROVIDER_REGISTRY_LOCK:
            _PROVIDER_REGISTRY.discard(self)

    def _memory_isolated_for_scope(self) -> bool:
        """Return whether this chat is excluded from every memory surface."""

        return scope_is_memory_isolated(getattr(self, "_scope", None), self._config)

    def system_prompt_block(self) -> str:
        if self._memory_isolated_for_scope():
            return (
                "# Scope Recall Memory\n"
                "Disabled for this chat by explicit source-isolation policy. "
                "Do not read, infer, or store durable user preferences or memories from this chat."
            )
        suffix = ""
        if self._vector_enabled and self._vector_ready:
            suffix = f" Hybrid lexical+vector recall is enabled with a local {self._vector_backend} companion index."
        elif self._vector_enabled and not self._vector_ready:
            status = str(self._vector_status or "unavailable")[:64]
            suffix = f" Vector companion requested but not active (status={status})."
        return (
            "# Scope Recall Memory\n"
            "Active. Uses current-turn local recall with conservative gating."
            " Durable user/project/ops/memory rows are shared across windows/chats for the same user + agent identity,"
            " while raw general turn captures remain local to the current chat/thread/session."
            " Built-in curated memory files are read live at recall time, and previous-turn prefetched memory is never injected into a new topic."
            " Local entity indexes and trust feedback can refine recall without leaving the SQLite truth boundary."
            + suffix
        )

    def suppress_builtin_memory(self) -> bool:
        """Hide Hermes' parallel curated-memory surface for isolated chats."""

        return self._memory_isolated_for_scope()

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        del message, kwargs
        self._current_turn = int(turn_number or 0)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        del query, session_id
        return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        del session_id
        if self._memory_isolated_for_scope():
            return ""
        try:
            recall_block = render_current_turn_recall(self, query)
        except Exception:
            self._rollback_conn_after_error("current-turn recall prefetch")
            logger.exception("Scope Recall current-turn recall prefetch failed")
            recall_block = ""
        raw_experience_config = self._config.get("experience")
        experience_config = raw_experience_config if isinstance(raw_experience_config, dict) else {}
        if not config_bool(experience_config, "enabled", True):
            return recall_block
        if not config_bool(experience_config, "prefetch_enabled", False):
            return recall_block
        try:
            with self._lock:
                packet = experience_preflight(
                    self._require_conn(),
                    query=query,
                    accessible_scope_ids=self._accessible_scope_ids,
                    config=self._config,
                    record_run=True,
                    scope_id=self._scope_id,
                ).get("packet", "")
        except Exception:
            self._rollback_conn_after_error("experience preflight")
            logger.exception("Scope Recall experience preflight failed")
            packet = ""
        if not packet:
            return recall_block
        return f"{recall_block}\n\n{packet}" if recall_block else str(packet)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: list[dict[str, Any]] | None = None) -> None:
        """Synchronize one Hermes turn into capture, journal, recall, and prompt context state.

        This method is on the hot path, so it avoids heavyweight repair work and records failures as sanitized diagnostics instead of blocking the main agent loop."""
        del session_id
        if self._memory_isolated_for_scope():
            return
        if messages:
            self._append_session_tool_journal(messages)
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

    def _event_digest_config(self) -> dict[str, Any]:
        raw = self._config.get("event_digest")
        return raw if isinstance(raw, dict) else {}

    def _dry_run_event_candidates(self, *, kind: str, messages: List[Dict[str, Any]]) -> dict[str, Any]:
        event_config = self._event_digest_config()
        enabled = config_bool(event_config, "enabled", True)
        write_candidates = config_bool(event_config, "write_candidates", False)
        dry_run = not write_candidates
        report: dict[str, Any] = {
            "enabled": enabled,
            "dry_run": dry_run,
            "dry_run_log": config_bool(event_config, "dry_run_log", True),
            "write_candidates": write_candidates,
            "event_kind": kind,
            "events_seen": 0,
            "candidates_proposed": 0,
            "candidates_rejected": 0,
            "rejection_reasons": {},
            "store": {"planned": 0, "inserted": 0, "updated_existing": 0, "ids": []},
        }
        if not enabled or self._scope.agent_context != "primary":
            self._last_event_digest_report = report
            return report
        max_events = int(event_config.get("max_events_per_turn") or 3)
        if max_events <= 0:
            max_events = 3
        reasons: Counter[str] = Counter()
        proposed_candidates = []
        seen = 0
        proposed = 0
        rejected = 0
        for index, message in enumerate(messages, start=1):
            if seen >= max_events:
                break
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = sanitize_report_text(self._clean_text(message.get("content")))
            if not content:
                continue
            event = MemoryEvent(
                kind=kind,
                scope_id=self._scope_id,
                session_id=self._session_id,
                turn_number=index,
                content=content,
                metadata={"source": "provider-hook", "role": role},
            )
            packet = build_evidence_packet(event)
            extraction = extract_candidates_from_packet(packet, dry_run=True)
            seen += 1
            proposed += len(extraction.candidates)
            proposed_candidates.extend(extraction.candidates)
            if extraction.rejection_reasons:
                rejected += 1
                reasons.update(extraction.rejection_reasons)
        with self._lock:
            store_report = store_event_candidates(
                self._require_conn(),
                candidates=proposed_candidates,
                scope=self._scope,
                scope_id=self._scope_id,
                session_id=self._session_id,
                dry_run=dry_run,
            )
        report.update(
            {
                "events_seen": seen,
                "candidates_proposed": proposed,
                "candidates_rejected": rejected,
                "rejection_reasons": dict(sorted(reasons.items())),
                "store": store_report,
            }
        )
        self._last_event_digest_report = report
        return report

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Provide recall context before Hermes compresses the conversation.

        The hook should be compact and safe because it is injected into compression prompts, not treated as new user truth."""
        if self._memory_isolated_for_scope() or not messages or not config_bool(self._config, "auto_capture", True):
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
        self._dry_run_event_candidates(kind="pre_compress", messages=messages)
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
        if self._memory_isolated_for_scope():
            return
        self._append_session_tool_journal(messages)
        flush_writer(self, timeout=3.0)
        self._run_session_end_journal_digest()

    def _journal_config(self) -> dict[str, Any]:
        raw_journal = self._config.get("journal")
        return raw_journal if isinstance(raw_journal, dict) else {}

    def _append_session_tool_journal(self, messages: List[Dict[str, Any]]) -> None:
        if self._memory_isolated_for_scope() or not messages or self._scope.agent_context != "primary":
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
        """Decide how much tool result content should be captured into the journal.

        The helper strips low-value or risky tool traces so compression/journal evidence does not amplify logs, secrets, or large blobs."""
        tool_name = str(message.get("name") or message.get("tool_name") or message.get("recipient") or "").strip()
        journal_config = self._journal_config()
        skip_names = set(DEFAULT_TOOL_TRACE_SKIP_NAMES)
        raw_skip_names = journal_config.get("tool_trace_skip_names")
        if isinstance(raw_skip_names, str):
            skip_names.add(raw_skip_names.strip().lower())
        elif isinstance(raw_skip_names, (list, tuple, set)):
            skip_names.update(str(item).strip().lower() for item in raw_skip_names if str(item).strip())
        normalized_tool_name = tool_name.lower()
        if normalized_tool_name in skip_names or any(fragment in normalized_tool_name for fragment in DEFAULT_TOOL_TRACE_SKIP_NAME_FRAGMENTS):
            return ""
        raw_content = message.get("content")
        if raw_content is None:
            raw_content = message.get("output")
        if raw_content is None:
            raw_content = message.get("result")
        raw_clean = clean_text(raw_content)
        if contains_secret_like_text(raw_clean):
            return ""
        content = sanitize_capture_text(redact_secret_like_text(raw_clean))
        filter_config = dict(self._config)
        try:
            filter_config["capture_hard_max_chars"] = int(journal_config.get("tool_trace_hard_max_chars") or 4000)
        except (TypeError, ValueError):
            filter_config["capture_hard_max_chars"] = 4000
        include_preview = config_bool(journal_config, "tool_trace_include_output_preview", False)
        output_chars = len(content)
        safe_fields: list[str] = []
        if tool_name:
            safe_fields.append(f"tool={tool_name}")
        safe_fields.append(f"output_chars={output_chars}")
        parsed: Any = None
        if content:
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = None
        if isinstance(parsed, dict):
            for key in ("exit_code", "status", "ok", "success", "skipped", "deleted", "updated", "inserted"):
                if key in parsed and isinstance(parsed.get(key), (str, int, float, bool)):
                    safe_fields.append(f"{key}={parsed.get(key)}")
            error = parsed.get("error")
            if error:
                safe_fields.append(f"error={compact_text(sanitize_report_text(str(error)), 160)}")
        if include_preview and content and should_capture_text(content, filter_config).allowed:
            try:
                preview_chars = int(journal_config.get("tool_trace_preview_max_chars") or 500)
            except (TypeError, ValueError):
                preview_chars = 500
            safe_fields.append(f"preview={compact_text(content, max(120, preview_chars))}")
        elif content:
            safe_fields.append("output_preview=omitted")
        prefix = f"Tool execution summary ({tool_name})" if tool_name else "Tool execution summary"
        try:
            max_chars = int(journal_config.get("tool_trace_max_chars") or 1800)
        except (TypeError, ValueError):
            max_chars = 1800
        return compact_text(f"{prefix}: " + "; ".join(safe_fields), max(200, max_chars))

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
        if self._shutdown_requested.is_set():
            return
        if self._memory_isolated_for_scope() or self._hermes_home is None or self._scope.agent_context != "primary":
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
            if self._shutdown_requested.is_set():
                return
            if self._journal_digest_thread is not None and self._journal_digest_thread.is_alive():
                return
            if self._last_journal_digest_started and now - self._last_journal_digest_started < interval_hours * 3600:
                return
            self._last_journal_digest_started = now
            self._last_journal_digest_status = "running"
            self._last_journal_digest_error = ""
            if config_bool(journal_config, "background_digest_synchronous", False):
                current_thread = threading.current_thread()
                self._journal_digest_thread = current_thread
                try:
                    self._run_background_journal_digest(journal_config)
                finally:
                    if self._journal_digest_thread is current_thread:
                        self._journal_digest_thread = None
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
        if self._shutdown_requested.is_set():
            with self._journal_digest_lock:
                self._last_journal_digest_status = "skipped"
                self._last_journal_digest_error = "shutdown requested"
                self._last_journal_digest_finished = time.time()
            return
        if self._memory_isolated_for_scope():
            with self._journal_digest_lock:
                self._last_journal_digest_status = "skipped"
                self._last_journal_digest_error = "source-isolated chat"
                self._last_journal_digest_finished = time.time()
            return
        if self._hermes_home is None:
            with self._journal_digest_lock:
                self._last_journal_digest_status = "skipped"
                self._last_journal_digest_error = "missing hermes_home"
                self._last_journal_digest_finished = time.time()
            return
        extractor = str(journal_config.get("extractor") or "llm").strip().lower()
        try:
            result = run_journal_digest(
                hermes_home=self._hermes_home,
                extractor=extractor,
                scope=self._background_digest_scope(),
                interval_label=f"background-{journal_config.get('digest_interval_hours', 2)}h",
                limit_entries=None,
                dry_run=False,
            )
            ok = bool(result.get("ok", result.get("status") == "ok"))
            status = "ok" if ok else str(result.get("status") or "error")
            error = "" if ok else compact_text(sanitize_report_text(str(result.get("error") or result.get("message") or result)), 240)
            with self._journal_digest_lock:
                self._last_journal_digest_finished = time.time()
                self._last_journal_digest_status = status
                self._last_journal_digest_error = error
                self._journal_digest_consecutive_failures = 0 if ok else self._journal_digest_consecutive_failures + 1
            if ok and not self._shutdown_requested.is_set():
                self._maybe_run_auto_experience_promotion(trigger="background-journal-digest")
        except Exception as exc:
            self._rollback_conn_after_error("background journal digest")
            with self._journal_digest_lock:
                self._last_journal_digest_finished = time.time()
                self._last_journal_digest_status = "error"
                self._last_journal_digest_error = compact_text(sanitize_report_text(str(exc)), 240)
                self._journal_digest_consecutive_failures += 1
            logger.exception("Scope Recall background journal digest failed")

    def _run_session_end_journal_digest(self) -> None:
        if self._shutdown_requested.is_set():
            return
        if self._memory_isolated_for_scope() or self._hermes_home is None or self._scope.agent_context != "primary":
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
            result = run_journal_digest(
                hermes_home=self._hermes_home,
                extractor=extractor,
                scope=self._scope,
                interval_label="session-end",
                limit_entries=max(1, limit_entries),
                dry_run=False,
            )
            if result.get("ok", result.get("status") == "ok"):
                self._maybe_run_auto_experience_promotion(trigger="session-end-journal-digest")
        except Exception:
            self._rollback_conn_after_error("session-end journal digest")
            logger.exception("Scope Recall session-end journal digest failed")

    def _maybe_run_auto_experience_promotion(self, *, trigger: str) -> None:
        if self._shutdown_requested.is_set():
            return
        if self._memory_isolated_for_scope() or self._scope.agent_context != "primary":
            return
        raw_experience_config = self._config.get("experience")
        experience_config = raw_experience_config if isinstance(raw_experience_config, dict) else {}
        if not config_bool(experience_config, "enabled", True):
            return
        if not config_bool(experience_config, "auto_promotion_enabled", False):
            return
        try:
            limit_sessions = int(experience_config.get("auto_promotion_limit_sessions") or 20)
        except (TypeError, ValueError):
            limit_sessions = 20
        try:
            with self._lock:
                result = promote_experiences(
                    self._require_conn(),
                    accessible_scope_ids=self._accessible_scope_ids,
                    scope_id=self._scope_id,
                    shared_scope_id=self._shared_scope_id,
                    config=self._config,
                    limit_sessions=max(1, limit_sessions),
                    dry_run=False,
                )
            logger.info("Scope Recall auto experience promotion after %s: %s", trigger, result)
        except Exception:
            self._rollback_conn_after_error(f"auto experience promotion after {trigger}")
            logger.exception("Scope Recall auto experience promotion failed after %s", trigger)

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
        if self._memory_isolated_for_scope():
            return []
        return build_tool_schemas(self._schema_config(), agent_context=self._scope.agent_context)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        del kwargs
        if self._memory_isolated_for_scope():
            from tools.registry import tool_error

            return tool_error("scope-recall memory is disabled for this chat")
        if not config_bool(self._schema_config(), "enable_tools", True):
            from tools.registry import tool_error

            return tool_error("scope-recall tools are disabled by configuration")
        if self._scope.agent_context != "primary":
            from tools.registry import tool_error

            return tool_error("scope-recall tools are only available in the primary agent context")
        return self._tool_service.handle(tool_name, args)

    def shutdown(self, *, timeout: float = 3.0) -> None:
        """Quiesce workers before closing shared SQLite/vector resources.

        A timed-out worker remains visible and resources stay open so a caller
        can retry safely. Closing underneath a live digest can otherwise turn a
        slow shutdown into partial writes or use-after-close failures.
        """

        wait_timeout = max(0.0, float(timeout))
        with self._writer_lifecycle_lock:
            self._shutdown_requested.set()
            self._maintenance_stop.set()
        writer_error: Exception | None = None
        try:
            shutdown_writer(self, timeout=wait_timeout)
        except Exception as exc:
            writer_error = exc

        thread = self._journal_digest_thread
        if thread is not None and thread.is_alive():
            if thread is threading.current_thread():
                raise RuntimeError(
                    "Scope Recall journal digest cannot shut down its own provider"
                )
            thread.join(timeout=wait_timeout)
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                "Scope Recall journal digest did not acknowledge shutdown before timeout"
            ) from writer_error
        if writer_error is not None:
            raise writer_error

        with self._journal_digest_lock:
            self._journal_digest_thread = None
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        if self._vector_store is not None:
            self._vector_store.close()
            self._vector_store = None
        self._unregister_provider_instance()

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
        semantic_merge: bool = False,
        scope_mode: str | None = None,
    ) -> tuple[str, bool, str]:
        try:
            return store_memory_now(
                self,
                content=content,
                source=source,
                target=target,
                session_id=session_id,
                metadata=metadata,
                allow_duplicate=allow_duplicate,
                semantic_merge=semantic_merge,
                scope_mode=scope_mode,
            )
        except Exception:
            self._rollback_conn_after_error("store_now")
            raise

    def _find_semantic_merge_candidate(
        self, content: str, target: str
    ) -> tuple[str, str, str]:
        return find_semantic_merge_candidate(self, content, target)

    def _update_memory(self, memory_id: str, content: str, target: str | None = None) -> tuple[bool, str, str]:
        return update_memory(self, memory_id, content, target)

    def _merge_memories(self, target_id: str, source_ids: list[str], content: str | None = None, target: str | None = None) -> dict[str, Any]:
        return merge_memories(self, target_id, source_ids, content, target)

    def _export_memories(self, *, fmt: str = "jsonl", scope_only: bool = True) -> dict[str, Any]:
        return export_memories(self, fmt=fmt, scope_only=scope_only)

    def _govern_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return govern_memories(self, dry_run=dry_run, scope_only=scope_only)

    def _archive_memories(self, ids: list[str], *, reason: str = "scope_recall_forget", actor: str = "scope_recall_forget", batch_id: str = "") -> dict[str, Any]:
        return archive_memories(self, ids, reason=reason, actor=actor, batch_id=batch_id)

    def _fact_owned_memory_ids(self, ids: list[str]) -> list[str]:
        return fact_owned_memory_ids(self, ids)

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

    def _profile_payload(
        self,
        *,
        query: str = "",
        entity: str = "",
        targets: list[str] | None = None,
        include_general: bool = False,
        include_candidates: bool = False,
        include_curated: bool = True,
        limit: int = 5,
        max_chars: int = 1200,
    ) -> dict[str, Any]:
        return profile_payload(
            self,
            query=query,
            entity=entity,
            targets=targets,
            include_general=include_general,
            include_candidates=include_candidates,
            include_curated=include_curated,
            limit=limit,
            max_chars=max_chars,
        )

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

    def _benchmark_queries(
        self,
        *,
        queries: list[str] | None = None,
        cases: list[dict[str, Any]] | None = None,
        limit: int = 5,
        auto_explain_on_fail: bool = False,
        include_trace: bool = False,
        prompt_budget_chars: int = 0,
    ) -> dict[str, Any]:
        return benchmark_queries(
            self,
            queries=queries,
            cases=cases,
            limit=limit,
            auto_explain_on_fail=auto_explain_on_fail,
            include_trace=include_trace,
            prompt_budget_chars=prompt_budget_chars,
        )

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

    def _rollback_conn_after_error(self, context: str) -> None:
        """Clear a dirty shared SQLite transaction after an exception boundary.

        Scope Recall keeps one provider-owned SQLite connection open for the
        process lifetime. If any write path exits through an exception after
        SQLite has implicitly opened a transaction, that connection can keep the
        WAL write lock until process restart. Exception handlers that swallow or
        translate errors should call this helper before continuing.
        """
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                in_transaction = bool(conn.in_transaction)
            except sqlite3.ProgrammingError:
                if self._conn is conn:
                    self._conn = None
                return
            if not in_transaction:
                return
            try:
                conn.rollback()
            except Exception:
                logger.exception("Scope Recall SQLite rollback failed after %s", context)

    def _rollback_peer_provider_transactions(self, context: str) -> dict[str, int]:
        """Rollback dirty same-process peer providers that share this SQLite DB.

        A recoverable `database is locked` error can be caused by another live
        Scope Recall provider instance in the same process, not by the current
        connection. The process-local registry lets store recovery clear those
        peer dirty transactions before probing/reopening the current connection.
        """
        db_path = self._db_path
        result = {"peer_providers_checked": 0, "peer_rollbacks": 0, "peer_rollback_errors": 0}
        if db_path is None:
            return result
        with _PROVIDER_REGISTRY_LOCK:
            peers = [provider for provider in list(_PROVIDER_REGISTRY) if provider is not self]
        for peer in peers:
            peer_db_path = getattr(peer, "_db_path", None)
            if peer_db_path is None or Path(peer_db_path) != db_path:
                continue
            result["peer_providers_checked"] += 1
            peer_lock = getattr(peer, "_lock", None)
            if peer_lock is None:
                continue
            with peer_lock:
                peer_conn = getattr(peer, "_conn", None)
                if peer_conn is None or not getattr(peer_conn, "in_transaction", False):
                    continue
                try:
                    peer_conn.rollback()
                    result["peer_rollbacks"] += 1
                except Exception:
                    result["peer_rollback_errors"] += 1
                    logger.exception("Scope Recall peer SQLite rollback failed after %s", context)
        return result

    def _recover_sqlite_connection_after_error(self, context: str) -> dict[str, Any]:
        """Rollback/probe/reopen the provider SQLite connection after lock errors.

        This is intentionally conservative and is used only by `scope_recall_store`
        for SQLite lock/transaction errors. Non-SQLite exceptions still surface as
        errors so business-logic failures are not hidden by a retry.
        """
        payload: dict[str, Any] = {
            "recovered": False,
            "rolled_back": False,
            "reopened": False,
            "write_probe": False,
            "reconnect_pending": False,
        }
        payload.update(self._rollback_peer_provider_transactions(context))
        with self._lock:
            conn = self._conn
            if conn is None:
                return payload
            if conn.in_transaction:
                try:
                    conn.rollback()
                    payload["rolled_back"] = True
                except Exception:
                    logger.exception("Scope Recall SQLite rollback failed during recovery after %s", context)
            if self._sqlite_write_probe(conn):
                payload["recovered"] = True
                payload["write_probe"] = True
                return payload
            if self._db_path is None:
                return payload
            self._conn = None
            try:
                conn.close()
            except Exception:
                logger.exception("Scope Recall SQLite close failed during recovery after %s", context)
            try:
                reopened = self._open_runtime_connection()
                self._conn = reopened
                payload["reopened"] = True
                payload["write_probe"] = self._sqlite_write_probe(reopened)
                payload["recovered"] = bool(payload["write_probe"])
                payload["reconnect_pending"] = not payload["recovered"]
                if not payload["recovered"]:
                    self._conn = None
                    reopened.close()
            except Exception:
                self._conn = None
                payload["reconnect_pending"] = True
                logger.exception("Scope Recall SQLite reopen failed during recovery after %s", context)
            return payload

    def _sqlite_write_probe(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return True
        except Exception:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except Exception:
                logger.exception("Scope Recall SQLite write probe rollback failed")
            return False

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is not None:
            return conn
        if self._shutdown_requested.is_set():
            raise RuntimeError("Scope Recall is shutting down")
        with self._lock:
            if self._conn is None:
                self._conn = self._open_runtime_connection()
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
