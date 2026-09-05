"""Hermes MemoryProvider implementation for Scope Recall.

The provider owns runtime lifecycle, scope resolution, vector setup, journal capture, and tool registration while delegating domain logic to smaller modules."""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from agent.memory_provider import MemoryProvider  # type: ignore[reportMissingImports]

from .capture import (
    shutdown_writer,
    start_writer,
)
from .capture_control import new_write_queue
from .write_kernel import (
    TruthWriterLease,
    hold_positive_write_authority,  # noqa: F401
    holding_truth_writer_lease,
    sanitized_truth_writer_owner,
)
from .capture_llm import extract_capture_candidates  # noqa: F401 - compatibility hook
from .config import load_runtime_config, save_runtime_config
from .journal import ensure_journal_schema, run_journal_digest
from ._internal.compatibility.provider_runtime import (
    build_provider_runtime_dependencies,
)
from ._internal.runtime.composition import RuntimeComposition, assemble_runtime
from ._internal.runtime.process_lifecycle import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
from ._internal.runtime import peer_recovery as _peer_recovery
from ._internal.runtime.truth_session import TruthSession
from ._internal.runtime.background import BackgroundWork

from .maintenance_lease import (
    MaintenanceLeaseError,
    activation_lease_status,
    ensure_activation_guard_triggers,
    install_activation_lease_authorizer,
)
from .embedders import BaseEmbedder
from .gating import clean_text, config_bool, dedup_key, normalize_query, should_skip_retrieval
from .governance import extract_candidates  # noqa: F401 - compatibility hook
from ._internal.application.capture_journal import (
    CaptureTurnRequest,
    JournalMessagesRequest,
    JournalTurnRequest,
)
from ._internal.runtime.kernel import KERNEL
from ._internal.runtime.hooks import observe_memory_write
from ._internal.runtime.storage import (
    configure_published_writer_connection,
    finish_writer_schema_setup,
    open_configured_truth_connection,
    open_readonly_truth_connection,
    prepare_truth_schema,  # noqa: F401
    truth_has_unprocessed_journal,
)
from ._internal.experience.runtime import (
    backfill_skill_anchors,
    run_experience_preflight,
)
from ._internal.journal.tool_trace import tool_journal_content
from .memory_ops import (
    archive_memories,  # noqa: F401
    dedupe_memories,  # noqa: F401
    delete_memories,  # noqa: F401
    feedback_memory,  # noqa: F401
    fact_owned_memory_ids,
    find_semantic_merge_candidate,
    govern_memories,  # noqa: F401
    merge_memories,  # noqa: F401
    repair_vector,  # noqa: F401
    store_memory_now,  # noqa: F401 - compatibility patch point resolved by command gateway
    update_memory,  # noqa: F401
)
from .migration import migrate_legacy_scope_recall_storage
from .models import RecallItem, RuntimeScope, recall_scope_mode
from .recall import RecallService
from .prompting import render_current_turn_recall
from .provider_schemas import build_config_schema, build_tool_schemas
from .desktop_principal import (
    desktop_principal_from_config,
    is_desktop_platform,
    resolve_desktop_principal,
)
from .scope import (
    RUNTIME_STATUS_ACTIVE,
    RUNTIME_STATUS_ACTIVE_READ_ONLY,
    RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL,
    accessible_scope_ids,
    build_scope_id,
    build_shared_pool_scope_id,
    build_shared_scope_id,
    normalize_scope_identity,
    runtime_principal_status,
    writable_scope_ids,
)
from .source_isolation import scope_is_memory_isolated
from .sqlite_recovery import is_sqlite_lock_contention as _is_sqlite_lock_contention
from .sql_store import ensure_schema
from .storage_views import (
    search_curated_memories,
    search_db_memories,
    search_vector_memories,
    search_vector_memories_with_vector,
)
from .tooling import ScopeRecallToolService
from .truth_connection import connect_truth_database
from .vector_bootstrap import bootstrap_fresh_vector_companion
from .vector_runtime import setup_vector_layer
from .freshness import backfill_untracked_memory_freshness

logger = logging.getLogger(__name__)

