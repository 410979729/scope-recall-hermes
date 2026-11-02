"""The container lease excludes a second process using the same store."""

import subprocess
import sys


def _attempt(store):
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from scope_recall.distribution.docker.entry import acquire_container_lock\n"
        "try:\n"
        "    acquire_container_lock(Path(sys.argv[1]))\n"
        "except RuntimeError as exc:\n"
        "    print(exc)\n"
        "    raise SystemExit(1)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", program, str(store)], capture_output=True, text=True, timeout=60, check=False
    )


def test_second_process_cannot_take_container_lease(tmp_path):
    from scope_recall.distribution.docker.entry import acquire_container_lock

    with acquire_container_lock(tmp_path):
        result = _attempt(tmp_path)
    assert result.returncode != 0
    assert "already" in result.stdout.lower()


def test_container_rejects_an_existing_truth_writer(tmp_path):
    from scope_recall.core.writer_lease import holding_truth_writer_lease

    with holding_truth_writer_lease(tmp_path):
        result = _attempt(tmp_path)
    assert result.returncode != 0
    assert "already" in result.stdout.lower()
