"""A client's hooks on another machine, and its token, install and flush commands, under the name installed
configurations hold (see this package):
``python -m scope_recall.adapters.codex.remote_client``.  The code is ``adapters/clients/remote_client.py``."""

from ..clients.remote_client import main

if __name__ == "__main__":
    raise SystemExit(main())