# Monkeypatch anchors. Composition/bootstrap/prefetch resolve these at call time.
_COMPOSITION_RUNTIME_HOOKS = (
    setup_vector_layer,
    start_writer,
)
_BOOTSTRAP_RUNTIME_HOOKS = (
    holding_truth_writer_lease,
    save_runtime_config,
    open_configured_truth_connection,
    configure_published_writer_connection,
    bootstrap_fresh_vector_companion,
    activation_lease_status,
    MaintenanceLeaseError,
    install_activation_lease_authorizer,
)
_INITIALIZE_RUNTIME_HOOKS = (
    TruthWriterLease,
    sanitized_truth_writer_owner,
    desktop_principal_from_config,
    is_desktop_platform,
    resolve_desktop_principal,
    runtime_principal_status,
    migrate_legacy_scope_recall_storage,
    normalize_scope_identity,
    build_scope_id,
    build_shared_scope_id,
    accessible_scope_ids,
    writable_scope_ids,
    build_shared_pool_scope_id,
    _is_sqlite_lock_contention,
    backfill_untracked_memory_freshness,
    backfill_skill_anchors,
    finish_writer_schema_setup,
    ensure_schema,
    ensure_journal_schema,
    ensure_activation_guard_triggers,
    open_readonly_truth_connection,
    connect_truth_database,
    shutdown_writer,
    RUNTIME_STATUS_ACTIVE,
    RUNTIME_STATUS_ACTIVE_READ_ONLY,
)
_PREFETCH_RUNTIME_HOOKS = (
    render_current_turn_recall,
    run_experience_preflight,
    config_bool,
    logger,
)

SQLITE_BUSY_TIMEOUT_SECONDS = 10.0
STARTUP_FRESHNESS_BACKFILL_LIMIT = 500

_COMPOSITION_BIND_LOCK = threading.Lock()


