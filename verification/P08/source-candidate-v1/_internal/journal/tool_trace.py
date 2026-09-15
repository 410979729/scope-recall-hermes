"""Pure tool-trace journal summarizer. No SQL."""

from __future__ import annotations

import json
from typing import Any

from ...capture_filters import (
    contains_secret_like_text,
    redact_secret_like_text,
    sanitize_capture_text,
    sanitize_report_text,
    should_capture_text,
)
from ...gating import clean_text, compact_text, config_bool

DEFAULT_TOOL_TRACE_SKIP_NAMES = {"todo", "skill_view", "skills_list"}
DEFAULT_TOOL_TRACE_SKIP_NAME_FRAGMENTS = {"session_messages"}


def tool_journal_content(
    message: dict[str, Any],
    *,
    journal_config: dict[str, Any],
    runtime_config: dict[str, Any],
) -> str:
    """Decide how much tool result content should be captured into the journal."""

    tool_name = str(
        message.get("name") or message.get("tool_name") or message.get("recipient") or ""
    ).strip()
    skip_names = set(DEFAULT_TOOL_TRACE_SKIP_NAMES)
    raw_skip_names = journal_config.get("tool_trace_skip_names")
    if isinstance(raw_skip_names, str):
        skip_names.add(raw_skip_names.strip().lower())
    elif isinstance(raw_skip_names, (list, tuple, set)):
        skip_names.update(str(item).strip().lower() for item in raw_skip_names if str(item).strip())
    normalized_tool_name = tool_name.lower()
    if normalized_tool_name in skip_names or any(
        fragment in normalized_tool_name for fragment in DEFAULT_TOOL_TRACE_SKIP_NAME_FRAGMENTS
    ):
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
    filter_config = dict(runtime_config)
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
