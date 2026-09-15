"""Exact Hermes audience declarations, read/write grants and retained registration.

Empty route fields match only empty route fields. Scope IDs are opaque: validate
without rewriting their bytes or interpreting legacy length-prefixed tokens.
"""
from __future__ import annotations

from typing import Any, Sequence


def normalize_owner_principals(values: object) -> tuple[dict[str, str], ...]:
    """Accept explicit owner aliases only; never infer ownership from routes."""
    if not isinstance(values, (list, tuple)) or not values:
        raise HermesIdentityError("owner_principals must be a nonempty explicit sequence")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"platform", "user_id"}:
            raise HermesIdentityError("owner_principals require only platform and user_id")
        for text in value.values():
            if type(text) is not str or not text or text != text.strip() or len(text) > 240 or text in {"*", "unknown"}:
                raise HermesIdentityError("invalid exact owner principal")
        key = (value["platform"], value["user_id"])
        if key in seen:
            raise HermesIdentityError("duplicate owner principal")
        seen.add(key)
        result.append(dict(value))
    return tuple(result)


class HermesIdentityError(RuntimeError):
    """Raised when documented host identity is missing or conflicts."""


EXACT_FIELDS = (
    "platform", "user_id", "chat_type", "chat_id", "thread_id",
    "gateway_session_key", "agent_workspace",
)
_EMPTY_ROUTE_FIELDS = frozenset({"chat_type", "chat_id", "thread_id", "gateway_session_key"})


def is_archive_scope(scope_id: str) -> bool:
    """Recognize namespaces that can never receive runtime grants."""
    return str(scope_id).startswith(("archive|", "archive:", "__audit:", "__quarantine:"))


def _scope_ids(values: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise HermesIdentityError(f"{field} must be an explicit scope sequence")
    result: list[str] = []
    for value in values:
        if type(value) is not str or not value.strip() or value == "*" or len(value) > 240:
            raise HermesIdentityError(f"{field} contains an invalid scope ID")
        if value in result or is_archive_scope(value):
            raise HermesIdentityError(f"{field} contains duplicate or archive-only scope IDs")
        result.append(value)
    return tuple(result)


def normalize_retained_scope_ids(values: object) -> frozenset[str]:
    """Validate non-runtime original scopes without changing their identifiers."""
    return frozenset(_scope_ids(values, field="retained_scope_ids"))


def _audience_entry(
    *, platform: str, user_id: str, chat_type: str, chat_id: str,
    thread_id: str, gateway_session_key: str, agent_workspace: str,
    allowed_scope_ids: Sequence[str], writable_scope_ids: Sequence[str],
    capture_scope_id: str, kind: str,
) -> dict[str, Any]:
    """Build an exact declaration; capture must belong to its writable subset."""
    fields = dict(platform=platform, user_id=user_id, chat_type=chat_type,
                  chat_id=chat_id, thread_id=thread_id,
                  gateway_session_key=gateway_session_key, agent_workspace=agent_workspace)
    for key, value in fields.items():
        if (type(value) is not str or len(value) > 240
                or (key not in _EMPTY_ROUTE_FIELDS and not value.strip())
                or value.strip().lower() == "unknown"):
            raise HermesIdentityError(f"audiences.{key} must be an explicit exact string")
    fields = {key: value.strip() for key, value in fields.items()}
    if type(kind) is not str or not kind.strip() or len(kind) > 240:
        raise HermesIdentityError("audiences.kind is required")
    if kind == "owner_private" and fields["chat_type"] not in {"cli", "private", "direct", "dm"}:
        raise HermesIdentityError("owner_private audience must be an explicit private or CLI chat")
    allowed = _scope_ids(allowed_scope_ids, field="allowed_scope_ids")
    writable = _scope_ids(writable_scope_ids, field="writable_scope_ids")
    if not allowed or not set(writable) <= set(allowed):
        raise HermesIdentityError("writable_scope_ids must be a subset of nonempty allowed_scope_ids")
    if type(capture_scope_id) is not str or (capture_scope_id not in writable if writable else capture_scope_id != ""):
        raise HermesIdentityError("capture_scope_id must be writable, or empty for a read-only audience")
    return {**fields, "allowed_scope_ids": list(allowed), "writable_scope_ids": list(writable),
            "capture_scope_id": capture_scope_id, "kind": kind.strip()}


def _normalize_audience_entry(value: object) -> dict[str, Any]:
    """Reject pre-upgrade or incomplete rows rather than inferring new grants."""
    required = {*EXACT_FIELDS, "allowed_scope_ids", "writable_scope_ids", "capture_scope_id"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise HermesIdentityError("audience mapping incomplete; explicit v3 upgrade required")
    if value.keys() - (required | {"kind"}):
        raise HermesIdentityError("unsupported audience fields")
    if not isinstance(value["allowed_scope_ids"], list) or not isinstance(value["writable_scope_ids"], list):
        raise HermesIdentityError("audience read/write scope sets must be explicit lists")
    return _audience_entry(**{key: value[key] for key in required}, kind=value.get("kind", "conversation"))
