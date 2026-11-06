"""Hermes plugin entry for the bounded core adapter slice."""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> Any:
    """Assemble the public provider with its operator setup workflow and capture hooks."""
    from ...maintenance.setup import configure_hermes
    from .hooks import register_capture_hooks, unsupported_host_fields
    from .provider import ScopeRecallHermesAdapter

    adapter = ScopeRecallHermesAdapter(setup=configure_hermes)
    adapter._diagnostics.unsupported_fields = unsupported_host_fields()
    ctx.register_memory_provider(adapter)
    register_capture_hooks(ctx, adapter)
    return adapter
