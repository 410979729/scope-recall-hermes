"""Typed request for the unique production recall search path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecallSearchRequest:
    """Parsed ordinary-search arguments after the public facade.

    ``search_memories`` never exposes ``query_vector`` or ``sanitize_output``.
    Those fields exist only for the internal evidence-set path.
    """

    query: str
    limit: int
    recall_mode: str = "advisory"
    query_vector: list[float] | None = None
    sanitize_output: bool = True
