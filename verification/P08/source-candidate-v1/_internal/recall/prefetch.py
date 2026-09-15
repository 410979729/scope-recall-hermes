"""Current-turn recall plus experience packet merge for prefetch.

RecallService owns this read-side path. Provider keeps a one-line
delegate and module-level monkeypatch anchors. Render, experience
preflight, config_bool, and logger resolve through ProviderModuleHooks
so provider-module patches still apply. Isolation, fail-soft rollback,
experience config gates, the existing lock around preflight, and packet
merge stay here.
"""

from __future__ import annotations

import logging
from typing import Any

from ...gating import config_bool as default_config_bool
from ...prompting import render_current_turn_recall as default_render
from ..compatibility.provider_hooks import ProviderModuleHooks
from ..experience.runtime import run_experience_preflight as default_preflight
from .deadline import (
    EXPERIENCE_MIN_REMAINING_SECONDS,
    RequestDeadline,
    acquire_until,
    is_request_budget_failure,
    resolve_foreground_budget_seconds,
    using_request_deadline,
)
from .sources import SourceUnavailable

_LOGGER = logging.getLogger("scope_recall.provider")


def _rollback_if_uncontended(provider: Any, rollback: Any, context: str) -> None:
    """Rollback only when the writer lifecycle lock is free.

    Request-budget exhaustion must not contend for the shared connection.
    If a writer already holds the lifecycle lock, skip rather than wait.
    """

    if not callable(rollback):
        return
    lifecycle = getattr(provider, "_writer_lifecycle_lock", None)
    acquire = getattr(lifecycle, "acquire", None)
    release = getattr(lifecycle, "release", None)
    if callable(acquire) and callable(release):
        if not bool(acquire(blocking=False)):
            return
        try:
            rollback(context)
        finally:
            release()
        return
    rollback(context)


def prefetch_prompt(provider: Any, query: str, *, session_id: str = "") -> str:
    """Assemble the current-turn recall block and optional experience packet.

    ``session_id`` is accepted for Hermes hook compatibility and unused.
    Experience preflight stays zero-write because the experience owner is
    called with its existing ``record_run=False`` contract.
    """

    del session_id
    isolated = getattr(provider, "_memory_isolated_for_scope", None)
    if callable(isolated) and isolated():
        return ""
    hooks = ProviderModuleHooks(provider)
    render = hooks.resolve("render_current_turn_recall", default_render)
    rollback = getattr(provider, "_rollback_conn_after_error", None)
    logger = hooks.resolve("logger", _LOGGER)
    deadline = RequestDeadline.from_budget(
        resolve_foreground_budget_seconds(getattr(provider, "_config", {}))
    )
    with using_request_deadline(deadline):
        try:
            recall_block = render(provider, query)
        except SourceUnavailable:
            recall_block = ""
        except Exception as exc:
            if is_request_budget_failure(exc):
                logger.warning(
                    "Scope Recall current-turn recall prefetch hit the request deadline"
                )
                recall_block = ""
            else:
                _rollback_if_uncontended(
                    provider, rollback, "current-turn recall prefetch"
                )
                logger.exception("Scope Recall current-turn recall prefetch failed")
                recall_block = ""
        raw_config = getattr(provider, "_config", None) or {}
        raw_experience_config = (
            raw_config.get("experience") if isinstance(raw_config, dict) else {}
        )
        experience_config = (
            raw_experience_config if isinstance(raw_experience_config, dict) else {}
        )
        config_bool = hooks.resolve("config_bool", default_config_bool)
        if not config_bool(experience_config, "enabled", True):
            return recall_block
        if not config_bool(experience_config, "prefetch_enabled", False):
            return recall_block
        if deadline.remaining() <= EXPERIENCE_MIN_REMAINING_SECONDS:
            return recall_block
        preflight = hooks.resolve("run_experience_preflight", default_preflight)
        lock = getattr(provider, "_lock", None)
        packet = ""
        if lock is None:
            try:
                packet = preflight(provider, query=query).get("packet", "")
            except Exception:
                logger.exception("Scope Recall experience preflight failed")
                packet = ""
        elif acquire_until(lock, deadline):
            try:
                packet = preflight(provider, query=query).get("packet", "")
            except Exception:
                logger.exception("Scope Recall experience preflight failed")
                packet = ""
            finally:
                lock.release()
        if not packet:
            return recall_block
        return f"{recall_block}\n\n{packet}" if recall_block else str(packet)
