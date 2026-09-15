"""Codex hook CLI entry: JSON stdin, one JSON stdout, diagnostics on stderr."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .handler import CodexHookHandler, emit_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall Codex hook adapter")
    parser.add_argument("--config", type=Path, required=True, help="Absolute path to codex-installation.json")
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=None,
        help="Absolute path to trusted local runtime worker config",
    )
    args = parser.parse_args(argv)
    # Start the trusted wall-clock budget before configuration/runtime loading;
    # model or hook payload fields never participate in this timestamp.
    hook_started_at = time.monotonic()
    config_path = args.config.expanduser()
    if not config_path.is_absolute():
        sys.stderr.write("CODEX_HOOK:config_path_not_absolute\n")
        emit_result({})
        return 0
    runtime_config = args.runtime_config.expanduser() if args.runtime_config is not None else None
    if runtime_config is not None and not runtime_config.is_absolute():
        sys.stderr.write("CODEX_HOOK:runtime_config_not_absolute\n")
        emit_result({})
        return 0
    try:
        handler = CodexHookHandler.from_config_path(
            str(config_path),
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
