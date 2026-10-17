"""A pass whose receipt is larger than a pipe still finishes, and is not called a timeout.

A pipe holds a few kilobytes.  The owner spawns its pass with piped stdout and reads it while the
process runs: waiting for the process to exit before reading a byte would block a pass that
reported more items than fit in that pipe in ``write`` with its work already committed, so it
could never exit, and would be killed at its deadline and reported as ``worker_watchdog_timeout``.

Every background pass would then burn its whole 120-second window, so the queue would move at a
fraction of the rate the pass itself is capable of, and the receipt that would have said so would
be the thing being swallowed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest
from scope_recall.runtime.worker_watchdog import _ChildOutput, _wait_for_exit

#: Comfortably more than a pipe buffer on any host, and the shape of a real
#: receipt: one long JSON line, written last.
CHATTY = textwrap.dedent("""
    import json, sys
    items = [{"work_id": index, "work_type": "embed", "disposition": "completed",
              "state": "done", "error_code": None} for index in range(%d)]
    sys.stderr.write("TEST diagnostic\\n" * 200)
    sys.stdout.write(json.dumps({"status": "completed", "processed": len(items), "items": items}) + "\\n")
    sys.stdout.flush()
""")


def _child(items: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", "-c", CHATTY % items],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.parametrize("items", [1, 200, 2000])
def test_a_child_that_writes_more_than_a_pipe_still_exits(items):
    child = _child(items)
    output = _ChildOutput(child)
    began = time.monotonic()
    try:
        assert _wait_for_exit(child, began + 30), "the child never exited: its own output wedged it"
        assert time.monotonic() - began < 25, "it exited, but only near the deadline"
        stdout, stderr = output.collect()
        receipt = json.loads(stdout.strip().splitlines()[-1])
        assert receipt["processed"] == items, "the receipt survived the reading"
        assert len(receipt["items"]) == items
        assert "TEST diagnostic" in stderr
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_without_reading_the_same_child_cannot_exit():
    """What the reading is load-bearing for: the shape that waits before reading, where the same child cannot exit.

    A test rather than a comment because the failure it describes is invisible from
    the outside -- the work is done, the queue moves, and the only symptom is a timeout that
    looks like a hung worker.
    """
    child = _child(2000)
    try:
        assert not _wait_for_exit(child, time.monotonic() + 3), (
            "a child whose output nobody reads exited anyway; this host's pipe is large enough "
            "to hide the deadlock, so the fix is untested here"
        )
        assert child.poll() is None, "it is blocked in write, with its work already done"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_tail_is_bounded_and_keeps_the_receipt():
    """A runaway child cannot grow the owner's memory, and the receipt is the last line."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-c",
            "import sys\n"
            'for index in range(5000): sys.stdout.write("TEST noise %d\\n" % index)\n'
            'sys.stdout.write(\'{"status":"completed","processed":7}\\n\')\n',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = _ChildOutput(child)
    try:
        assert _wait_for_exit(child, time.monotonic() + 30)
        stdout, _stderr = output.collect()
        lines = stdout.splitlines()
        assert len(lines) <= _ChildOutput.LINES, f"the tail was unbounded: {len(lines)} lines"
        assert json.loads(lines[-1])["processed"] == 7
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
