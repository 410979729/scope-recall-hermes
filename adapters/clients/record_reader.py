"""Reading a client's session record at the end of a turn, for what the person and the model said that no hook
stored: the record Claude Code and WorkBuddy keep beside the session, or the lines a client on another machine (and
dsh's plugin) sends with its Stop (``RecordLines``).  What a hook already stored is recognised and skipped; the
reading resumes where the last one stopped (``transcript.Cursor``)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from scope_recall.contracts import ContractError
from scope_recall.core.private import parse_recording_command

from ..host_profiles import HOSTS
from . import transcript
from .boundary import claude_code_person_text, host_source_key, recorded_source_event
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
_READS_RECORD = frozenset(host for host, profile in HOSTS.items() if profile.record_format is not None)
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
    ``outcomes`` counts capture dispositions for processed lines without retaining their content.
    """

    start: int
    lines: list[tuple[int, transcript.Said | None]]
    through: int | None = None
    outcomes: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dsh(cls, payload: dict[str, Any]) -> RecordLines:
        """The turn's messages dsh's plugin sends inside its Stop's payload (``transcript.dsh_lines``)."""
        return cls(start=0, lines=transcript.dsh_lines(payload.get("record")))

    def reply(self, entry_id: object, text: str) -> transcript.Said | None:
        """The model's line with this id that says ``text``: the line a client names as its Stop's reply (dsh's
        ``reply_id``), found by its words as well as its id, since a message edited later comes again under its id.
        The last such line; None when no line is one."""
        wanted = (entry_id.strip() if type(entry_id) is str else None, "assistant", text)
        found = None
        for _end, said in self.lines:
            if said is not None and (said.entry_id, said.role, said.text) == wanted:
                found = said
        return found


def _without_command_records(lines):
    """Advance past command text without replaying it as captured content."""
    return [
        (
            end,
            None
            if entry is not None
            and entry.role == "user"
            and not entry.private_spans_removed
            and parse_recording_command(entry.text)
            else entry,
        )
        for end, entry in lines
    ]


