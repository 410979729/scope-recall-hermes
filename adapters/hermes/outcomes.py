"""Bounded outcome tracking for success, failure, cancel, and truncation gaps."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Literal

OutcomeState = Literal["open", "success", "failure", "cancelled", "interrupted", "truncated"]


@dataclass
class TurnOutcomeTracker:
    """Record bounded gaps when a turn never reaches a success-only post-LLM path."""

    _turns: dict[tuple[str, str], OutcomeState] = field(default_factory=dict)
    _gaps: deque[str] = field(default_factory=lambda: deque(maxlen=128))

    def _set(self, key: tuple[str, str], state: OutcomeState) -> None:
        self._turns[key] = state
        while len(self._turns) > 256:
            old = self._turns.pop(next(iter(self._turns)))
            if old == "open":
                self._gaps.append("outcome_gap:tracking_capacity")

    def open_turn(self, session_id: str, turn_id: str) -> None:
        key = (session_id, turn_id)
        if key not in self._turns:
            self._set(key, "open")

    def mark_success(self, session_id: str, turn_id: str) -> None:
        self._set((session_id, turn_id), "success")

    def mark_failure(self, session_id: str, turn_id: str, *, reason: str) -> None:
        key = (session_id, turn_id)
        if self._turns.get(key) == "success":
            return
        self._set(key, "failure")
        self._gaps.append(f"outcome_gap:failure:{session_id}:{turn_id}:{reason}")

    def mark_cancelled(self, session_id: str, turn_id: str) -> None:
        key = (session_id, turn_id)
        if self._turns.get(key) == "success":
            return
        self._set(key, "cancelled")
        self._gaps.append(f"outcome_gap:cancelled:{session_id}:{turn_id}")

    def mark_interrupted(self, session_id: str, turn_id: str) -> None:
        key = (session_id, turn_id)
        if self._turns.get(key) == "success":
            return
        self._set(key, "interrupted")
        self._gaps.append(f"outcome_gap:interrupted:{session_id}:{turn_id}")

    def mark_truncated(self, session_id: str, turn_id: str) -> None:
        key = (session_id, turn_id)
        if self._turns.get(key) == "success":
            return
        self._set(key, "truncated")
        self._gaps.append(f"outcome_gap:truncated:{session_id}:{turn_id}")

    def pending_gaps(self) -> tuple[str, ...]:
        gaps = list(self._gaps)
        for (session_id, turn_id), state in self._turns.items():
            if state == "open":
                gaps.append(f"outcome_gap:open:{session_id}:{turn_id}")
        return tuple(dict.fromkeys(gaps))

    def reset_session(self, session_id: str) -> None:
        for key in [item for item in self._turns if item[0] == session_id]:
            self._turns.pop(key, None)
        self._gaps = deque((gap for gap in self._gaps if f":{session_id}:" not in gap), maxlen=128)

    def snapshot(self) -> dict[str, str]:
        return {f"{session}:{turn}": state for (session, turn), state in self._turns.items()}
