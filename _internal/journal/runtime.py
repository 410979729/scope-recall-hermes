"""Provider-facing journal append and background digest. Owns the lock and connection."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Callable

from ...capture_filters import sanitize_report_text
from ...gating import compact_text, config_bool
from ...journal_store import append_journal_entry, ensure_journal_schema
from ...sqlite_recovery import is_sqlite_lock_contention

logger = logging.getLogger(__name__)


def append_scoped_journal_entries(
    provider: Any,
    entries: list[dict[str, Any]],
) -> int:
    """Append one or more scoped journal rows under the provider lock.

    Provider hooks must not open SQLite themselves. This helper is the
    journal write port for Hermes lifecycle capture.
    """

    if not entries:
        return 0
    appended = 0
    with provider._lock:
        conn = provider._require_conn()
        ensure_journal_schema(conn, commit=False)
        opened = not bool(getattr(conn, "in_transaction", False))
        if opened:
            conn.execute("BEGIN IMMEDIATE")
        try:
            for entry in entries:
                inserted = append_journal_entry(
                    conn,
                    scope=provider._scope,
                    scope_id=provider._scope_id,
                    shared_scope_id=provider._shared_scope_id,
                    session_id=provider._session_id,
                    turn_number=int(entry.get("turn_number") or 0),
                    role=str(entry.get("role") or ""),
                    content=entry.get("content"),
                    metadata=entry.get("metadata"),
                )
                if inserted:
                    appended += 1
            if opened:
                conn.commit()
        except Exception:
            if opened and getattr(conn, "in_transaction", False):
                conn.rollback()
            raise
    return appended


def run_provider_background_journal_digest(
    provider: Any,
    journal_config: dict[str, Any],
    *,
    digest_fn: Callable[..., dict[str, Any]],
) -> None:
    """Drain journal in the background using a caller-supplied digest function."""

    if provider._shutdown_requested.is_set():
        with provider._journal_digest_lock:
            provider._last_journal_digest_status = "skipped"
            provider._last_journal_digest_error = "shutdown requested"
            provider._last_journal_digest_finished = time.time()
        return
    if provider._memory_isolated_for_scope():
        with provider._journal_digest_lock:
            provider._last_journal_digest_status = "skipped"
            provider._last_journal_digest_error = "source-isolated chat"
            provider._last_journal_digest_finished = time.time()
        return
    hermes_home = provider._hermes_home
    if hermes_home is None:
        with provider._journal_digest_lock:
            provider._last_journal_digest_status = "skipped"
            provider._last_journal_digest_error = "missing hermes_home"
            provider._last_journal_digest_finished = time.time()
        return
    extractor = str(journal_config.get("extractor") or "llm").strip().lower()
    drain_while_idle = config_bool(journal_config, "background_digest_drain_while_idle", True)
    synchronous = config_bool(journal_config, "background_digest_synchronous", False)
    max_passes = 1
    if drain_while_idle and not synchronous:
        try:
            max_passes = max(1, int(journal_config.get("background_digest_max_passes") or 20))
        except (TypeError, ValueError):
            max_passes = 20
    pause = provider._coerce_journal_float(
        journal_config, "background_digest_idle_pause_seconds", 0.4
    )

    def run_once() -> dict[str, Any]:
        return digest_fn(
            hermes_home=hermes_home,
            extractor=extractor,
            scope=provider._background_digest_scope(),
            interval_label=f"background-{journal_config.get('digest_interval_hours', 2)}h",
            limit_entries=None,
            dry_run=False,
        )

    last_result: dict[str, Any] | None = None
    any_ok = False
    leftover = False
    try:
        for pass_index in range(max_passes):
            if provider._shutdown_requested.is_set():
                break
            if pass_index and int(getattr(provider, "_foreground_busy_count", 0) or 0) > 0:
                leftover = True
                break
            try:
                result = run_once()
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_contention(exc):
                    raise
                recovery = provider._recover_sqlite_connection_after_error(
                    "background journal digest lock contention"
                )
                if not bool(recovery.get("recovered")):
                    raise
                logger.warning(
                    "Scope Recall recovered SQLite lock contention; retrying background journal digest once"
                )
                result = run_once()
            last_result = result
            ok = bool(result.get("ok", result.get("status") == "ok"))
            status = "ok" if ok else str(result.get("status") or "error")
            error = "" if ok else compact_text(sanitize_report_text(str(result.get("error") or result.get("message") or result)), 240)
            with provider._journal_digest_lock:
                provider._last_journal_digest_finished = time.time()
                provider._last_journal_digest_status = status
                provider._last_journal_digest_error = error
                provider._journal_digest_consecutive_failures = 0 if ok else provider._journal_digest_consecutive_failures + 1
            if not ok:
                leftover = True
                break
            any_ok = True
            backlog_after = result.get("backlog_after")
            try:
                remaining = int(backlog_after) if backlog_after is not None else 0
            except (TypeError, ValueError):
                remaining = 0
            processed = int(result.get("processed_entries") or 0)
            try:
                delta = int(result.get("backlog_delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            if remaining <= 0:
                leftover = False
                break
            leftover = True
            if processed <= 0 and delta >= 0:
                break
            if pass_index + 1 >= max_passes:
                break
            if pause > 0:
                time.sleep(min(pause, 5.0))
        if any_ok and not provider._shutdown_requested.is_set():
            work = getattr(provider, "_background", None)
            if work is not None:
                work.maybe_promote(trigger="background-journal-digest")
                work.maybe_adjudicate(trigger="background-journal-digest")
    except Exception as exc:
        leftover = True
        provider._rollback_conn_after_error("background journal digest")
        with provider._journal_digest_lock:
            provider._last_journal_digest_finished = time.time()
            provider._last_journal_digest_status = "error"
            provider._last_journal_digest_error = compact_text(sanitize_report_text(str(exc)), 240)
            provider._journal_digest_consecutive_failures += 1
        logger.exception("Scope Recall background journal digest failed")
    finally:
        provider._journal_digest_needs_resume = bool(leftover)
        if last_result is None:
            return


def run_provider_session_end_journal_digest(
    provider: Any,
    *,
    digest_fn: Callable[..., dict[str, Any]],
) -> None:
    if provider._shutdown_requested.is_set():
        return
    if provider._truth_writes_blocked():
        return
    if provider._memory_isolated_for_scope() or provider._hermes_home is None or provider._scope.agent_context != "primary":
        return
    journal_config = provider._journal_config()
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
        result = digest_fn(
            hermes_home=provider._hermes_home,
            extractor=extractor,
            scope=provider._scope,
            interval_label="session-end",
            limit_entries=max(1, limit_entries),
            dry_run=False,
        )
        if result.get("ok", result.get("status") == "ok"):
            work = getattr(provider, "_background", None)
            if work is not None:
                work.maybe_promote(trigger="session-end-journal-digest")
    except Exception:
        provider._rollback_conn_after_error("session-end journal digest")
        logger.exception("Scope Recall session-end journal digest failed")


def append_session_tool_journal_entries(provider: Any, messages: list[dict[str, Any]]) -> None:
    if provider._memory_isolated_for_scope() or not messages or provider._scope.agent_context != "primary":
        return
    journal_config = provider._journal_config()
    if not config_bool(journal_config, "enabled", True):
        return
    entries: list[dict[str, Any]] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("type") or "").strip().lower()
        if role != "tool":
            continue
        content = provider._tool_journal_content(message)
        if not content:
            continue
        entries.append(
            {
                "role": "tool",
                "content": content,
                "turn_number": index,
                "metadata": {
                    "source": "session-end-tool-trace",
                    "tool_name": str(message.get("name") or message.get("tool_name") or ""),
                    "message_index": index,
                },
            }
        )
    append_scoped_journal_entries(provider, entries)


def stage_pre_compress_journal_entries(provider: Any, messages: list[dict[str, Any]]) -> tuple[int, set[str]]:
    from ...capture_filters import sanitize_capture_text, should_capture_text
    from ...gating import config_bool

    if provider._memory_isolated_for_scope() or not messages or not config_bool(provider._config, "auto_capture", True):
        return 0, set()
    if provider._truth_writes_blocked():
        return 0, set()
    if provider._scope.agent_context != "primary":
        return 0, set()
    journal_config = provider._journal_config()
    if not config_bool(journal_config, "enabled", True):
        return 0, set()
    filter_config = dict(provider._config)
    filter_config["capture_hard_max_chars"] = -1
    entries: list[dict[str, Any]] = []
    roles: set[str] = set()
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("type") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = sanitize_capture_text(provider._clean_text(message.get("content")))
        if not content:
            continue
        if not should_capture_text(content, filter_config).allowed:
            continue
        entries.append(
            {
                "role": role,
                "content": content,
                "turn_number": index,
                "metadata": {
                    "source": "pre-compression",
                    "compression_boundary": True,
                    "message_index": index,
                },
            }
        )
        roles.add(role)
    return append_scoped_journal_entries(provider, entries), roles


def append_turn_journal_entries(
    provider: Any,
    *,
    clean_user: str,
    clean_assistant: str,
    user_allowed: bool,
    assistant_allowed: bool,
) -> int:
    raw_journal_cfg = provider._config.get("journal")
    journal_cfg = raw_journal_cfg if isinstance(raw_journal_cfg, dict) else {}
    journal_enabled = journal_cfg.get("enabled", True)
    if isinstance(journal_enabled, str):
        journal_enabled = journal_enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not journal_enabled or not (user_allowed or assistant_allowed):
        return 0
    journal_entries: list[dict[str, Any]] = []
    if user_allowed and clean_user:
        journal_entries.append(
            {
                "role": "user",
                "content": clean_user,
                "turn_number": provider._current_turn,
            }
        )
    if assistant_allowed and clean_assistant:
        journal_entries.append(
            {
                "role": "assistant",
                "content": clean_assistant,
                "turn_number": provider._current_turn,
            }
        )
    return append_scoped_journal_entries(provider, journal_entries)
