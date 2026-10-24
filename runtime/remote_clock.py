"""Content-free metadata for remote hook envelopes and their durable spool copies.

The transport and backup reader share the wire translation. Privacy decisions
and clock uncertainty belong to Core; this module never opens a store or sends.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


def stored_session_id(session_id: str, *, entry_id: str | None = None) -> str:
    """Translate a client session to its entry's stored identity; local sessions keep their key."""
    return f"{entry_id}:{session_id}" if entry_id is not None else session_id


def _payload(body):
    """Bare transcript records carry no hook payload or routing identity."""
    value = body.get("payload")
    return value if isinstance(value, dict) else {}


def _record_clocks(body):
    """Yield each record's timestamp and its own calibration, including native records."""
    record = body.get("record")
    if isinstance(record, dict):
        for item in record.get("lines") or ():
            if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
                yield item[1].get("occurred_at"), item[1].get("clock")
    native = _payload(body).get("record")
    for row in native if isinstance(native, list) else ():
        if not isinstance(row, dict):
            continue
        value = row.get("time")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            continue
        try:
            moment = datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, ValueError, OSError):
            continue
        yield moment, row.get("clock")


def event_times(body: dict[str, Any], observed_at: str | None) -> list:
    """Distinct event timestamps without copying any message body."""
    payload = _payload(body)
    if payload.get("hook_event_name") == "PrivateCheck":
        return payload.get("times") or []
    moments = [observed_at] if isinstance(observed_at, str) else []
    moments.extend(moment for moment, _clock in _record_clocks(body) if isinstance(moment, str))
    return list(dict.fromkeys(moments))


def private_metadata(body: dict[str, Any], times: list | None = None) -> dict[str, Any]:
    """Bind each event to its original clock; ambiguous duplicates stay uncalibrated."""
    payload = _payload(body)
    moments = event_times(body, body.get("observed_at")) if times is None else times
    clocks = {body.get("observed_at"): body.get("clock")}

    def remember(moment, clock):
        clocks[moment] = clock if moment not in clocks or clocks[moment] == clock else None

    if payload.get("hook_event_name") == "PrivateCheck":
        supplied = payload.get("clocks") or [None] * len(payload.get("times") or [])
        for moment, clock in zip(payload.get("times") or [], supplied, strict=True):
            remember(moment, body.get("clock") if payload.get("fresh_observation") is True else clock)
    for moment, clock in _record_clocks(body):
        remember(moment, clock)
    return dict(
        session_id=payload.get("session_id"),
        project=payload.get("cwd"),
        project_unverified=payload.get("project_unverified") is True,
        rule_marker=body.get("rule_marker"),
        clock=body.get("clock"),
        times=moments,
        clocks=[clocks.get(moment) for moment in moments],
    )
