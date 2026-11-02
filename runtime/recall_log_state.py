"""Bounded worker-published counters and asynchronous process liveness observations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..contracts import ContractError, TrustedContext
from ..core.deadline import RequestDeadline, using_request_deadline
from ..core.storage import SQLiteStorage
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


def queue_observation(binding):
    """Publish bounded partition counters during a worker pass, never during recall."""
    state = dict(observed_at=datetime.now(UTC).isoformat(), groups=[], truncated=True)
    context = TrustedContext(binding, "background-diagnostics", binding.scope_ids, "host_generated")
    storage = SQLiteStorage(binding, timeout_seconds=0.2, upgrade_on_open=False)
    try:
        with using_request_deadline(RequestDeadline.from_budget(0.2)), storage.read(context) as tx:
            scopes = sorted(binding.scope_ids)
            rows = (
                tx._check()
                .execute(
                    "SELECT scope_id,project_id,branch_id,count(*) AS pending,"
                    "sum(state='leased') AS leased,"
                    "coalesce(sum(last_error_code LIKE '%budget_exhausted' "
                    "OR last_error_code LIKE '%budget_unavailable'),0) AS paused "
                    "FROM work_items WHERE state IN ('pending','leased') "
                    f"AND scope_id IN ({','.join('?' for _ in scopes)}) "
                    "GROUP BY scope_id,project_id,branch_id LIMIT 257",
                    scopes,
                )
                .fetchall()
            )
        groups = [dict(row) for row in rows]
        if len(groups) <= 256 and len(json.dumps(groups).encode("utf-8")) <= 32768:
            state.update(groups=groups, truncated=False)
    except (OSError, ValueError, ContractError, sqlite3.Error):
        pass
    return state
