"""Capture infrastructure behind the typed application service."""

from __future__ import annotations

import sys
from typing import Any, cast

from ..application.capture_journal import (
    CaptureGateway,
    CaptureTurnPlan,
    CaptureTurnRequest,
)
from ...capture import capture_turn_fallbacks, capture_turn_llm_candidates, flush_writer
from ...capture_filters import sanitize_capture_text, should_capture_text
from ...capture_llm import extract_capture_candidates as default_extract_capture_candidates
from ...gating import clean_text
from ...governance import extract_candidates as default_extract_candidates


def _provider_hook(host: Any, name: str, default: Any) -> Any:
    module = sys.modules.get(type(host).__module__)
    candidate = getattr(module, name, None) if module is not None else None
    return candidate if callable(candidate) else default


class ProviderCaptureAdapter:
    """Confine current Provider-shaped capture state to infrastructure."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def prepare_turn(self, request: CaptureTurnRequest) -> CaptureTurnPlan:
        runtime_config = dict(getattr(self._host, "_config", {}) or {})
        clean_fn = getattr(self._host, "_clean_text", None)
        if not callable(clean_fn):
            clean_fn = clean_text
        config_value = getattr(self._host, "_config_value", None)
        min_capture_raw = (
            config_value("min_capture_length", 40)
            if callable(config_value)
            else runtime_config.get("min_capture_length", 40)
        )
        clean_user = sanitize_capture_text(str(clean_fn(request.user_content) or ""))
        clean_assistant = sanitize_capture_text(
            str(clean_fn(request.assistant_content) or "")
        )
        min_capture = int(cast(Any, min_capture_raw))
        user_filter = should_capture_text(clean_user, runtime_config)
        assistant_filter = should_capture_text(clean_assistant, runtime_config)
        journal_filter_config = dict(runtime_config)
        journal_filter_config["capture_hard_max_chars"] = -1
        journal_user_filter = should_capture_text(clean_user, journal_filter_config)
        journal_assistant_filter = should_capture_text(
            clean_assistant, journal_filter_config
        )
        return CaptureTurnPlan(
            clean_user=clean_user,
            clean_assistant=clean_assistant,
            user_allowed=user_filter.allowed,
            assistant_allowed=assistant_filter.allowed,
            journal_user_allowed=journal_user_filter.allowed,
            journal_assistant_allowed=journal_assistant_filter.allowed,
            min_capture=min_capture,
        )

    def capture_turn(self, plan: CaptureTurnPlan) -> None:
        extract_capture = _provider_hook(
            self._host,
            "extract_capture_candidates",
            default_extract_capture_candidates,
        )
        extract_fallbacks = _provider_hook(
            self._host, "extract_candidates", default_extract_candidates
        )
        llm_extracted, capture_policy_blocked = capture_turn_llm_candidates(
            self._host,
            clean_user=plan.clean_user,
            clean_assistant=plan.clean_assistant,
            user_allowed=plan.user_allowed,
            assistant_allowed=plan.assistant_allowed,
            extract_fn=extract_capture,
        )
        capture_turn_fallbacks(
            self._host,
            clean_user=plan.clean_user,
            clean_assistant=plan.clean_assistant,
            user_allowed=plan.user_allowed,
            assistant_allowed=plan.assistant_allowed,
            llm_extracted=llm_extracted,
            capture_policy_blocked=capture_policy_blocked,
            min_capture=plan.min_capture,
            extract_candidates_fn=extract_fallbacks,
        )

    def flush(self, timeout: float) -> bool:
        return bool(flush_writer(self._host, timeout=timeout))


def bind_capture_gateway(host: Any) -> CaptureGateway:
    return ProviderCaptureAdapter(host)
