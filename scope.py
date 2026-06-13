from __future__ import annotations

from typing import Any

from .models import RuntimeScope


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _identity_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("identity")
    return raw if isinstance(raw, dict) else {}


def _legacy_identities_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("identities")
    return raw if isinstance(raw, dict) else {}


def normalize_scope_identity(scope: RuntimeScope, config: dict[str, Any] | None = None) -> RuntimeScope:
    """Return a runtime scope with safe identity fallbacks applied.

    The raw platform remains platform-specific. Only empty CLI user ids are
    normalized because Hermes CLI sessions may not carry a user id at all, while
    durable identity mapping needs a stable account key such as ``cli:local``.
    """

    identity = _identity_config(config)
    platform = str(scope.platform or "cli")
    user_id = str(scope.user_id or "")
    if not user_id and platform == "cli" and _truthy(identity.get("cross_platform_shared_scope")):
        user_id = str(identity.get("cli_user_id_fallback") or "local")
    return RuntimeScope(
        platform=platform,
        user_id=user_id,
        chat_id=str(scope.chat_id or ""),
        thread_id=str(scope.thread_id or ""),
        gateway_session_key=str(scope.gateway_session_key or ""),
        agent_identity=str(scope.agent_identity or ""),
        agent_workspace=str(scope.agent_workspace or ""),
        agent_context=str(scope.agent_context or "primary"),
    )


def _account_key(platform: str, user_id: str) -> str:
    return f"{platform}:{user_id}"


def _identity_enabled(config: dict[str, Any] | None) -> bool:
    identity = _identity_config(config)
    return _truthy(identity.get("cross_platform_shared_scope"))


def _canonical_user_for_account(config: dict[str, Any] | None, platform: str, user_id: str) -> str:
    if not _identity_enabled(config):
        return ""
    identity = _identity_config(config)
    key = _account_key(platform, user_id)
    aliases = identity.get("user_aliases") or identity.get("aliases") or {}
    if isinstance(aliases, dict) and str(aliases.get(key) or "").strip():
        return str(aliases[key]).strip()

    # Also support the issue-proposed shape:
    # {"identities": {"joy": {"accounts": {"cli": "local"}}}}
    for canonical, payload in _legacy_identities_config(config).items():
        if not isinstance(payload, dict):
            continue
        accounts = payload.get("accounts")
        if isinstance(accounts, dict) and str(accounts.get(platform) or "") == user_id:
            return str(canonical)
        if isinstance(accounts, list) and key in {str(item) for item in accounts}:
            return str(canonical)
    return ""


