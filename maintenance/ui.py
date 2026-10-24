"""Start the local owner's memory management page until Ctrl+C."""

from __future__ import annotations

import argparse
import webbrowser

from ..contracts import ContractError
from .memory import open_management
from .restore import RestoreError
from .ui_api import UIApi
from .ui_http import UIServer


def parser():
    """Select the same store as the memory CLI, with no listening-address option."""
    result = argparse.ArgumentParser(prog="scope-recall ui", description=__doc__)
    result.add_argument("--root", help="shared store root")
    result.add_argument("--host", choices=("hermes", "codex", "claude-code", "workbuddy", "dsh"))
    result.add_argument("--instance-root", help="host installation home for a store of its own")
    return result


def main(argv=None):
    """Print and open a fresh process capability, then close the socket on interrupt."""
    args = parser().parse_args(argv)
    try:
        with UIServer(UIApi(open_management(args))) as server:
            print(server.url, flush=True)
            print("Ctrl+C to stop / 按 Ctrl+C 停止", flush=True)
            try:
                webbrowser.open(server.url)
            except webbrowser.Error:
                print("Open the link above / 请打开上方链接", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    except (ContractError, RestoreError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'CONFIG_INVALID')}: {getattr(exc, 'field', 'store')}")
        return 2
