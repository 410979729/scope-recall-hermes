"""Compatibility shim. Implementation: _internal.runtime.kernel."""
from __future__ import annotations

from ._internal.runtime.kernel import (
    COMMAND_KERNEL,
    CommandKernel,
    KERNEL,
    QueryKernel,
    RuntimeKernel,
    bind_memory_command_port,
)

__all__ = [
    "COMMAND_KERNEL",
    "CommandKernel",
    "KERNEL",
    "QueryKernel",
    "RuntimeKernel",
    "bind_memory_command_port",
]
