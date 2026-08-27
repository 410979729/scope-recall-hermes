"""Immutable runtime snapshots consumed by application use cases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    scope_id: str
    shared_scope_id: str
    shared_pool_scope_id: str
    accessible_scope_ids: tuple[str, ...]
    writable_scope_ids: tuple[str, ...]
    shared_pool_enabled: bool
    shared_pool_write_enabled: bool


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    writer_role: str
    writer_authorized: bool
    read_only: bool


@dataclass(frozen=True, slots=True)
class VectorSnapshot:
    state: str
    reason_code: str
    enabled: bool
    ready: bool
    usable_for_query: bool
    repair_required: bool


@dataclass(frozen=True, slots=True)
class RuntimeStateSnapshot:
    status: str
    scope: ScopeSnapshot
    authority: AuthoritySnapshot
    vector: VectorSnapshot
