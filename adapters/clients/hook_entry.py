"""Codex hook CLI entry: JSON stdin, one JSON stdout, diagnostics on stderr.

``--config`` names a local Codex installation; ``--home`` with ``--host`` names
a client attached to a shared store, Codex or Claude Code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ...runtime.resume_entry import host_process_credential_environment
from .config import load_codex_config, load_shared_client
from .boundary import EMPTY_ANSWER
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
    # Started whether or not the entry's server is asked: the hook recalls itself when the server has not answered in
    # time, and without a helper started here that recall ran by words alone (review of rc11).
    _prestart_vector_helper(raw)
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
    if len(raw) > 65536:
        sys.stderr.write("CODEX_HOOK:input_too_large\n")
        emit_result({}, empty=empty)
        return 0
    # A client attached to a shared store runs the entry's MCP server for as long as it is open, and that server
    # answers the prompt's recall with its vector search warm (``local_endpoint``).  The prompt is stored here.
    recaller = None
    if args.config is None and runtime_config is None:
        from .local_endpoint import Recaller

        recaller = Recaller(location, args.host)
        handler.resident_recall = recaller
    result = handler.handle_bytes(raw)
    emit_result(result, diagnostics=handler.diagnostics, empty=empty)
    if recaller is not None:
        outcome = getattr(handler, "resident_outcome", None) or recaller.outcome
        if outcome is not None:
            sys.stderr.write(f"CODEX_RECALL_RESIDENT:{outcome}\n")
        if handler.diagnostics.last_event == "UserPromptSubmit":
            sys.stdout.flush()  # the answer's bytes leave now; a client that reads to the end still waits for the exit
            _keep_a_resident_server(location, args.host, args.env_file)
    return 0


def _keep_a_resident_server(home: Path, host: str, env_file: Path | None) -> None:
    """After the prompt's answer is out: start the entry's resident recall server when the client keeps one and none
    of this version runs, so that the next prompt finds it warm; one of another version is stopped first, since this
    hook would not ask it (``local_endpoint.ensure_resident``).  Says on stderr what it did, but for ``running``."""
    try:
        from .local_endpoint import ensure_resident, resident_minutes

        minutes = resident_minutes(home, host)
        if minutes <= 0:
            return
        env = env_file.expanduser() if env_file is not None else None
        started = ensure_resident(
            home, host, minutes=minutes, env_file=env if env is not None and env.is_absolute() else None, replace=True
        )
    except Exception:  # noqa: BLE001 - the prompt is answered; a server not started is started by a later one
        started = "failed"
    if started not in ("running", "off"):
        sys.stderr.write(f"CODEX_RECALL_RESIDENT_START:{started}\n")


def _prestart_vector_helper(raw: bytes) -> None:
    """Start the vector search's helper while the prompt is being stored (``vector.process_store.prestart``).

    Each hook is a new process, and a helper started when the recall reached its vector search spent the rest of
    the recall's budget importing LanceDB: Claude Code and Codex recalled from words alone.
    """
    if sys.platform != "win32" or len(raw) > 65536:
        return
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
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
