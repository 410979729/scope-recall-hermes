"""CLI entry for the six-tool Codex MCP stdio server."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import CodexConfigError, load_codex_config
from .mcp_server import build_server


def _absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{field} must be absolute")
    return path.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall Codex MCP server")
    parser.add_argument("--config", required=True, help="absolute trusted installation config")
    parser.add_argument("--workspace", required=True, help="absolute mapped Codex project workspace")
    parser.add_argument("--runtime-config", default=None, help="absolute trusted local runtime worker config")
    args = parser.parse_args(argv)
    try:
        config = load_codex_config(_absolute(args.config, "config"))
        workspace = _absolute(args.workspace, "workspace")
        runtime_config = _absolute(args.runtime_config, "runtime-config") if args.runtime_config else None
        server = build_server(
            config,
            workspace=workspace,
            trusted_runtime_config_path=str(runtime_config) if runtime_config is not None else None,
        )
        server.server.run(transport="stdio")
    except (CodexConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
