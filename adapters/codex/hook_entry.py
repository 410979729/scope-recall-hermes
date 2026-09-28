"""Codex hook CLI entry: JSON stdin, one JSON stdout, diagnostics on stderr.

``--config`` names a local Codex installation; ``--home`` with ``--host`` names
a client attached to a shared store, Codex or Claude Code.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from ...runtime.resume_entry import host_process_credential_environment
from .config import load_codex_config, load_shared_client
from .handler import CodexHookHandler, HookDiagnostics, emit_result


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
    raw = sys.stdin.buffer.read(65537)
    if (args.home is not None and args.runtime_config is None and len(raw) <= 65536
            and _ask_resident(args.home.expanduser(), args.host, raw, hook_started_at)):
        return 0
    _prestart_vector_helper(raw)
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
        # A hook must answer inside its budget whatever happens; a missing key
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
    if len(raw) > 65536:
        sys.stderr.write("CODEX_HOOK:input_too_large\n")
        emit_result({})
        return 0
    result = handler.handle_bytes(raw)
    emit_result(result, diagnostics=handler.diagnostics)
    return 0


def _ask_resident(home: Path, host: str, raw: bytes, started: float) -> bool:
    """Hand the hook to the entry's MCP server when one runs (``local_endpoint``); True once its answer is out.

    False when none took it: the hook then does the work itself, as it did before there was one.
    """
    from .local_endpoint import ask, client_wait

    try:
        event = json.loads(raw).get("hook_event_name")
    except (ValueError, AttributeError):
        return False
    wait = client_wait(host, event)
    if wait is None or not home.is_absolute():
        return False
    answer = ask(home, host, raw, started=started, budget=wait)
    if answer is None:
        return False
    result, fields = answer
    known = {field.name for field in dataclasses.fields(HookDiagnostics)}
    values = {key: (tuple(value) if key == "capability_gaps" else value)
              for key, value in fields.items() if key in known}
    emit_result(result, diagnostics=HookDiagnostics(**values) if values else None)
    return True


def _prestart_vector_helper(raw: bytes) -> None:
    """Start the vector search's helper while the prompt is being stored (``vector.process_store.prestart``).

    Each hook is a new process, and a helper started when the recall reached its vector search spent the rest of
    the recall's budget importing LanceDB: Claude Code and Codex recalled from words alone.
    """
    if sys.platform != "win32" or len(raw) > 65536:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return
    try:
        from ...vector.process_store import prestart
        prestart()
    except OSError:
        sys.stderr.write("CODEX_HOOK:vector_prestart_failed\n")


if __name__ == "__main__":
    raise SystemExit(main())
