"""CLI entry for the six-tool Codex MCP stdio server.

``--config`` with ``--workspace`` serves a local Codex installation's mapped
project; ``--home`` with ``--host`` serves a client attached to a shared store,
Codex or Claude Code, whose audience does not depend on a workspace.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ...runtime.resume_entry import host_process_credential_environment
from .config import CodexConfigError, CodexInstallationConfig, SharedClientConfig, load_codex_config, load_shared_client
from .mcp_server import build_server


def _absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{field} must be absolute")
    return path.resolve()


def apply_credential_environment(
    config: CodexInstallationConfig | SharedClientConfig,
    env_file: Path,
    runtime_config: Path | None,
    *,
    stderr=None,
) -> bool:
    """Load the configured credential names from ``env_file`` into this process.

    Codex starts the server with its own environment, so without this the embedding
    route has no key and every recall degrades to lexical-only.  Failure is reported
    on stderr and the server still starts: a memory tool without its semantic channel
    is worth more than no memory tool.
    """
    default = (config.runtime_config_path if isinstance(config, SharedClientConfig)
               else config.data_directory / "runtime-config.json")
    runtime_path = runtime_config or default
    try:
        loaded = host_process_credential_environment(runtime_path, env_file)
    except (OSError, ValueError) as exc:
        (stderr or sys.stderr).write(
            f"scope-recall mcp: credential environment unavailable ({exc}); embeddings unavailable\n"
        )
        return False
    os.environ.update(loaded)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall Codex MCP server")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--config", help="absolute trusted installation config")
    where.add_argument("--home", help="absolute home of a client attached to a shared store")
    parser.add_argument("--host", choices=("codex", "claude-code"), default="codex",
                        help="the client that starts this server, for --home")
    parser.add_argument("--workspace", default=None, help="absolute mapped Codex project workspace, for --config")
    parser.add_argument("--runtime-config", default=None, help="absolute trusted local runtime worker config")
    parser.add_argument(
        "--env-file",
        default=None,
        help="absolute file holding the credential names the runtime config declares; "
        "Codex does not pass them in the process environment",
    )
    args = parser.parse_args(argv)
    try:
        if args.config is not None:
            if not args.workspace:
                raise ValueError("--workspace is required with --config")
            config = load_codex_config(_absolute(args.config, "config"))
            workspace = _absolute(args.workspace, "workspace")
        else:
            config = load_shared_client(_absolute(args.home, "home"), args.host)
            workspace = None
        runtime_config = _absolute(args.runtime_config, "runtime-config") if args.runtime_config else None
        if args.env_file:
            apply_credential_environment(config, _absolute(args.env_file, "env-file"), runtime_config)
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
