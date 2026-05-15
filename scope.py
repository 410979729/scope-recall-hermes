from __future__ import annotations

from .models import RuntimeScope


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def build_scope_id(scope: RuntimeScope) -> str:
    parts = [
        _scope_component("platform", scope.platform or "cli"),
        _scope_component("workspace", scope.agent_workspace or "default"),
        _scope_component("agent", scope.agent_identity or "default"),
        _scope_component("user", scope.user_id or "local"),
    ]
    if scope.gateway_session_key:
        parts.append(_scope_component("session", scope.gateway_session_key))
    else:
        if scope.chat_id:
            parts.append(_scope_component("chat", scope.chat_id))
        if scope.thread_id:
            parts.append(_scope_component("thread", scope.thread_id))
    return "|".join(parts)
