"""The hook every installed client runs, under the name installed configurations hold (see this package):
``python -m scope_recall.adapters.codex.hook_entry``.  The code is ``adapters/clients/hook_entry.py``."""

from ..clients.hook_entry import main

if __name__ == "__main__":
    raise SystemExit(main())
