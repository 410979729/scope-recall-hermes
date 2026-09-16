"""Finite retained-resource lock probes for P10 purge deadlines."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

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
        "from scope_recall.core.file_lock import advisory_file_lock\n"
        "import sys\n"
        "try:\n"
        "  with advisory_file_lock(__import__('pathlib').Path(sys.argv[1]), timeout_seconds=0.08): print('acquired')\n"
        "except TimeoutError: print('timeout')\n"
    )
    with advisory_file_lock(path):
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert completed.stdout.strip() == "timeout"
    assert elapsed < 1.0
