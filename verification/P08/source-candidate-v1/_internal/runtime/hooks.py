from __future__ import annotations

from typing import Any


def observe_memory_write(
    *,
    agent_context: str,
    action: str,
    target: str,
    content: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Keep the Hermes memory-write hook as an explicit no-op.

    Built-in USER.md/MEMORY.md writes stay authoritative. Copying them into
    SQLite here would create stale duplicates after replace/remove.
    """

    del action, target, content, metadata
    if agent_context != "primary":
        return
    return
