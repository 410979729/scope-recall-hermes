"""Reading a client's session record at the end of a turn, for what the person and the model said that no hook
stored: the record Claude Code and WorkBuddy keep beside the session, or the lines a client on another machine (and
dsh's plugin) sends with its Stop (``RecordLines``).  What a hook already stored is recognised and skipped; the
reading resumes where the last one stopped (``transcript.Cursor``)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scope_recall.contracts import ContractError

from . import transcript
from .boundary import host_source_key, recorded_source_event
from .config import SharedClientConfig
from .session_marks import record_turns, replied_entry

if TYPE_CHECKING:
    from .handler import CodexHookHandler

#: Clients whose Stop and SessionEnd also read the session record (``transcript``): what the person said,
#: whatever the prompt hook could not write, and what the model said while it worked.  Claude Code waits
#: 10 s for these hooks; a turn's lines take well under a second, and a long backlog is read over several
#: turns, at most ``_RECORD_READ_S`` each, so the end of a turn is not held up.
#: dsh has no record a hook can read (its session log is compressed); its plugin sends the turn's messages with the Stop
#: as the lines a remote client sends (``transcript.dsh_lines``).
_READS_RECORD = frozenset({"claude-code", "workbuddy", "dsh"})
_RECORD_READ_S = 3.0
#: A capture is started only with this much of the reading time left.
_RECORD_CAPTURE_MIN_S = 0.5
#: A hook's copy of a message and the record's are the same message when the words match and the moments are this close.
_RECORD_SAME_MESSAGE_S = 120.0


@dataclass
class RecordLines:
    """Lines a client on another machine read from its own session record, from ``start``.

    Each is the offset just past the line and what it shows being said (``transcript.said``); a line that
    shows nothing may be left out, as long as the last offset the client read is present.  The handler sets
    ``through`` to the offset every stored line reaches, which is where that client's cursor may move.
    """

    start: int
    lines: list[tuple[int, "transcript.Said | None"]]
    through: int | None = None


class RecordReader:
    """A hook's reading of its session record; one per handler."""

    def __init__(self, hook: CodexHookHandler) -> None:
        self._hook = hook

    def reads(self, event: object) -> bool:
        return (
            event in ("Stop", "SessionEnd")
            and self._hook.host in _READS_RECORD
            and isinstance(self._hook.config, SharedClientConfig)
        )

    def read(
        self,
        session_id: str,
        audience,
        payload: dict[str, Any],
        deadline: float,
        *,
        remote: RecordLines | None = None,
        closed_reply: tuple[str, str] | None = None,
    ) -> None:
        """Record what the session record shows was said since the last read (see ``transcript``).

        What a hook already stored is recognised by its words and moment and skipped.  A capture that
        cannot be written now ends the read there; the next Stop starts again from that message.  A client
        on another machine reads its record there and sends the lines (``remote``); the offset reached goes
        back in ``remote.through`` for that client's own cursor.  ``closed_reply`` is the turn a WorkBuddy Stop closed
        and the words of its reply (``session_marks.close_turn``).
        """
        cursor = None
        workbuddy = self._hook.host == "workbuddy"
        if remote is not None:
            start, lines = remote.start, remote.lines
        else:
            record = (
                transcript.workbuddy_record_path(
                    payload.get("transcript_path"), session_id, record_id=payload.get("agent_id")
                )
                if workbuddy
                else transcript.record_path(payload.get("transcript_path"), session_id)
            )
            if record is None:
                self._hook.note("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
            cursor = transcript.Cursor(self._hook.config.home, session_id, record)
            start = cursor.load()
            try:
                lines = transcript.read(record, start, rows=transcript.workbuddy_said if workbuddy else transcript.said)
            except OSError:
                self._hook.note("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
        said = [entry for _end, entry in lines if entry is not None]
        # WorkBuddy's record names no turn: a person's message there is the turn its prompt hook kept for the same words,
        # and the model's message after it with the words of the Stop's reply is that Stop's turn: held when the Stop
        # stored it, stored from here when the Stop took it for the previous reply repeated (``close_turn``).
        turns = record_turns(self._hook.config, session_id, said) if workbuddy else {}
        replied = replied_entry(said, closed_reply) if workbuddy else None
        held: tuple[bool, ...] = ()
        if said:
            try:
                held = self._hook.core.said_in_session(
                    self._hook.context(audience, session_id, "host_generated"),
                    audience.capture_scope_id,
                    [
                        (entry.role, entry.text, entry.occurred_at, self._key(session_id, entry, turns, replied))
                        for entry in said
                    ],
                    window_seconds=_RECORD_SAME_MESSAGE_S,
                    remaining_seconds=max(0.0, self._hook.remaining(deadline)),
                )
            except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
                # Named, so that a store that fails otherwise than busy says what failed (review of rc13).
                self._hook.diagnostics.capture_error_type = type(exc).__name__
                self._hook.note("session_record_check_failed")
                return
        known = {entry.entry_id for entry, stored in zip(said, held) if stored}
        until = min(deadline, self._hook.clock.monotonic() + _RECORD_READ_S)
        position = start
        for end, entry in lines:
            if entry is not None and entry.entry_id not in known:
                if self._hook.remaining(until) < _RECORD_CAPTURE_MIN_S:
                    break
                event = recorded_source_event(
                    installation_id=self._hook.config.installation_id,
                    host=self._hook.host,
                    session_id=session_id,
                    entry_id=entry.entry_id,
                    role=entry.role,
                    text=entry.text,
                    occurred_at=entry.occurred_at,
                    recorded_at=self._hook.clock.utc_now(),
                )
                origin = "human_direct" if entry.role == "user" else "assistant_visible"
                if not self._captured_for_good(
                    self._hook.context(audience, session_id, origin), audience, event, until
                ):
                    break
            position = end
        if remote is not None:
            remote.through = position
        elif position != start:
            cursor.save(position)

    def _key(
        self, session_id: str, entry: "transcript.Said", turns: dict[str, str], replied: tuple[str, str] | None
    ) -> str | None:
        """The key a hook stored a record message under, when the record or a kept turn names it."""
        if entry.role == "user" and (turns.get(entry.entry_id) or entry.prompt_id):
            kind, event_id = "user", turns.get(entry.entry_id) or entry.prompt_id
        elif replied is not None and entry.entry_id == replied[0]:
            kind, event_id = "assistant", replied[1]
        else:
            return None
        return host_source_key(
            host=self._hook.host,
            installation_id=self._hook.config.installation_id,
            session_id=session_id,
            event_kind=kind,
            event_id=event_id,
        )

    def _captured_for_good(self, context, audience, event, deadline: float) -> bool:
        """Capture one record message; False when it may succeed later and the read must stop here."""
        diagnostics = self._hook.diagnostics
        diagnostics.capture_disposition = diagnostics.capture_error_code = diagnostics.capture_error_type = None
        self._hook.capture(context, audience, event, deadline=deadline, via_inbox=False)
        return diagnostics.capture_settled
