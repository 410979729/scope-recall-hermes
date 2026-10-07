"""A client's resident prompt recall server, as a running one starts the next, under the name installed
configurations hold (see this package):
``python -m scope_recall.adapters.codex.resident_entry``.  The code is ``adapters/clients/resident_entry.py``."""

from ..clients.resident_entry import main

if __name__ == "__main__":
    raise SystemExit(main())
