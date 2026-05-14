from __future__ import annotations

from .models import RuntimeScope


def build_scope_id(scope: RuntimeScope) -> str:
    parts = [
        scope.platform or "cli",
        scope.agent_workspace or "default",
        scope.agent_identity or "default",
        f"user:{scope.user_id or 'local'}",
    ]
    if scope.gateway_session_key:
        parts.append(f"session:{scope.gateway_session_key}")
    else:
        if scope.chat_id:
            parts.append(f"chat:{scope.chat_id}")
        if scope.thread_id:
            parts.append(f"thread:{scope.thread_id}")
    return "|".join(parts)
