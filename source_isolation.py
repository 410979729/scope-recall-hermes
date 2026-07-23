"""Public source-isolation policy for chats excluded from memory surfaces.

The policy contains no deployment identifiers. Operators store private chat IDs
only in Hermes-home runtime configuration.
"""

from __future__ import annotations

from typing import Any

from .models import RuntimeScope


def memory_isolated_chat_ids(config: dict[str, Any] | None) -> frozenset[str]:
    """Return normalized non-empty chat identifiers from runtime config."""

    raw = (config or {}).get("memory_isolated_chat_ids")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(value).strip() for value in raw if str(value).strip())


def chat_is_memory_isolated(
    chat_id: object,
    config: dict[str, Any] | None,
) -> bool:
    """Return whether one chat ID is denied by the configured policy."""

    normalized = str(chat_id or "").strip()
    return bool(normalized and normalized in memory_isolated_chat_ids(config))


def scope_is_memory_isolated(
    scope: RuntimeScope | None,
    config: dict[str, Any] | None,
) -> bool:
    """Return whether a runtime scope is denied by chat source policy."""

    if scope is None:
        return False
    return chat_is_memory_isolated(scope.chat_id, config)


__all__ = [
    "chat_is_memory_isolated",
    "memory_isolated_chat_ids",
    "scope_is_memory_isolated",
]
