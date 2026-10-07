"""The MCP server every installed client runs, under the name installed configurations hold (see this package):
``python -m scope_recall.adapters.codex.mcp_entry``.  The code is ``adapters/clients/mcp_entry.py``."""

from ..clients.mcp_entry import main

if __name__ == "__main__":
    raise SystemExit(main())
