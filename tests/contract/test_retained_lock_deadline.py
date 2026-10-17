"""Finite retained-resource lock probes for P10 purge deadlines."""

from __future__ import annotations

import subprocess
import sys
import threading

from scope_recall.core.file_lock import advisory_file_lock


def test_retained_lock_timeout_returns_quickly_for_competing_thread(tmp_path):
    path = tmp_path / "retained.lock"
    started = threading.Event()
    outcome: list[str] = []

    def contend() -> None:
        started.set()
        try:
            with advisory_file_lock(path, timeout_seconds=0.08):
                outcome.append("acquired")
        except TimeoutError:
            outcome.append("timeout")

    with advisory_file_lock(path):
        thread = threading.Thread(target=contend)
        thread.start()
        assert started.wait(1)
        thread.join(1)
        assert not thread.is_alive()
    assert outcome == ["timeout"]


def test_retained_lock_timeout_returns_quickly_for_competing_process(tmp_path):
    path = tmp_path / "retained-process.lock"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from scope_recall.core.file_lock import advisory_file_lock\n"
        "started = time.monotonic()\n"
        "try:\n"
        "  with advisory_file_lock(Path(sys.argv[1]), timeout_seconds=0.08): outcome = 'acquired'\n"
        "except TimeoutError: outcome = 'timeout'\n"
        "print(outcome, time.monotonic() - started)\n"
    )
    with advisory_file_lock(path):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    outcome, waited = completed.stdout.split()
    assert (completed.returncode, outcome) == (0, "timeout"), completed.stderr
    # Timed by the child around the lock alone: its interpreter's start-up, which a busy runner stretches past a
    # second, is not the wait under test.
    assert float(waited) < 1.0
