"""Read-only worker liveness observation for the asynchronous recall writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .process_probe import probe_process
from .running_code import live_records


def worker_observation(binding):
    """Sample liveness after enqueue, distinguishing absent or reused process identities."""
    result: dict = dict(worker_running=None, worker_observed_at=datetime.now(UTC).isoformat())
    path = binding.data_directory / "runtime-worker-status.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        status = json.loads(raw) if len(raw) <= 65536 else {}
        if not isinstance(status, dict):
            return result
        pid = status.get("worker_pid")
        if status.get("installation_id") != binding.installation_id or type(pid) is not int:
            return result
        state = probe_process(pid)
        same = any(
            record.pid == pid and record.installation_id == binding.installation_id
            for record in live_records(binding.data_directory)
        )
        result["worker_running"] = state.running if same or not state.running else None
    except (OSError, ValueError, TypeError):
        pass
    return result
