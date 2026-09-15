"""Wait for watchdog ownership before importing or running worker code.

Invoked by absolute file path, so the startup gate needs only the stdlib and
also runs from a clean installed wheel.  Losing the parent's pipe before the
one-byte release token exits without opening a database or starting children.
"""
from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 2 or sys.stdin.buffer.read(1) != b"\x01":
        return 125
    sys.stdin.close()
    from scope_recall.runtime.worker_entry import run_worker

    return run_worker(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
