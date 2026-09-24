"""Codex hook CLI entry: JSON stdin, one JSON stdout, diagnostics on stderr.

``--config`` names a local Codex installation; ``--home`` with ``--host`` names
a client attached to a shared store, Codex or Claude Code.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from ...runtime.resume_entry import host_process_credential_environment
from .config import load_codex_config, load_shared_client
from .handler import CodexHookHandler, emit_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall Codex hook adapter")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--config", type=Path, help="Absolute path to codex-installation.json")
    where.add_argument("--home", type=Path, help="Absolute home of a client attached to a shared store")
    parser.add_argument("--host", choices=("codex", "claude-code"), default="codex",
                        help="the client whose hooks call this, for --home")
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=None,
        help="Absolute path to trusted local runtime worker config",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Absolute file holding the credential names the runtime config declares; "
        "Codex does not pass them in the hook's environment",
    )
    args = parser.parse_args(argv)
    # Start the trusted wall-clock budget before configuration/runtime loading;
    # model or hook payload fields never participate in this timestamp.
    hook_started_at = time.monotonic()
    location = (args.config if args.config is not None else args.home).expanduser()
    if not location.is_absolute():
        sys.stderr.write("CODEX_HOOK:config_path_not_absolute\n")
        emit_result({})
        return 0
    runtime_config = args.runtime_config.expanduser() if args.runtime_config is not None else None
    if runtime_config is not None and not runtime_config.is_absolute():
        sys.stderr.write("CODEX_HOOK:runtime_config_not_absolute\n")
        emit_result({})
        return 0
    if args.env_file is not None:
        # A hook must answer inside its 2 s budget whatever happens; a missing key
        # only costs the semantic channel, so the failure is logged and not fatal.
        env_file = args.env_file.expanduser()
        if not env_file.is_absolute():
            sys.stderr.write("CODEX_HOOK:env_file_not_absolute\n")
        else:
            try:
                if args.config is not None:
                    declared = runtime_config or (load_codex_config(str(location)).data_directory / "runtime-config.json")
                else:
                    declared = runtime_config or load_shared_client(location, args.host).runtime_config_path
                os.environ.update(host_process_credential_environment(declared, env_file))
            except Exception:
                sys.stderr.write("CODEX_HOOK:credential_environment_unavailable\n")
    try:
        if args.config is not None:
            handler = CodexHookHandler.from_config_path(
                str(location),
                trusted_runtime_config_path=str(runtime_config) if runtime_config is not None else None,
                hook_started_at=hook_started_at,
            )
        else:
            handler = CodexHookHandler.from_home(
                str(location),
                args.host,
                trusted_runtime_config_path=str(runtime_config) if runtime_config is not None else None,
                hook_started_at=hook_started_at,
            )
    except Exception:
        sys.stderr.write("CODEX_HOOK:config_unavailable\n")
        emit_result({})
        return 0
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        sys.stderr.write("CODEX_HOOK:input_too_large\n")
        emit_result({})
        return 0
    result = handler.handle_bytes(raw)
    emit_result(result, diagnostics=handler.diagnostics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
