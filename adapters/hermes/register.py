"""Hermes plugin entry for the bounded core adapter slice."""
from __future__ import annotations

from typing import Any


def register(ctx: Any) -> Any:
    from .provider import register_adapter

    return register_adapter(ctx)
