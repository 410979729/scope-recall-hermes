"""One capture of what a Hermes session said: how long it waits for the store, and how a log line names it."""

from __future__ import annotations

from .boundary import SourceIdentity

#: How long one capture waits for the store's writer lease.
CAPTURE_TIMEOUT_S = 1.0


def label(identity: SourceIdentity) -> str:
    """A capture's key and revision for a log line: never its content."""
    return f"{identity[0]}@{identity[1]}"[:200]