def _accounts_for_canonical(config: dict[str, Any] | None, canonical_user: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    identity = _identity_config(config)
    aliases = identity.get("user_aliases") or identity.get("aliases") or {}
    if isinstance(aliases, dict):
        for key, canonical in aliases.items():
            if str(canonical) != canonical_user or ":" not in str(key):
                continue
            platform, user_id = str(key).split(":", 1)
            if platform and user_id:
                accounts.append((platform, user_id))

    for canonical, payload in _legacy_identities_config(config).items():
        if str(canonical) != canonical_user or not isinstance(payload, dict):
            continue
        raw_accounts = payload.get("accounts")
        if isinstance(raw_accounts, dict):
            for platform, user_id in raw_accounts.items():
                if str(platform) and str(user_id):
                    accounts.append((str(platform), str(user_id)))
        elif isinstance(raw_accounts, list):
            for item in raw_accounts:
                if ":" not in str(item):
                    continue
                platform, user_id = str(item).split(":", 1)
                if platform and user_id:
                    accounts.append((platform, user_id))

    output: list[tuple[str, str]] = []
    for item in accounts:
        if item not in output:
            output.append(item)
    return output


def canonical_user_id(scope: RuntimeScope, config: dict[str, Any] | None = None) -> str:
    normalized = normalize_scope_identity(scope, config)
    return _canonical_user_for_account(config, normalized.platform or "cli", normalized.user_id or "local")


def build_shared_scope_id(scope: RuntimeScope, config: dict[str, Any] | None = None) -> str:
    """Return the durable user/profile scope shared across chats/windows.

    By default this remains bounded by platform + user id to prevent memory
    leaks. When ``identity.cross_platform_shared_scope`` is explicitly enabled
    and the current account maps to a canonical user, durable shared scope uses
    that canonical identity and deliberately excludes platform. Local scratch
    scope remains platform/account bounded via ``build_scope_id``.
    """

    normalized = normalize_scope_identity(scope, config)
    canonical = canonical_user_id(normalized, config)
    if canonical:
        return "|".join(
            [
                _scope_component("workspace", normalized.agent_workspace or "default"),
                _scope_component("agent", normalized.agent_identity or "default"),
                _scope_component("canonical_user", canonical),
            ]
        )

    return "|".join(
        [
            _scope_component("platform", normalized.platform or "cli"),
            _scope_component("workspace", normalized.agent_workspace or "default"),
            _scope_component("agent", normalized.agent_identity or "default"),
            _scope_component("user", normalized.user_id or "local"),
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


def build_scope_id(scope: RuntimeScope, config: dict[str, Any] | None = None) -> str:
    normalized = normalize_scope_identity(scope, config)
    parts = [build_shared_scope_id(normalized, config)]
    if canonical_user_id(normalized, config):
        # Keep local scratch/platform context isolated even when durable memory is
        # shared through a canonical cross-platform user.
        parts.append(_scope_component("platform", normalized.platform or "cli"))
        parts.append(_scope_component("account", _account_key(normalized.platform or "cli", normalized.user_id or "local")))
    if normalized.gateway_session_key:
        parts.append(_scope_component("session", normalized.gateway_session_key))
    else:
        if normalized.chat_id:
            parts.append(_scope_component("chat", normalized.chat_id))
        if normalized.thread_id:
            parts.append(_scope_component("thread", normalized.thread_id))
    return "|".join(parts)


def writable_scope_ids(scope: RuntimeScope, config: dict[str, Any] | None = None) -> list[str]:
    """Return scopes this runtime may mutate.

    Cross-platform identity mapping adds legacy platform scopes to readable
    access for compatibility, but those legacy aliases are intentionally not
    writable: updates/deletes/merges should only affect the current local
    scratch scope and the current canonical durable shared scope.
    """

    normalized = normalize_scope_identity(scope, config)
    output: list[str] = []
    for scope_id in (build_scope_id(normalized, config), build_shared_scope_id(normalized, config)):
        if scope_id and scope_id not in output:
            output.append(scope_id)
    return output


def accessible_scope_ids(scope: RuntimeScope, config: dict[str, Any] | None = None) -> list[str]:
    """Return local + shared scopes readable/writable by this runtime identity.

    With explicit cross-platform identity mapping enabled, durable shared scope is
    canonical while local scratch remains platform/account scoped. Legacy shared
    scopes for mapped accounts are included read-only so existing durable rows
    remain discoverable before an operator runs any explicit migration.
    """

    normalized = normalize_scope_identity(scope, config)
    local = build_scope_id(normalized, config)
    shared = build_shared_scope_id(normalized, config)
    scopes = [local, shared]

    canonical = canonical_user_id(normalized, config)
    if canonical:
        # Preserve current-platform local scratch created before identity mapping.
        scopes.append(build_scope_id(normalized))
        # Preserve old durable rows for all explicitly mapped platform accounts.
        accounts = _accounts_for_canonical(config, canonical)
        if (normalized.platform, normalized.user_id or "local") not in accounts:
            accounts.append((normalized.platform, normalized.user_id or "local"))
        for platform, user_id in accounts:
            legacy_scope = RuntimeScope(
                platform=platform,
                user_id=user_id,
                agent_identity=normalized.agent_identity,
                agent_workspace=normalized.agent_workspace,
                agent_context=normalized.agent_context,
            )
            scopes.append(build_shared_scope_id(legacy_scope))

    output: list[str] = []
    for scope_id in scopes:
        if scope_id and scope_id not in output:
            output.append(scope_id)
    return output