class RecordReader:
    """A hook's reading of its session record; one per handler."""

    def __init__(self, hook: CodexHookHandler) -> None:
        self._hook = hook

    def reads(self, event: object) -> bool:
        return event in ("Stop", "SessionEnd") and self._hook.host in _READS_RECORD

    def read(
        self,
        session_id: str,
        audience,
        payload: dict[str, Any],
        deadline: float,
        *,
        remote: RecordLines | None = None,
        closed_reply: tuple[str, str] | None = None,
        reply_line: tuple[str, str] | None = None,
    ) -> None:
        """Record what the session record shows was said since the last read (see ``transcript``).

        What a hook already stored is recognised by its words and moment and skipped.  A capture that
        cannot be written now ends the read there; the next Stop starts again from that message.  A client
        on another machine reads its record there and sends the lines (``remote``); the offset reached goes
        back in ``remote.through`` for that client's own cursor.  ``closed_reply`` is the turn a WorkBuddy Stop closed
        and the words of its reply (``session_marks.close_turn``); ``reply_line`` the line of ``remote`` holding the
        reply a dsh Stop stored, and its turn: that line is held as the reply under the turn's key, not by its words.
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
                else (
                    transcript.codex_record_path(payload.get("transcript_path"), session_id)
                    if self._hook.host == "codex"
                    else transcript.record_path(payload.get("transcript_path"), session_id)
                )
            )
            if record is None:
                self._hook.note(
                    self._hook.diagnostics.last_reason or "session_record_unavailable",
                    gaps=("capture_gap:session_record_unavailable",),
                )
                return
            cursor = transcript.Cursor(
                self._hook.config.home
                if isinstance(self._hook.config, SharedClientConfig)
                else self._hook.config.data_directory,
                session_id,
                record,
            )
            start = cursor.load()
            try:
                lines = transcript.read(
                    record,
                    start,
                    rows=transcript.PARSERS[self._hook.host],
                    directory=cursor.directory(),
                )
            except OSError:
                self._hook.note(
                    self._hook.diagnostics.last_reason or "session_record_unavailable",
                    gaps=("capture_gap:session_record_unavailable",),
                )
                return
        page = lines if isinstance(lines, transcript.RecordPage) else None
        if self._hook.host == "claude-code":
            # Read here or sent by a client on another machine, of any version: the record keeps the reminders
            # Claude's desktop app puts in a person's message, and they are not the person's.
            lines = [(end, _person_words(entry)) for end, entry in lines]
        lines = _without_command_records(lines)
        said = [entry for _end, entry in lines if entry is not None]
        # WorkBuddy's record names no turn: a person's message there is the turn its prompt hook kept for the same words,
        # and the model's message after it with the words of the Stop's reply is that Stop's turn: held when the Stop
        # stored it, stored from here when the Stop took it for the previous reply repeated (``close_turn``).
        turns = record_turns(self._hook.config, session_id, said) if workbuddy else {}
        replied = replied_entry(said, closed_reply) if workbuddy else reply_line
        held: tuple[bool, ...] = ()
        if said:
            try:
                held = self._already_held(session_id, audience, said, turns, replied, deadline)
            except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
                # Named, so that a store that fails otherwise than busy says what failed.
                self._hook.diagnostics.capture_error_type = type(exc).__name__
                if isinstance(exc, ContractError):
                    self._hook.diagnostics.note_capture_error(exc.code)
                self._hook.note("session_record_check_failed")
                return
        known = {entry.entry_id for entry, stored in zip(said, held) if stored}
        until = min(deadline, self._hook.clock.monotonic() + _RECORD_READ_S)
        position = start
        for end, entry in lines:
            if entry is not None and entry.entry_id not in known:
                if self._hook.remaining(until) < _RECORD_CAPTURE_MIN_S:
                    break
                bound = self._bind_entry(entry, audience)
                if bound is None:
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
                    self._hook.context(bound, session_id, origin),
                    bound,
                    event,
                    until,
                    entry.private_spans_removed,
                    remote=remote,
                ):
                    break
            position = end
        if remote is not None:
            remote.through = position
        elif position != start:
            cursor.save(position, directory=page.directory_at(position) if page is not None else None)

    def _bind_entry(self, entry: transcript.Said, fallback):
        """Rebind provenance through the same audience authority used by hooks; missing provenance narrows privacy."""
        audience = self._hook.audience_of({"cwd": entry.cwd}) if entry.cwd is not None else fallback
        if audience is None or audience.capture_scope_id is None:
            self._hook.diagnostics.note_capture_error("ACCESS_DENIED")
            return None
        if entry.cwd is None and HOSTS[self._hook.host].record_uses_hook_directory:
            return audience
        home = getattr(self._hook.config, "home", self._hook.config.data_directory)
        bound = self._hook.bind_project_root(
            {"cwd": entry.cwd or str(home), "project_unverified": entry.cwd is None}, audience
        )
        if bound is None:
            self._hook.bind_project_root({"cwd": str(home), "project_unverified": True}, audience)
        return audience

    def _already_held(self, session, audience, said, turns, replied, deadline) -> tuple[bool, ...]:
        """Check duplicate witnesses in batches sharing the original directory and authorization context."""
        groups: dict[str | None, list[tuple[int, transcript.Said]]] = {}
        for index, entry in enumerate(said):
            groups.setdefault(entry.cwd, []).append((index, entry))
        held = [False] * len(said)
        for group in groups.values():
            bound = self._bind_entry(group[0][1], audience)
            if bound is None:
                continue
            stored = self._hook.core.said_in_session(
                self._hook.context(bound, session, "host_generated"),
                cast(str, bound.capture_scope_id),
                [
                    (entry.role, entry.text, entry.occurred_at, self._key(session, entry, turns, replied))
                    for _i, entry in group
                ],
                window_seconds=_RECORD_SAME_MESSAGE_S,
                remaining_seconds=max(0.0, self._hook.remaining(deadline)),
            )
            for (index, _entry), known in zip(group, stored, strict=True):
                held[index] = known
        return tuple(held)

    def _key(
        self, session_id: str, entry: transcript.Said, turns: dict[str, str], replied: tuple[str, str] | None
    ) -> str | None:
        """The key a hook stored a record message under, when the record, a kept turn or the Stop names it."""
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

    def _captured_for_good(self, context, audience, event, deadline: float, removed: int = 0, *, remote=None) -> bool:
        """Capture one record message; False when it may succeed later and the read must stop here."""
        diagnostics = self._hook.diagnostics
        diagnostics.capture_disposition = diagnostics.capture_error_code = diagnostics.capture_error_type = None
        gaps = (f"private_spans_removed:{removed}", "private_content_omitted") if removed else ()
        if gaps:
            event["capture_state"] = "partial"
        self._hook.capture(context, audience, event, deadline=deadline, via_inbox=False, gaps=gaps)
        if remote is not None:
            outcome = diagnostics.capture_disposition or diagnostics.capture_error_detail or "unavailable"
            remote.outcomes[outcome] = remote.outcomes.get(outcome, 0) + 1
        return diagnostics.capture_settled


def _person_words(entry: transcript.Said | None) -> transcript.Said | None:
    """A Claude Code record message with only the person's words in it (``claude_code_person_text``); None for one that
    holds nothing else.  The model's messages are as they were."""
    if entry is None or entry.role != "user":
        return entry
    text = claude_code_person_text(entry.text)
    if text == entry.text:
        return entry
    return replace(entry, text=text) if text.strip() else None
