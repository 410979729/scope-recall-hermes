"""Wait for watchdog ownership before importing or running worker code.

Invoked by absolute file path, so the startup gate needs only the stdlib and
also runs from a clean installed wheel.  Losing the parent's pipe before the
one-byte release token exits without opening a database or starting children.
An optional second argument is the owner's deadline in epoch seconds; a third
selects an independent backup job instead of a model-work drain.
"""

from __future__ import annotations

import sys


def main() -> int:
    if (
        len(sys.argv) not in (2, 3, 4)
        or (len(sys.argv) == 4 and sys.argv[3] != "backup")
        or sys.stdin.buffer.read(1) != b"\x01"
    ):
        return 125
    sys.stdin.close()
    from scope_recall.runtime.worker_entry import run_backup_worker, run_worker

    deadline_epoch = float(sys.argv[2]) if len(sys.argv) >= 3 else None
    if len(sys.argv) == 4:
        from scope_recall.runtime.scheduling import operator_paused

        return run_backup_worker(sys.argv[1], deadline_epoch=deadline_epoch, paused=operator_paused)
    return run_worker(sys.argv[1], deadline_epoch=deadline_epoch)


if __name__ == "__main__":
    raise SystemExit(main())
