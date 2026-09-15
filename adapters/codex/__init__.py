"""Bounded Codex public Hook adapter over the host-independent core."""

from .config import CodexConfigError, install_codex_scope_recall, load_codex_config
from .handler import CodexHookHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Keep MCP optional at runtime while making the lazy public exports
    # visible to static analyzers.
    from .mcp_server import CodexMCPServer, build_server


def __getattr__(name):
    # Hook capture, installation and readonly diagnostics do not require MCP.
    # Import the optional transport only when its public entry is requested.
    if name in {"CodexMCPServer", "build_server"}:
        from . import mcp_server
        return getattr(mcp_server, name)
    raise AttributeError(name)

__all__ = [
    "CodexConfigError",
    "CodexHookHandler",
    "install_codex_scope_recall",
    "load_codex_config",
    "CodexMCPServer",
    "build_server",
]
