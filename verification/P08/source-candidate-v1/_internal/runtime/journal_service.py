"""Journal infrastructure behind the typed application service."""

from __future__ import annotations

from typing import Any, cast

from ..application.capture_journal import (
    JournalGateway,
    JournalMessagesRequest,
    JournalStageResult,
    JournalTurnRequest,
)
from ..journal.runtime import (
    append_session_tool_journal_entries,
    append_turn_journal_entries,
    stage_pre_compress_journal_entries,
)


class ProviderJournalAdapter:
    """Confine current Provider-shaped journal state to infrastructure."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def append_session_tools(self, request: JournalMessagesRequest) -> None:
        messages = [cast(dict[str, Any], dict(item)) for item in request.messages]
        append_session_tool_journal_entries(self._host, messages)

    def append_turn(self, request: JournalTurnRequest) -> bool:
        return bool(
            append_turn_journal_entries(
                self._host,
                clean_user=request.clean_user,
                clean_assistant=request.clean_assistant,
                user_allowed=request.user_allowed,
                assistant_allowed=request.assistant_allowed,
            )
        )

    def stage_pre_compress(
        self, request: JournalMessagesRequest
    ) -> JournalStageResult:
        messages = [cast(dict[str, Any], dict(item)) for item in request.messages]
        appended, roles = stage_pre_compress_journal_entries(self._host, messages)
        return JournalStageResult(
            appended=int(appended),
            roles=tuple(sorted(str(role) for role in roles)),
        )


def bind_journal_gateway(host: Any) -> JournalGateway:
    return ProviderJournalAdapter(host)
