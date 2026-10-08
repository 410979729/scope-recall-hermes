"""A client's hooks on another machine, and its token, install and flush commands, under the name installed
configurations hold (see this package):
``python -m scope_recall.adapters.codex.remote_client``.  The code is ``adapters/clients/remote_client.py``."""

import sys

from ..clients.remote_client import main as client_main


def main(argv: list[str] | None = None) -> int:
    """``install`` is the installer's (``maintenance/install_remote.py``); every other command is the client's."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["install"]:
        from ...maintenance.install_remote import main as install_main  # this entry is the composition root

        return install_main(args[1:])
    return client_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
