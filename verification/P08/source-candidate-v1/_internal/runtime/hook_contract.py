"""Typed constructor-injected runtime hook lookup."""

from __future__ import annotations

from typing import Protocol, TypeVar


T = TypeVar("T")


class RuntimeHooks(Protocol):
    def resolve(self, name: str, default: T) -> T: ...
