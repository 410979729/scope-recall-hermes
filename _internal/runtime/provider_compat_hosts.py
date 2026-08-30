"""Honest Provider-shaped host contracts retained only for 2.0.x adapters.

These protocols live beside the provider-bound runtime adapters so Core ports
and ``RuntimeDependencies`` do not acquire SQLite, lock, or generic host
capabilities.  They are registered compatibility debt scheduled for removal
after the 2.0 compatibility window.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .ports import RuntimeAdapterPort


@runtime_checkable
class LegacyProviderQueryHost(RuntimeAdapterPort, Protocol):
    """Raw query capabilities still consumed by legacy memory query helpers."""

    def query_connection(self) -> Any: ...

    def query_lock(self) -> Any: ...


@runtime_checkable
class LegacyProviderToolHost(LegacyProviderQueryHost, Protocol):
    """Required Provider methods behind the outer compatibility tool adapter.

    Optional V1 tool capabilities remain discovered with ``getattr`` by the
    adapter; the mandatory raw query/error/config hooks are explicit here.
    """

    def _require_conn(self) -> Any: ...

    def _clean_text(self, text: str) -> str: ...

    def _config_value(self, key: str, default: Any) -> Any: ...

    def _normalize_query(self, query: str, char_limit: int) -> str: ...

    def _rollback_conn_after_error(self, context: str) -> Any: ...

    def _recover_sqlite_connection_after_error(
        self, context: str
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class LegacyProviderRuntimeHost(LegacyProviderToolHost, Protocol):
    """Complete outer host accepted by the 2.0.x compatibility builder."""


__all__ = [
    "LegacyProviderQueryHost",
    "LegacyProviderRuntimeHost",
    "LegacyProviderToolHost",
]
