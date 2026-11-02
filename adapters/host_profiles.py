"""Host declarations and their common interpretation for hooks, records and identity.

Payloads name sessions, never principals. Only installation grants approve the person behind a surface.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HostProfile:
    """The host's identity fields, lifecycle events and visible-text envelopes."""

    session_field: str = "session_id"
    turn_field: str = "turn_id"
    events: frozenset[str] = frozenset()
    empty_answer: str = "{}"
    record_format: str | None = None
    record_directory: tuple[str, ...] = ("cwd",)
    record_directory_events: frozenset[str] | None = None
    record_uses_hook_directory: bool = False
    thread_meta: str | None = None
    local_surfaces: frozenset[str] = frozenset()
    hook_observers: tuple[tuple[str, str], ...] = ()
    prompt_prefixes: tuple[str, ...] = ()
    reminder_open: str | None = None
    reminder_close: str = "</system-reminder>"
    reminder_trailing: str = r"(?:[ \t]*\r?\n)*"
    query_block: str | None = None
    suggestion_frame: str | None = None
    suggestion_topic: str | None = None
    suggestion_headings: str | None = None
    subagent_prefix: str | None = None
    subagent_path: str | None = None
    generated_reply_shapes: tuple[tuple[str, type], ...] = ()


HOSTS = {
    "hermes": HostProfile(
        events=frozenset(
            {
                "on_turn_start",
                "pre_llm_call",
                "post_tool_call",
                "api_request_error",
                "post_llm_call",
                "sync_turn",
                "prefetch",
                "on_session_end",
                "on_session_switch",
                "on_pre_compress",
            }
        ),
        hook_observers=(
            ("pre_llm_call", "observe_pre_llm"),
            ("api_request_error", "observe_api_request_error"),
            ("post_tool_call", "observe_post_tool_call"),
            ("post_llm_call", "observe_post_llm_call"),
        ),
        local_surfaces=frozenset({"desktop", "tui"}),
    ),
    "codex": HostProfile(
        events=frozenset({"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "Interrupt", "SessionEnd"}),
        generated_reply_shapes=(("suggestions", list), ("suggestion", str)),
        record_format="codex",
        record_directory=("payload", "cwd"),
        record_directory_events=frozenset({"session_meta", "turn_context"}),
        thread_meta="threadId",
        suggestion_frame=(
            r"This is an app-generated request for a suggested next user message\.\s+"
            r"The user did not write this message\."
        ),
        suggestion_topic=r"\bhyperpersonali[sz]ed\s+suggestions?\b",
        suggestion_headings=r"^#{1,2}[ \t]*(?:overview|rules|examples|bad examples|response format)[ \t]*$",
    ),
    "claude-code": HostProfile(
        turn_field="prompt_id",
        events=frozenset({"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}),
        record_format="claude-code",
        prompt_prefixes=("<task-notification>",),
        reminder_open=r"^[ \t]*<system-reminder\b[^>\n]*>",
    ),
    "workbuddy": HostProfile(
        turn_field="generation_id",
        events=frozenset({"UserPromptSubmit", "Stop", "SessionEnd"}),
        empty_answer="",
        record_format="workbuddy",
        prompt_prefixes=("<task-notification>", "Stop hook feedback:"),
        reminder_open=r"<system-reminder\b[^>]*>",
        reminder_trailing=r"\s*",
        query_block=r"<user_query>(.*?)</user_query>",
        subagent_prefix="agent-",
        subagent_path=r"[\\/]subagents[\\/]",
    ),
    "dsh": HostProfile(
        events=frozenset({"UserPromptSubmit", "Stop"}), record_format="dsh", record_uses_hook_directory=True
    ),
}


def person_text(host: str, text: str, *, record: bool = False) -> str:
    """Remove complete host envelopes; a record may require an explicit user block."""
    profile = HOSTS[host]
    if profile.reminder_open is not None:
        kept = []
        position = 0
        pattern = re.compile(profile.reminder_open, re.MULTILINE)
        while (opening := pattern.search(text, position)) is not None:
            closing = text.find(profile.reminder_close, opening.end())
            if closing < 0:
                break
            kept.append(text[position : opening.start()])
            position = closing + len(profile.reminder_close)
            blank = re.match(profile.reminder_trailing, text[position:])
            position += blank.end() if blank else 0
        if kept:
            text = "".join([*kept, text[position:]]).strip()
    if profile.query_block is not None:
        queries = re.findall(profile.query_block, text, re.DOTALL)
        if record:
            return "\n".join(query.strip() for query in queries if query.strip())
        return (queries[-1] if queries else text).strip()
    return text


def host_prompt(host: str, prompt: str) -> bool:
    """Recognize host-authored requests by their envelope, never an isolated topic word."""
    profile = HOSTS[host]
    text = prompt.lstrip()
    if text.startswith(profile.prompt_prefixes):
        return True
    if profile.suggestion_frame and re.match(profile.suggestion_frame, text, re.IGNORECASE):
        return True
    return bool(
        profile.suggestion_topic
        and profile.suggestion_headings
        and text.startswith("#")
        and len(text) >= 8000
        and re.search(profile.suggestion_topic, text[:600], re.IGNORECASE)
        and len(
            {
                heading.strip("# \t").lower()
                for heading in re.findall(profile.suggestion_headings, text, re.IGNORECASE | re.MULTILINE)
            }
        )
        >= 3
    )


def agent_run(host: str, payload: dict[str, Any]) -> bool:
    """Recognize a subagent where the host distinguishes it from the visible conversation."""
    profile = HOSTS[host]
    agent, record = payload.get("agent_id"), payload.get("transcript_path")
    return bool(
        (profile.subagent_prefix and type(agent) is str and agent.strip().startswith(profile.subagent_prefix))
        or (profile.subagent_path and type(record) is str and re.search(profile.subagent_path, record))
    )


def claude_code_person_text(text: str) -> str:
    """The visible person's words in a Claude Code envelope."""
    return person_text("claude-code", text)


def workbuddy_person_text(text: str) -> str:
    """The current visible prompt in a WorkBuddy envelope."""
    return person_text("workbuddy", text)


def workbuddy_record_words(text: str) -> str:
    """All visible user blocks in a WorkBuddy record."""
    return person_text("workbuddy", text, record=True)


def is_task_notification(text: str) -> bool:
    """Whether the complete Claude Code prompt is a host notice."""
    return host_prompt("claude-code", text)


def is_workbuddy_notice(text: str) -> bool:
    """Whether the complete WorkBuddy prompt is a host notice."""
    return host_prompt("workbuddy", text)


def is_codex_suggestions_prompt(text: str) -> bool:
    """Whether the complete Codex prompt is an application suggestion request."""
    return host_prompt("codex", text)


def is_workbuddy_agent_run(payload: dict[str, Any]) -> bool:
    """Whether a WorkBuddy event belongs to a subagent."""
    return agent_run("workbuddy", payload)


def is_codex_suggestions_reply(message: str) -> bool:
    """Recognize a JSON reply containing only suggested next activity."""
    text = message.strip()
    if not text.startswith("{") or len(text) > 65536:
        return False
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict) or len(value) != 1:
        return False
    return any(isinstance(value.get(key), shape) for key, shape in HOSTS["codex"].generated_reply_shapes)
