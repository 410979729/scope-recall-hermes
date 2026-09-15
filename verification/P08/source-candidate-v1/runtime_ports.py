"""Compatibility shim. Implementation: _internal.runtime.ports."""
from __future__ import annotations

from ._internal.runtime.ports import (
    FactToolPort,
    MemoryCommandPort,
    MemoryQueryPort,
    RuntimeAdapterPort,
    RuntimeStatusPort,
    ScopeContextPort,
    ToolRouterPort,
    ToolRuntimePort,
    TruthStorePort,
)

__all__ = [
    "FactToolPort",
    "MemoryCommandPort",
    "MemoryQueryPort",
    "RuntimeAdapterPort",
    "RuntimeStatusPort",
    "ScopeContextPort",
    "ToolRouterPort",
    "ToolRuntimePort",
    "TruthStorePort",
]
