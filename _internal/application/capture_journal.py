"""Typed capture and journal application service contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CaptureTurnRequest:
    user_content: str
    assistant_content: str


@dataclass(frozen=True, slots=True)
class CaptureTurnPlan:
    clean_user: str
    clean_assistant: str
    user_allowed: bool
    assistant_allowed: bool
    journal_user_allowed: bool
    journal_assistant_allowed: bool
    min_capture: int


@dataclass(frozen=True, slots=True)
class JournalMessagesRequest:
    messages: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class JournalTurnRequest:
    clean_user: str
    clean_assistant: str
    user_allowed: bool
    assistant_allowed: bool


@dataclass(frozen=True, slots=True)
class JournalStageResult:
    appended: int
    roles: tuple[str, ...]


@runtime_checkable
class CaptureGateway(Protocol):
    def prepare_turn(self, request: CaptureTurnRequest) -> CaptureTurnPlan: ...

    def capture_turn(self, plan: CaptureTurnPlan) -> None: ...

    def flush(self, timeout: float) -> bool: ...


@runtime_checkable
class JournalGateway(Protocol):
    def append_session_tools(self, request: JournalMessagesRequest) -> None: ...

    def append_turn(self, request: JournalTurnRequest) -> bool: ...

    def stage_pre_compress(
        self, request: JournalMessagesRequest
    ) -> JournalStageResult: ...


class CaptureApplication:
    def __init__(self, gateway: CaptureGateway) -> None:
        self._gateway = gateway

    def prepare_turn(self, request: CaptureTurnRequest) -> CaptureTurnPlan:
        return self._gateway.prepare_turn(request)

    def capture_turn(self, plan: CaptureTurnPlan) -> None:
        self._gateway.capture_turn(plan)

    def flush(self, timeout: float) -> bool:
        return self._gateway.flush(timeout)


class JournalApplication:
    def __init__(self, gateway: JournalGateway) -> None:
        self._gateway = gateway

    def append_session_tools(self, request: JournalMessagesRequest) -> None:
        self._gateway.append_session_tools(request)

    def append_turn(self, request: JournalTurnRequest) -> bool:
        return self._gateway.append_turn(request)

    def stage_pre_compress(
        self, request: JournalMessagesRequest
    ) -> JournalStageResult:
        return self._gateway.stage_pre_compress(request)

