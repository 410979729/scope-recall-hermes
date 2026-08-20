"""Scope helpers shared by memory commands and read-only queries."""

from __future__ import annotations

from typing import Any

from ...graph import normalize_entity
from ...models import resolve_store_scope_mode


def scope_params(provider: Any, *, writable: bool = False) -> list[str]:
    attr = "_writable_scope_ids" if writable else "_accessible_scope_ids"
    scopes = getattr(provider, attr, []) or []
    return [str(scope_id) for scope_id in scopes if str(scope_id)]


def scope_placeholders(provider: Any, *, writable: bool = False) -> str:
    params = scope_params(provider, writable=writable)
    return ",".join("?" for _ in params) or "NULL"


def accessible_scope_params(provider: Any) -> list[str]:
    return scope_params(provider, writable=False)


def writable_scope_params(provider: Any) -> list[str]:
    return scope_params(provider, writable=True)


def normalized_scope_mode(
    provider: Any, target: str, source: str = "", scope_mode: str | None = None
) -> str:
    del provider
    return resolve_store_scope_mode(target, source, scope_mode)


def payload_entities(metadata: dict[str, Any]) -> list[str]:
    raw_entities = metadata.get("entities")
    if not isinstance(raw_entities, list):
        return []
    seen: set[str] = set()
    output: list[str] = []
    for raw_entity in raw_entities:
        entity = normalize_entity(raw_entity)
        if not entity or entity in seen:
            continue
        seen.add(entity)
        output.append(entity)
    return output


# Compatibility names used throughout memory_ops.
_scope_params = scope_params
_scope_placeholders = scope_placeholders
_accessible_scope_params = accessible_scope_params
_writable_scope_params = writable_scope_params
_normalized_scope_mode = normalized_scope_mode
_payload_entities = payload_entities
