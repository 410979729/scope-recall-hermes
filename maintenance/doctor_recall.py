"""Read-only recall-log health and request lookup for the installation owner."""

from __future__ import annotations

import sqlite3

from ..contracts import ContractError, TrustedContext
from ..core import CoreConfig, MemoryCore
from ..core.management import MemoryManagement
from ..core.recall_log_writer import read_health
from ..core.restore import InstallationMaintenance
from ..core.storage import SQLiteStorage
from .doctor_report import record_check


def check_recall_log(report, binding, *, request_id=None):
    """Report lossy writes and resolve one request without opening model ports."""
    report.injection_usage = {}
    path = binding.data_directory / "recall.sqlite3"
    core = MemoryCore(CoreConfig(binding), storage=SQLiteStorage(binding, upgrade_on_open=False))
    core.recall_diagnostics.expire(core.clock.utc_now())
    core.recall_diagnostics.flush()
    health = read_health(path)
    report.recall_log = dict(health, present=path.is_file())
    if health.get("dropped_full") or health.get("write_failures"):
        report.capability_gaps.append("recall_log_dropped")
        record_check(report, "recall_log", "dropped")
    else:
        record_check(report, "recall_log", "ok" if path.is_file() else "not_recorded")
    context = TrustedContext(binding, "local-management", binding.scope_ids, "host_generated")
    try:
        rows = MemoryManagement(core, InstallationMaintenance(context)).recalls(
            request_id=request_id, limit=1 if request_id else 30
        )
        items = rows.get("items") or []
        latest = next((row for row in items if "injection" in row), None)
        report.injection_usage = (
            dict(latest["injection"], session_id=latest["session_id"], request_id=latest["request_id"])
            if latest
            else {}
        )
        if request_id is not None:
            report.recall_attempt = (
                rows["items"][0] if rows["items"] else dict(request_id=request_id, state="not_found")
            )
    except (OSError, ValueError, ContractError, sqlite3.Error):
        report.recall_attempt = dict(request_id=request_id, state="unavailable")
