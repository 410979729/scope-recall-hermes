"""Shared admission Protocol only. Journal and Nightly keep separate implementations."""

from __future__ import annotations

from typing import Any, Protocol


class CandidateAdmission(Protocol):
    def rejection_reason(self, candidate: Any) -> str: ...

    def allowed(self, candidate: Any) -> bool: ...
