"""Current-turn recall plus experience packet merge for prefetch.

RecallService owns this read-side path. Provider keeps a one-line
delegate and module-level monkeypatch anchors. Render, experience
preflight, config_bool, and logger resolve from
``type(provider).__module__`` at call time so provider-module patches
still apply. Isolation, fail-soft rollback, experience config gates,
the existing lock around preflight, and packet merge stay here.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from ...gating import config_bool as default_config_bool
from ...prompting import render_current_turn_recall as default_render
from ..experience.runtime import run_experience_preflight as default_preflight

_MISSING = object()
_LOGGER = logging.getLogger("scope_recall.provider")


def _provider_modules(provider: Any) -> list[Any]:
    names: list[str] = []
    module_name = type(provider).__module__ or ""
    if module_name:
        names.append(module_name)
    if module_name != "scope_recall.provider":
        names.append("scope_recall.provider")
    modules: list[Any] = []
    seen: set[int] = set()
    for name in names:
        module = sys.modules.get(name)
        if module is None or id(module) in seen:
            continue
        seen.add(id(module))
        modules.append(module)
    return modules


def _module_attr(provider: Any, name: str, default: Any) -> Any:
    """Prefer the provider-class module hook, then scope_recall.provider."""

    for module in _provider_modules(provider):
        value = getattr(module, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


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
    render = _module_attr(provider, "render_current_turn_recall", default_render)
    rollback = getattr(provider, "_rollback_conn_after_error", None)
    logger = _module_attr(provider, "logger", _LOGGER)
    try:
        recall_block = render(provider, query)
    except Exception:
        if callable(rollback):
            rollback("current-turn recall prefetch")
        logger.exception("Scope Recall current-turn recall prefetch failed")
        recall_block = ""
    raw_experience_config = provider._config.get("experience")
    experience_config = raw_experience_config if isinstance(raw_experience_config, dict) else {}
    config_bool = _module_attr(provider, "config_bool", default_config_bool)
    if not config_bool(experience_config, "enabled", True):
        return recall_block
    if not config_bool(experience_config, "prefetch_enabled", False):
        return recall_block
    preflight = _module_attr(provider, "run_experience_preflight", default_preflight)
    try:
        with provider._lock:
            packet = preflight(provider, query=query).get("packet", "")
    except Exception:
        if callable(rollback):
            rollback("experience preflight")
        logger.exception("Scope Recall experience preflight failed")
        packet = ""
    if not packet:
        return recall_block
    return f"{recall_block}\n\n{packet}" if recall_block else str(packet)
