"""Compatibility shim. Implementation: _internal.contracts.config_runtime_spec."""
from __future__ import annotations

from ._internal.contracts.config_runtime_spec import (
    PLUGIN_ROOT,
    walk_leaves,
    config_leaf_kinds,
)

__all__ = ['PLUGIN_ROOT', 'walk_leaves', 'config_leaf_kinds']
