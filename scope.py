from __future__ import annotations

from .models import RuntimeScope


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def build_shared_scope_id(scope: RuntimeScope) -> str:
    """Return the durable user/profile scope shared across chats/windows.

    This deliberately excludes chat_id, thread_id, gateway_session_key, and
    session_id-like state. It is still bounded by platform, agent workspace,
    agent identity, and user id so memories do not leak between users or sibling
    agent identities.
    """

    return "|".join(
        [
            _scope_component("platform", scope.platform or "cli"),
            _scope_component("workspace", scope.agent_workspace or "default"),
            _scope_component("agent", scope.agent_identity or "default"),
            _scope_component("user", scope.user_id or "local"),
        ]
    )


def build_shared_pool_scope_id(scope: RuntimeScope, pool_id: str) -> str:
    """Return an optional cross-agent shared pool scope for one user/workspace.

    Unlike ``build_shared_scope_id()``, this deliberately excludes
    ``agent_identity``. It is opt-in via config and therefore acts as groundwork
    for a future shared memory pool without changing the default isolation model.
    """

    normalized_pool = str(pool_id or "default").strip() or "default"
    return "|".join(
        [
            _scope_component("pool", normalized_pool),
            _scope_component("platform", scope.platform or "cli"),
            _scope_component("workspace", scope.agent_workspace or "default"),
            _scope_component("user", scope.user_id or "local"),
        ]
    )



def build_scope_id(scope: RuntimeScope) -> str:
    parts = [build_shared_scope_id(scope)]
    if scope.gateway_session_key:
        parts.append(_scope_component("session", scope.gateway_session_key))
    else:
        if scope.chat_id:
            parts.append(_scope_component("chat", scope.chat_id))
        if scope.thread_id:
            parts.append(_scope_component("thread", scope.thread_id))
    return "|".join(parts)


def accessible_scope_ids(scope: RuntimeScope) -> list[str]:
    """Return local + shared scopes readable/writable by this runtime identity."""

    local = build_scope_id(scope)
    shared = build_shared_scope_id(scope)
    scopes = [local, shared]
    output: list[str] = []
    for scope_id in scopes:
        if scope_id and scope_id not in output:
            output.append(scope_id)
    return output
