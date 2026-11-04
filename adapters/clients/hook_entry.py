"""Codex hook CLI entry: JSON stdin, one JSON stdout, diagnostics on stderr.

``--config`` names a local Codex installation; ``--home`` with ``--host`` names
a client attached to a shared store, Codex or Claude Code.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import closing
from pathlib import Path

from ...runtime.worker_entry import host_process_credential_environment
from .boundary import EMPTY_ANSWER
from .config import load_codex_config, load_shared_client
from .handler import CodexHookHandler
from .hook_answer import emit_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall Codex hook adapter")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--config", type=Path, help="Absolute path to codex-installation.json")
    where.add_argument("--home", type=Path, help="Absolute home of a client attached to a shared store")
    parser.add_argument(
        "--host",
        choices=("codex", "claude-code", "workbuddy", "dsh"),
        default="codex",
        help="the client whose hooks call this, for --home",
    )
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
    empty = EMPTY_ANSWER[args.host]
    raw = sys.stdin.buffer.read(65537)
    location = (args.config if args.config is not None else args.home).expanduser()
    if not location.is_absolute():
        sys.stderr.write("CODEX_HOOK:config_path_not_absolute\n")
        emit_result({}, empty=empty)
        return 0
    runtime_config = args.runtime_config.expanduser() if args.runtime_config is not None else None
    if runtime_config is not None and not runtime_config.is_absolute():
        sys.stderr.write("CODEX_HOOK:runtime_config_not_absolute\n")
        emit_result({}, empty=empty)
        return 0
    if args.env_file is not None:
        # A hook must answer inside its budget whatever happens; a missing key
        # only costs the semantic channel, so the failure is logged and not fatal.
        env_file = args.env_file.expanduser()
        if not env_file.is_absolute():
            sys.stderr.write("CODEX_HOOK:env_file_not_absolute\n")
        else:
            try:
                if args.config is not None:
                    declared = runtime_config or (
                        load_codex_config(str(location)).data_directory / "runtime-config.json"
                    )
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
        emit_result({}, empty=empty)
        return 0
    # Closing abandons late attachment and preserves any watchdog detached by this hook.
    with closing(handler):
        if len(raw) > 65536:
            sys.stderr.write("CODEX_HOOK:input_too_large\n")
            emit_result({}, empty=empty)
            return 0
        # The library worker owns the kept recall service. Each hook captures its prompt locally before using it.
        recaller = None
        if args.config is None and runtime_config is None:
            from .local_endpoint import Recaller, request_recall

            request_recall(location, args.host)
            recaller = Recaller(location, args.host)
            handler.resident_recall = recaller
        result = handler.handle_bytes(raw)
        emit_result(result, diagnostics=handler.diagnostics, empty=empty)
        if recaller is not None:
            outcome = getattr(handler, "resident_outcome", None) or recaller.outcome
            if outcome is not None:
                sys.stderr.write(f"CODEX_RECALL_RESIDENT:{outcome}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