class ScopeRecallMemoryProvider(MemoryProvider):
    """Hermes memory-provider runtime for Scope Recall.

    This class is the lifecycle boundary: it opens SQLite truth, configures scope visibility, starts background capture/digest work, and exposes tool schemas. Domain decisions live in helper modules so startup and shutdown remain auditable."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._retrieval_config: dict[str, Any] = {}
        self._vector_config: dict[str, Any] = {}
        self._composition = assemble_runtime(
            build_provider_runtime_dependencies(
                self, truth_cls=TruthSession, background_cls=BackgroundWork
            )
        )
        self._truth = self._composition.truth
        self._lock = threading.RLock()
        self._write_queue: queue.Queue[Any] = new_write_queue()
        self._writer_thread: threading.Thread | None = None
        # Queue-order receipts: a flush acknowledges every write job before its
        # marker and must report whether any of those jobs failed.  Keep only
        # the exception class for diagnostics so provider/auth text cannot leak
        # through the stats tool.
        self._writer_failed_writes = 0
        self._writer_reported_failures = 0
        self._writer_last_error_type = ""
        self._capture_queue_rejected = 0
        self._capture_queue_deferred = 0
        self._capture_queue_processing = 0
        self._stop = threading.Event()
        self._maintenance_stop = threading.Event()
        self._session_id = ""
        self._runtime_status = "uninitialized"
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
        self._vector_reason_code = "disabled_by_config"
        self._vector_auto_recoverable = False
        self._vector_repair_required = False
        self._vector_usable_for_query = False
        self._vector_debt_counts: dict[str, int] = {
            "pending": 0,
            "processing": 0,
            "retry": 0,
            "dead_letter": 0,
            "replayable": 0,
        }
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
        self._truth_writer_lease: TruthWriterLease | None = None
        self._truth_writer_role = "unknown"
        self._truth_writer_owner: dict[str, Any] = {}
        self._last_adjudication_at = 0.0
        self._last_adjudication_report: dict[str, Any] = {}
        self._recall_service = RecallService(self)
        self._tool_service = ScopeRecallToolService(self._composition.tool_port)
        self._shutdown_requested = threading.Event()
        self._capture_submission_lock = threading.RLock()
        self._writer_lifecycle_lock = threading.RLock()
        self._background = self._composition.background
        self._foreground_busy_count = 0
        self._writer_handoff_activity_lock = threading.RLock()
        self._writer_handoff_last_user_activity = time.monotonic()
        self._writer_handoff_last_truth_activity = time.monotonic()
        self._writer_handoff_activity_generation = 0
        self._writer_handoff_active_truth_work = 0
        self._writer_handoff_last_probe = 0.0
        self._writer_handoff_fenced = False
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

    @property
    def runtime_status(self) -> str:
        """Return the provider lifecycle state without touching storage."""

        return self._runtime_status

    def is_available(self) -> bool:
        return self._runtime_status != RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return build_config_schema()

    def save_config(
        self,
        values: Dict[str, Any],
        hermes_home: str,
        *,
        activation_lease_token: str = "",
    ) -> None:
        return self._bind_composition().bootstrap.save_config(
            values,
            hermes_home,
            activation_lease_token=activation_lease_token,
        )

    def _bootstrap_storage(
        self,
        hermes_home: str | os.PathLike[str],
        *,
        activation_lease_token: str = "",
    ) -> None:
        return self._bind_composition().bootstrap.bootstrap_storage(
            hermes_home,
            activation_lease_token=activation_lease_token,
        )

    def _bootstrap_vector_companion(
        self,
        storage_dir: Path,
        runtime_config: dict[str, Any],
        *,
        truth_conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        return self._bind_composition().bootstrap.bootstrap_vector_companion(
            storage_dir,
            runtime_config,
            truth_conn=truth_conn,
        )

    def _open_runtime_connection(self) -> sqlite3.Connection:
        return self._bind_composition().bootstrap.open_runtime_connection()

    def initialize(self, session_id: str, **kwargs) -> None:
        return self._bind_composition().initialize(session_id, **kwargs)

    def _has_live_initialize_runtime(self) -> bool:
        return self._bind_composition().lifecycle.has_live_initialize_runtime()

    def _initialize_under_lifecycle_lock(self, session_id: str, **kwargs) -> None:
        return self._bind_composition().lifecycle.initialize_under_lifecycle_lock(
            session_id, kwargs
        )

    def _initialize_writer_runtime(self) -> None:
        return self._bind_composition().lifecycle.initialize_writer_runtime()

    def _cleanup_failed_writer_initialization(
        self, *, reraise_companion_errors: bool = False
    ) -> bool:
        return self._bind_composition().lifecycle.cleanup_failed_writer_initialization(
            reraise_companion_errors=reraise_companion_errors
        )

    def _initialize_read_only_runtime(self) -> None:
        return self._bind_composition().lifecycle.initialize_read_only_runtime()

    def _truth_writes_blocked(self) -> bool:
        """Return whether durable write surfaces must stay disabled."""

        if (
            self._shutdown_requested.is_set()
            or self._truth_writer_role != "owner"
            or bool(getattr(self, "_writer_handoff_fenced", False))
        ):
            return True
        storage_dir = getattr(self, "_storage_dir", None)
        if storage_dir is None:
            return False
        from .writer_lease import process_writer_handoff_state
        from ._internal.runtime.writer_handoff import (
            current_truth_work_started_before_fence,
        )

        state = process_writer_handoff_state(storage_dir)
        with state.lock:
            if bool(getattr(state, "release_uncertain", False)):
                return True
            if bool(getattr(state, "handoff_fenced", False)):
                return not current_truth_work_started_before_fence(self)
            return False

    def _register_provider_instance(self) -> None:
        _peer_recovery.register_provider_instance(self)

    def _unregister_provider_instance(self) -> None:
        _peer_recovery.unregister_provider_instance(self)

    def _runtime_memory_disabled(self) -> bool:
        return self._runtime_status == RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL

    def _memory_isolated_for_scope(self) -> bool:
        """Return whether this runtime is excluded from every memory surface."""

        return self._runtime_memory_disabled() or scope_is_memory_isolated(
            getattr(self, "_scope", None), self._config
        )

    def system_prompt_block(self) -> str:
        if self._runtime_memory_disabled():
            return (
                "# Scope Recall Memory\n"
                "Disabled because the trusted runtime user principal is missing "
                f"(status={RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL}). "
                "Do not read, infer, or store durable memory in this runtime."
            )
        if self._memory_isolated_for_scope():
            return (
                "# Scope Recall Memory\n"
                "Disabled for this chat by explicit source-isolation policy. "
                "Do not read, infer, or store durable user preferences or memories from this chat."
            )
        if self._truth_writes_blocked():
            return (
                "# Scope Recall Memory\n"
                "Active in read-only recall mode: another Scope Recall process "
                "(usually the gateway) currently owns the truth-database writer "
                "lease. Recall/search works, but do not attempt to store, "
                "update, merge, or forget memories from this runtime; durable "
                "writes belong to the writer process."
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
        from ._internal.runtime.writer_handoff import note_user_activity

        note_user_activity(self)
        self._current_turn = int(turn_number or 0)
        if self._truth_writes_blocked():
            self._maybe_promote_to_writer()
            return

    def _maybe_promote_to_writer(self) -> None:
        return self._bind_composition().promote_to_writer()

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        del query, session_id
        return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._recall_service.prefetch_prompt(query, session_id=session_id)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: list[dict[str, Any]] | None = None) -> None:
        """Synchronize one Hermes turn into capture, journal, recall, and prompt context state.

        This method is on the hot path, so it avoids heavyweight repair work and records failures as sanitized diagnostics instead of blocking the main agent loop.
        """
        self._foreground_busy_count = int(getattr(self, "_foreground_busy_count", 0) or 0) + 1
        try:
            self._sync_turn_unlocked(user_content, assistant_content, session_id=session_id, messages=messages)
        finally:
            self._foreground_busy_count = max(0, int(getattr(self, "_foreground_busy_count", 0) or 0) - 1)

    def _sync_turn_unlocked(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: list[dict[str, Any]] | None = None) -> None:
        del session_id
        if self._memory_isolated_for_scope():
            return
        if self._truth_writes_blocked():
            return
        if messages:
            self._append_session_tool_journal(messages)
        if not config_bool(self._config, "auto_capture", True):
            return
        if self._scope.agent_context != "primary":
            return

        runtime = self._bind_composition()
        plan = runtime.capture.prepare_turn(
            CaptureTurnRequest(user_content, assistant_content)
        )
        if plan.journal_user_allowed or plan.journal_assistant_allowed:
            if runtime.journal.append_turn(
                JournalTurnRequest(
                    clean_user=plan.clean_user,
                    clean_assistant=plan.clean_assistant,
                    user_allowed=plan.journal_user_allowed,
                    assistant_allowed=plan.journal_assistant_allowed,
                )
            ):
                self._maybe_start_background_journal_digest()
        runtime.capture.capture_turn(plan)

    def _event_digest_config(self) -> dict[str, Any]:
        raw = self._config.get("event_digest")
        return raw if isinstance(raw, dict) else {}

    def _dry_run_event_candidates(self, *, kind: str, messages: List[Dict[str, Any]]) -> dict[str, Any]:
        from .event_digest import run_provider_event_candidate_pass

        return run_provider_event_candidate_pass(self, kind=kind, messages=messages)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Provide recall context before Hermes compresses the conversation.

        The hook should be compact and safe because it is injected into compression prompts, not treated as new user truth.
        """
        staged = self._bind_composition().journal.stage_pre_compress(
            self._journal_messages_request(messages)
        )
        appended = staged.appended
        roles = staged.roles
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
        observe_memory_write(
            agent_context=self._scope.agent_context,
            action=action,
            target=target,
            content=content,
            metadata=metadata,
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._memory_isolated_for_scope():
            return
        if self._truth_writes_blocked():
            return
        self._append_session_tool_journal(messages)
        self._bind_composition().capture.flush(3.0)
        self._run_session_end_journal_digest()

    def _journal_config(self) -> dict[str, Any]:
        raw_journal = self._config.get("journal")
        return raw_journal if isinstance(raw_journal, dict) else {}

    def _append_session_tool_journal(self, messages: List[Dict[str, Any]]) -> None:
        self._bind_composition().journal.append_session_tools(
            self._journal_messages_request(messages)
        )

    @staticmethod
    def _journal_messages_request(
        messages: List[Dict[str, Any]],
    ) -> JournalMessagesRequest:
        return JournalMessagesRequest(
            tuple(cast(dict[str, object], dict(message)) for message in messages)
        )

    def _tool_journal_content(self, message: Dict[str, Any]) -> str:
        """Decide how much tool result content should be captured into the journal."""

        return tool_journal_content(
            message,
            journal_config=self._journal_config(),
            runtime_config=self._config,
        )

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

    def _has_unprocessed_journal(self) -> bool:
        return truth_has_unprocessed_journal(self)

    def _maybe_start_background_journal_digest(self) -> None:
        self._background_work().maybe_start_journal_digest()

    def _run_background_journal_digest(self, journal_config: dict[str, Any]) -> None:
        self._background_work().run_digest(journal_config, digest_fn=run_journal_digest)

    def _run_session_end_journal_digest(self) -> None:
        self._background_work().run_session_end_digest(digest_fn=run_journal_digest)

    def _maybe_run_auto_adjudication(self, *, trigger: str) -> None:
        self._background_work().maybe_adjudicate(trigger=trigger)

    def _maybe_run_auto_experience_promotion(self, *, trigger: str) -> None:
        self._background_work().maybe_promote(trigger=trigger)

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
        if self._runtime_memory_disabled():
            return {}
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
        return self.route_tool(tool_name, args, **kwargs)

    def route_tool(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        if self._runtime_memory_disabled():
            from tools.registry import tool_error  # type: ignore[reportMissingImports]

            return tool_error(RUNTIME_STATUS_DISABLED_MISSING_PRINCIPAL)
        if self._memory_isolated_for_scope():
            from tools.registry import tool_error  # type: ignore[reportMissingImports]

            return tool_error("scope-recall memory is disabled for this chat")
        if not config_bool(self._schema_config(), "enable_tools", True):
            from tools.registry import tool_error  # type: ignore[reportMissingImports]

            return tool_error("scope-recall tools are disabled by configuration")
        if self._scope.agent_context != "primary":
            from tools.registry import tool_error  # type: ignore[reportMissingImports]

            return tool_error("scope-recall tools are only available in the primary agent context")
        return self._tool_service.handle(tool_name, args)

    def query_connection(self) -> sqlite3.Connection:
        return self._require_conn()

    def query_lock(self) -> Any:
        return self._lock

    def query_scope_view(self) -> dict[str, Any]:
        return {
            "scope_id": str(getattr(self, "_scope_id", "") or ""),
            "shared_scope_id": str(getattr(self, "_shared_scope_id", "") or ""),
            "accessible_scope_ids": list(getattr(self, "_accessible_scope_ids", []) or []),
            "writable_scope_ids": list(getattr(self, "_writable_scope_ids", []) or []),
            "shared_pool_scope_id": str(getattr(self, "_shared_pool_scope_id", "") or ""),
        }

    def vector_status_view(self) -> dict[str, Any]:
        return cast(
            dict[str, Any], self._bind_composition().vector.status_payload()
        )

    def retrieval_status_view(self) -> dict[str, Any]:
        config = dict(getattr(self, "_retrieval_config", None) or {})
        from ._internal.recall.weights import effective_retrieval_weights

        config.update(effective_retrieval_weights(config))
        return {
            "config": config,
            "mode": str(config.get("mode") or "lexical"),
            "lexical_weight": float(config.get("lexical_weight") or 1.0),
            "vector_weight": float(config.get("vector_weight") or 0.0),
        }

    def runtime_status_view(self) -> dict[str, Any]:
        from ._internal.runtime.writer_handoff import writer_handoff_status
        from .curation_observability import curation_observability_config
        from .fact_observability import fact_observability_config

        thread = getattr(self, "_writer_thread", None)
        digest_thread = getattr(self, "_journal_digest_thread", None)
        db_path = getattr(self, "_db_path", None)
        observability_config = {
            **fact_observability_config(self._config),
            **curation_observability_config(self._config),
        }
        return {
            "status": str(getattr(self, "_runtime_status", "") or ""),
            "name": self.name,
            "hermes_home": getattr(self, "_hermes_home", None),
            "db_path": str(db_path) if db_path else "",
            "observability_config": observability_config,
            "truth_writer_role": str(getattr(self, "_truth_writer_role", "unknown") or "unknown"),
            "truth_writer_owner": sanitized_truth_writer_owner(
                getattr(self, "_truth_writer_owner", {})
            ),
            "writer_handoff": writer_handoff_status(self),
            "last_adjudication_report": dict(
                getattr(self, "_last_adjudication_report", {}) or {}
            ),
            "shared_pool_enabled": bool(getattr(self, "_shared_pool_enabled", False)),
            "shared_pool_write_enabled": bool(
                getattr(self, "_shared_pool_write_enabled", False)
            ),
            "shared_pool_id": str(getattr(self, "_shared_pool_id", "") or ""),
            "migration_info": dict(getattr(self, "_migration_info", {}) or {}),
            "writer_thread_alive": bool(thread is not None and thread.is_alive()),
            "writer_failed_writes": int(getattr(self, "_writer_failed_writes", 0) or 0),
            "writer_reported_failures": int(
                getattr(self, "_writer_reported_failures", 0) or 0
            ),
            "writer_last_error_type": str(
                getattr(self, "_writer_last_error_type", "") or ""
            ),
            "freshness_backfill": dict(getattr(self, "_freshness_backfill", {}) or {}),
            "journal_digest_thread_alive": bool(
                digest_thread is not None and digest_thread.is_alive()
            ),
            "journal_digest_last_started": float(
                getattr(self, "_last_journal_digest_started", 0.0) or 0.0
            ),
            "journal_digest_last_finished": float(
                getattr(self, "_last_journal_digest_finished", 0.0) or 0.0
            ),
            "journal_digest_last_status": str(
                getattr(self, "_last_journal_digest_status", "never_run") or "never_run"
            ),
            "journal_digest_last_error": str(
                getattr(self, "_last_journal_digest_error", "") or ""
            ),
            "journal_digest_consecutive_failures": int(
                getattr(self, "_journal_digest_consecutive_failures", 0) or 0
            ),
        }

    def recall_service_view(self) -> Any:
        return self._recall_service

    def recall_limit(self) -> int:
        return self._retrieve_limit()

    def shutdown(
        self, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    ) -> None:
        self._bind_composition().shutdown(timeout=timeout)

    def flush(self, timeout: float = 2.0) -> bool:
        if self._runtime_memory_disabled():
            return True
        return self._bind_composition().capture.flush(timeout)

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
        # The compatibility gateway resolves this module's store_memory_now at
        # execution time so existing patch points remain valid.
        return KERNEL.store(
            self._composition.command_port,
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata,
            allow_duplicate=allow_duplicate,
            semantic_merge=semantic_merge,
            scope_mode=scope_mode,
        )

    def _find_semantic_merge_candidate(
        self, content: str, target: str
    ) -> tuple[str, str, str]:
        return find_semantic_merge_candidate(self, content, target)

    def _update_memory(self, memory_id: str, content: str, target: str | None = None) -> tuple[bool, str, str]:
        return KERNEL.update(self._composition.command_port, memory_id, content, target)

    def _merge_memories(self, target_id: str, source_ids: list[str], content: str | None = None, target: str | None = None) -> dict[str, Any]:
        return KERNEL.merge(self._composition.command_port, target_id, source_ids, content, target)

    def _export_memories(self, *, fmt: str = "jsonl", scope_only: bool = True) -> dict[str, Any]:
        return KERNEL.export(self._composition.query_port, fmt=fmt, scope_only=scope_only)

    def _govern_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return KERNEL.govern(self._composition.command_port, dry_run=dry_run, scope_only=scope_only)

    def _archive_memories(self, ids: list[str], *, reason: str = "scope_recall_forget", actor: str = "scope_recall_forget", batch_id: str = "") -> dict[str, Any]:
        return KERNEL.archive(self._composition.command_port, ids, reason=reason, actor=actor, batch_id=batch_id)

    def _fact_owned_memory_ids(self, ids: list[str]) -> list[str]:
        return fact_owned_memory_ids(self, ids)

    def _delete_memories(self, ids: list[str]) -> int:
        return KERNEL.delete(self._composition.command_port, ids).deleted_count

    def _dedupe_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return KERNEL.dedupe(self._composition.command_port, dry_run=dry_run, scope_only=scope_only)

    def _repair_vector(self) -> dict[str, Any]:
        return KERNEL.repair(self._composition.command_port)

    def _hygiene_report(self, *, limit: int = 200) -> dict[str, Any]:
        return KERNEL.hygiene(self._composition.query_port, limit=limit)

    def _context_payload(self, *, query: str, limit: int = 5, max_chars: int = 900) -> dict[str, Any]:
        return KERNEL.context(self._composition.query_port, query=query, limit=limit, max_chars=max_chars)

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
        return KERNEL.profile(
            self._composition.query_port,
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
        return KERNEL.probe(self._composition.query_port, entity=entity, limit=limit)

    def _related_entities(self, *, entity: str, limit: int = 12) -> dict[str, Any]:
        return KERNEL.related(self._composition.query_port, entity=entity, limit=limit)

    def _feedback_memory(self, *, memory_id: str, rating: str, note: str = "") -> dict[str, Any]:
        return KERNEL.feedback(self._composition.command_port, memory_id=memory_id, rating=rating, note=note)

    def _inspect_memory(self, *, memory_id: str) -> dict[str, Any]:
        return KERNEL.inspect(self._composition.query_port, memory_id=memory_id)

    def _explain_query(self, *, query: str, limit: int = 5) -> dict[str, Any]:
        return KERNEL.explain(self._composition.query_port, query=query, limit=limit)

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
        return KERNEL.benchmark(
            self._composition.query_port,
            queries=queries,
            cases=cases,
            limit=limit,
            auto_explain_on_fail=auto_explain_on_fail,
            include_trace=include_trace,
            prompt_budget_chars=prompt_budget_chars,
        )

    def _embed_query_variants(self, queries: List[str]) -> List[List[float]]:
        return [
            list(row)
            for row in self._bind_composition().vector.embed_query_variants(
                tuple(queries)
            )
        ]

    def _search_vector_memories(self, query: str, *, limit: int) -> List[RecallItem]:
        return search_vector_memories(self, query, limit=limit)

    def _search_vector_memories_with_vector(
        self,
        query_vector: List[float],
        *,
        limit: int,
    ) -> List[RecallItem]:
        return search_vector_memories_with_vector(self, query_vector, limit=limit)

    def _search_curated_memories(self, query: str) -> List[RecallItem]:
        return search_curated_memories(self, query)

    def _mark_recalled(self, memory_ids: List[str]) -> None:
        for memory_id in memory_ids:
            self._last_recall_turns[memory_id] = self._current_turn

    def _stats_payload(self) -> Dict[str, Any]:
        return KERNEL.stats(self._composition.query_port)

    def _retrieve_limit(self) -> int:
        max_items = int(self._config_value("auto_recall_max_items", 3))
        max_per_turn = int(self._config_value("max_recall_per_turn", 10))
        return max(1, min(max_items * 3, max_per_turn * 2, 20))

    def _bind_composition(self) -> RuntimeComposition:
        composition = getattr(self, "_composition", None)
        if composition is not None:
            return composition
        with _COMPOSITION_BIND_LOCK:
            composition = getattr(self, "_composition", None)
            if composition is not None:
                return composition
            composition = assemble_runtime(
                build_provider_runtime_dependencies(
                    self, truth_cls=TruthSession, background_cls=BackgroundWork
                )
            )
            object.__setattr__(self, "_composition", composition)
            object.__setattr__(self, "_truth", composition.truth)
            object.__setattr__(self, "_background", composition.background)
            return composition

    def _truth_session(self) -> TruthSession:
        return self._bind_composition().truth

    def _background_work(self) -> BackgroundWork:
        return self._bind_composition().background

    def _close_published_connection(
        self,
        conn: Any,
        *,
        context: str,
        reraise: bool = True,
    ) -> bool:
        return self._truth_session().close_published(conn, context=context, reraise=reraise)

    def _quarantine_sqlite_connection(self, conn: Any, context: str) -> None:
        """Detach and close a connection whose transactional state is untrusted."""

        self._truth_session().quarantine(conn, context)

    def _rollback_conn_after_error(self, context: str) -> None:
        """Clear a dirty shared SQLite transaction after an exception boundary.

        Scope Recall keeps one provider-owned SQLite connection open for the
        process lifetime. If any write path exits through an exception after
        SQLite has implicitly opened a transaction, that connection can keep the
        WAL write lock until process restart. Exception handlers that swallow or
        translate errors should call this helper before continuing.

        Inspection, rollback, quarantine, and any resulting role demotion stay
        under ``_writer_lifecycle_lock`` then ``self._lock``. Callers that
        already hold the lifecycle RLock, including ``_store_now`` and
        ToolService, may re-enter.
        """
        with self._writer_lifecycle_lock:
            self._rollback_conn_after_error_under_lifecycle_lock(context)

    def _rollback_conn_after_error_under_lifecycle_lock(self, context: str) -> None:
        """Run rollback/quarantine/demotion while lifecycle is already held."""

        self._truth_session().rollback_after_error(context)

    def _rollback_peer_provider_transactions(self, context: str) -> dict[str, int]:
        return _peer_recovery.rollback_peer_provider_transactions(self, context)

    def _recover_sqlite_connection_after_error(self, context: str) -> dict[str, Any]:
        """Rollback/probe/reopen the provider SQLite connection after lock errors.

        This is intentionally conservative and is used only at explicit
        idempotent retry boundaries such as `scope_recall_store` and background
        journal digest. Non-SQLite exceptions still surface so business-logic
        failures are not hidden by a retry.

        Peer recovery and this instance's rollback/probe/close/reopen/demotion
        stay under ``_writer_lifecycle_lock`` so an authorized write unit cannot
        observe a published demotion mid-commit. Lock order is lifecycle, then
        ``self._lock`` / SQLite. Reentrant callers from ``_store_now`` and
        ToolService are safe because the gate is an RLock.
        """
        with self._writer_lifecycle_lock:
            return self._recover_sqlite_connection_after_error_under_lifecycle_lock(
                context
            )

    def _recover_sqlite_connection_after_error_under_lifecycle_lock(
        self, context: str
    ) -> dict[str, Any]:
        """Run peer and own recovery while lifecycle is already held."""

        return self._truth_session().recover_after_error(
            context,
            peer_rollback=self._rollback_peer_provider_transactions,
            open_writer=self._open_runtime_connection,
            write_probe=self._sqlite_write_probe,
        )

    def _sqlite_write_probe(self, conn: sqlite3.Connection) -> bool:
        return self._truth_session().probe_write(conn)

    def _require_conn(self) -> sqlite3.Connection:
        return self._truth_session().require()

    @property
    def _conn(self) -> sqlite3.Connection | None:
        truth = getattr(self, "_truth", None)
        if truth is None:
            return None
        return truth._conn

    @_conn.setter
    def _conn(self, value: sqlite3.Connection | None) -> None:
        self._truth_session()._conn = value

    @property
    def _journal_digest_thread(self) -> threading.Thread | None:
        return self._background_work().thread

    @_journal_digest_thread.setter
    def _journal_digest_thread(self, value: threading.Thread | None) -> None:
        self._background_work().thread = value

    @property
    def _journal_digest_lock(self) -> threading.RLock:
        return self._background_work().lock

    @_journal_digest_lock.setter
    def _journal_digest_lock(self, value: threading.RLock) -> None:
        self._background_work().lock = value

    @property
    def _last_journal_digest_started(self) -> float:
        return self._background_work().last_started

    @_last_journal_digest_started.setter
    def _last_journal_digest_started(self, value: float) -> None:
        self._background_work().last_started = value

    @property
    def _last_journal_digest_finished(self) -> float:
        return self._background_work().last_finished

    @_last_journal_digest_finished.setter
    def _last_journal_digest_finished(self, value: float) -> None:
        self._background_work().last_finished = value

    @property
    def _last_journal_digest_status(self) -> str:
        return self._background_work().last_status

    @_last_journal_digest_status.setter
    def _last_journal_digest_status(self, value: str) -> None:
        self._background_work().last_status = value

    @property
    def _last_journal_digest_error(self) -> str:
        return self._background_work().last_error

    @_last_journal_digest_error.setter
    def _last_journal_digest_error(self, value: str) -> None:
        self._background_work().last_error = value

    @property
    def _journal_digest_consecutive_failures(self) -> int:
        return self._background_work().consecutive_failures

    @_journal_digest_consecutive_failures.setter
    def _journal_digest_consecutive_failures(self, value: int) -> None:
        self._background_work().consecutive_failures = value

    @property
    def _journal_digest_needs_resume(self) -> bool:
        return self._background_work().needs_resume

    @_journal_digest_needs_resume.setter
    def _journal_digest_needs_resume(self, value: bool) -> None:
        self._background_work().needs_resume = value

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

    def store_now(
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
        """Public store command. Instance ``_store_now`` patches still apply."""
        return self._store_now(
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata,
            allow_duplicate=allow_duplicate,
            semantic_merge=semantic_merge,
            scope_mode=scope_mode,
        )

    def rollback_conn_after_error(self, context: str) -> None:
        """Public cleanup declared by MemoryCommandPort."""
        self._rollback_conn_after_error(context)

    def config_view(self) -> dict[str, Any]:
        return dict(self._config)

    def config_value(self, key: str, default: Any = None) -> Any:
        return self._config_value(key, default)

    def clean_text(self, text: Any) -> str:
        return self._clean_text(text)

    def scope_id(self) -> str:
        return str(self.query_scope_view().get("scope_id") or "")

    def shared_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_scope_id") or "")

    def shared_pool_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_pool_scope_id") or "")

    def writable_scope_ids(self) -> list[str]:
        return [
            str(item)
            for item in (self.query_scope_view().get("writable_scope_ids") or [])
            if str(item)
        ]

    def has_positive_write_authority(self) -> bool:
        from .write_kernel import has_positive_write_authority as check_authority

        return check_authority(self)

    def writer_lifecycle_lock(self) -> Any:
        return self._writer_lifecycle_lock

    def command_update_memory(
        self, memory_id: str, content: str, target: str | None = None
    ) -> tuple[bool, str, str]:
        return self._update_memory(memory_id, content, target)

    def command_merge_memories(
        self,
        target_id: str,
        source_ids: list[str],
        content: str | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        return self._merge_memories(target_id, source_ids, content, target)

    def command_archive_memories(
        self,
        ids: list[str],
        *,
        reason: str = "scope_recall_forget",
        actor: str = "scope_recall_forget",
        batch_id: str = "",
    ) -> dict[str, Any]:
        return self._archive_memories(ids, reason=reason, actor=actor, batch_id=batch_id)

    def command_delete_memories(self, ids: list[str]) -> int:
        return self._delete_memories(ids)

    def command_feedback_memory(self, *, memory_id: str, rating: str, note: str = "") -> dict[str, Any]:
        return self._feedback_memory(memory_id=memory_id, rating=rating, note=note)

    def command_govern_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return self._govern_memories(dry_run=dry_run, scope_only=scope_only)

    def command_dedupe_memories(self, *, dry_run: bool = True, scope_only: bool = True) -> dict[str, Any]:
        return self._dedupe_memories(dry_run=dry_run, scope_only=scope_only)

    def command_repair_vector(self) -> dict[str, Any]:
        return self._repair_vector()

    def fact_owned_memory_ids(self, ids: list[str]) -> list[str]:
        return self._fact_owned_memory_ids(ids)


def register(ctx) -> None:
    ctx.register_memory_provider(ScopeRecallMemoryProvider())
