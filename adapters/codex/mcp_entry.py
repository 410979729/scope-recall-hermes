"""CLI entry for the six-tool Codex MCP stdio server."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ...runtime.resume_entry import host_process_credential_environment
from .config import CodexConfigError, CodexInstallationConfig, load_codex_config
from .mcp_server import build_server


def _absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{field} must be absolute")
    return path.resolve()


def apply_credential_environment(
    config: CodexInstallationConfig,
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
    runtime_path = runtime_config or (config.data_directory / "runtime-config.json")
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
    parser.add_argument("--config", required=True, help="absolute trusted installation config")
    parser.add_argument("--workspace", required=True, help="absolute mapped Codex project workspace")
    parser.add_argument("--runtime-config", default=None, help="absolute trusted local runtime worker config")
    parser.add_argument(
        "--env-file",
        default=None,
        help="absolute file holding the credential names the runtime config declares; "
        "Codex does not pass them in the process environment",
    )
    args = parser.parse_args(argv)
    try:
        config = load_codex_config(_absolute(args.config, "config"))
        workspace = _absolute(args.workspace, "workspace")
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
