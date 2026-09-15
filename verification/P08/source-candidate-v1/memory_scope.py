"""Compatibility shim. Implementation: _internal.memory.scope."""
from __future__ import annotations

from ._internal.memory.scope import (
    scope_params,
    scope_placeholders,
    accessible_scope_params,
    writable_scope_params,
    normalized_scope_mode,
    payload_entities,
    _scope_params,
    _scope_placeholders,
    _accessible_scope_params,
    _writable_scope_params,
    _normalized_scope_mode,
    _payload_entities,
)

__all__ = ['scope_params', 'scope_placeholders', 'accessible_scope_params', 'writable_scope_params', 'normalized_scope_mode', 'payload_entities', '_scope_params', '_scope_placeholders', '_accessible_scope_params', '_writable_scope_params', '_normalized_scope_mode', '_payload_entities']
