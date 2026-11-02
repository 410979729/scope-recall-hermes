"""Worker control of the single resident recall process belonging to a local shared library."""

from __future__ import annotations

import json
import time

from ...runtime.validation import strict_int
from .config import shared_clients
from .local_endpoint import configured_minutes, ensure_resident, resident_alive, stop_residents


def library_minutes(binding, *, missing: int | None = 0) -> int | None:
    """Return the longest enabled entry idle allowance, preserving an unreadable live configuration as unknown."""
    path = binding.data_directory / "runtime-config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        override = raw.get("resident_recall_minutes")
        if override is not None:
            strict_int("resident_recall_minutes", override, minimum=0, maximum=1440)
            return override
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        return missing
    values = [
        configured_minutes(config.home, config.host, missing=missing) for config, _stamp in shared_clients(binding)
    ]
    known = [value for value in values if value is not None]
    return max(known) if known and (max(known) > 0 or None not in values) else missing


def manage(binding, *, minutes: int | None = None) -> str:
    """Reconcile one library service during a worker pass; never prevent the durable queue from draining."""
    if binding.installation_kind != "shared":
        return "not_shared"
    try:
        clients = list(shared_clients(binding))
        if not clients:
            return "no_clients"
        config = clients[0][0]
        allowance = (library_minutes(binding) or 0) if minutes is None else minutes
        if allowance <= 0:
            stop_residents(config.home, config.host)
            return "disabled"
        try:
            age = time.time() - resident_alive(config.home, config.host).stat().st_mtime
        except FileNotFoundError:
            return "idle"
        if not -60 <= age < allowance * 60:
            return "idle"
        return ensure_resident(config.home, config.host, minutes=allowance, replace=True)
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return "unavailable"
