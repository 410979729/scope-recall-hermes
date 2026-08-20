"""Compatibility shim. Implementation: _internal.contracts.tool_runtime_spec."""
from __future__ import annotations

from ._internal.contracts.tool_runtime_spec import (
    ToolSpec,
    TOOL_SPECS,
    tool_spec_by_name,
    visible_tool_specs,
)

__all__ = ['ToolSpec', 'TOOL_SPECS', 'tool_spec_by_name', 'visible_tool_specs']
