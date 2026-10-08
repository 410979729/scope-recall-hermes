"""Claude Code's session record, read for what the person and the model said on screen.

Claude Code's hooks carry the person's prompt and the model's last message of a turn, not
what the model says while it works, and a hook that cannot write when it runs gets no second
chance.  Claude Code keeps a record of every session, the ``transcript_path`` each hook is
given.  At the end of a turn the lines added since the last read are read here, and the
handler records what they show being said:

* the person's messages, typed or sent while a turn was running: only entries the record
  itself marks as the person's (``origin.kind == "human"``);
* the model's visible text, block by block, as it was shown.

Nothing else: no tool calls or results, no compaction summaries, task notifications, command
output or meta entries.  The record's layout is Claude Code's own and not a published
contract, so whatever is not recognised is skipped, never guessed at.

Where a read stopped is kept beside the entry, in ``<home>/scope-recall/transcripts``.  It is
disposable: without it the next read starts from the top, and what the store already holds is
recognised and skipped, so losing it costs time and never a duplicate.

WorkBuddy keeps a record of the same kind (``workbuddy_said``, ``workbuddy_record_path``), one
``message`` line per message: the person's (the ``<user_query>`` blocks of ``input_text``) and the
model's visible text (``output_text`` blocks).  Its other lines (reasoning, tool calls and results,
titles, snapshots) are skipped, and so is a user message WorkBuddy itself added: one marked
``providerData.isMeta``, a notice that a background task finished, or one with no ``<user_query>``
block (a command and its output, a teammate's report, a slash command's expansion).  A model message
whose words are only an error WorkBuddy showed in place of a reply (``providerData.error``) is
skipped as well.

dsh's session log is Zstandard-compressed and its hooks name no record, so its plugin keeps each
turn's messages and sends them with the Stop (``dsh_lines``), as a client on another machine sends
the lines of its own record.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .boundary import is_task_notification, without_lone_surrogates, workbuddy_record_words

#: How much of the record one read goes through.  A long session's first read spans several turns.
READ_BYTES = 16 * 1024 * 1024
#: The record's opening bytes identify it; a record rewritten under the same name starts over.
_HEAD_BYTES = 4096


@dataclass(frozen=True)
class Said:
    """One visible message and its host identity, if the record supplies one."""

    entry_id: str
    role: str
    text: str
    occurred_at: str
    prompt_id: str | None = None


def _human(origin: object) -> bool:
    return isinstance(origin, dict) and origin.get("kind") == "human"


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n".join(
        block["text"]
        for block in value
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _stamp(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def said(row: object) -> Said | None:
    """What one record entry shows being said, or None for everything else."""
    if not isinstance(row, dict) or row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
        return None
    entry_id, occurred_at = row.get("uuid"), _stamp(row.get("timestamp"))
    if type(entry_id) is not str or not entry_id.strip() or len(entry_id) > 100 or occurred_at is None:
        return None
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    spoken = _spoken(row, message)
    if spoken is None:
        return None
    role, text = spoken
    if not text.strip():
        return None
    # Half of a broken emoji is kept as U+FFFD, with the rest of the message (``boundary.without_lone_surrogates``);
    # the line was skipped and the message lost.  An entry id that cannot be encoded still skips its line.
    text = without_lone_surrogates(text)
    try:
        entry_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return Said(entry_id.strip(), role, text, occurred_at, _prompt_id(row) if role == "user" else None)


def _spoken(row: dict, message) -> tuple[str, str] | None:
    """Who speaks in a record entry, and the words: a person's message, a message they queued while a turn was
    running, or the model's answer; None for every other entry."""
    kind = row.get("type")
    if kind == "user" and _human(row.get("origin")) and message.get("role") == "user":
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return None
        return "user", _text(content)
    if kind == "attachment":
        # A message the person sent while a turn was running reaches the model as a queued command.
        attachment = row.get("attachment") if isinstance(row.get("attachment"), dict) else {}
        if (
            attachment.get("type") != "queued_command"
            or attachment.get("commandMode") != "prompt"
            or not _human(attachment.get("origin"))
        ):
            return None
        return "user", _text(attachment.get("prompt"))
    if kind == "assistant" and message.get("role") == "assistant":
        if row.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            return None
        return "assistant", _text(message.get("content"))
    return None


def _prompt_id(row: dict) -> str | None:
    """The prompt id of a person's message, when it is one the store can bind."""
    prompt_id = row.get("promptId")
    if type(prompt_id) is not str or not prompt_id.strip() or len(prompt_id) > 240:
        return None
    try:
        prompt_id.encode("utf-8")
    except UnicodeEncodeError:
        # An id the store cannot bind would stop every later read at this line; the message is still
        # matched by its words and moment.
        return None
    return prompt_id.strip()


def _milliseconds(value: object) -> str | None:
    """A WorkBuddy record's moment, milliseconds since the epoch (an ISO time is taken as well)."""
    if type(value) is str:
        return _stamp(value)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


#: The content blocks that carry each side's words in a WorkBuddy record.
_WORKBUDDY_BLOCKS = {"user": "input_text", "assistant": "output_text"}
#: How much of a WorkBuddy record's end a Stop reads for the model's last message (``workbuddy_error_reply``).
_TAIL_BYTES = 256 * 1024


def _workbuddy_error(provider: dict) -> str | None:
    """The message of the error a WorkBuddy model message carries (``providerData.error``), if any."""
    error = provider.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message.strip() if type(message) is str and message.strip() else None


def _error_words(text: str, error: str | None) -> bool:
    """Whether ``text`` is the error's message, whitespace aside: WorkBuddy hands its hooks copies with the line breaks
    taken out, and the rest of its matching ignores whitespace as well (``handler._words``)."""
    return error is not None and "".join(text.split()) == "".join(error.split())


def _workbuddy_internal(provider) -> bool:
    """Whether WorkBuddy marked a user message as its own (a meta note, or the compaction's internal prompt)."""
    return provider.get("isMeta") is True or provider.get("isCompactInternal") is True


def _workbuddy_blocks(content, kind) -> list | None:
    """A message's text blocks of one kind (a string is one block); None for any other content."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == kind and isinstance(block.get("text"), str)
        ]
    return None


def workbuddy_said(row: object) -> Said | None:
    """What one line of a WorkBuddy session record shows being said, or None for everything else.

    The person's words are the ``<user_query>`` blocks of a user message (``boundary.workbuddy_record_words``); a user
    message without one is not the person's.  The model's blocks are joined as WorkBuddy joins them for the Stop
    hook's ``last_assistant_message``."""
    if not isinstance(row, dict) or row.get("type") != "message":
        return None
    role = row.get("role")
    kind = _WORKBUDDY_BLOCKS.get(role) if type(role) is str else None
    entry_id, occurred_at = row.get("id"), _milliseconds(row.get("timestamp"))
    if kind is None or type(entry_id) is not str or not entry_id.strip() or len(entry_id) > 100 or occurred_at is None:
        return None
    provider = row.get("providerData") if isinstance(row.get("providerData"), dict) else {}
    if role == "user" and _workbuddy_internal(provider):
        return None
    blocks = _workbuddy_blocks(row.get("content"), kind)
    if blocks is None:
        return None
    text = "".join(blocks) if role == "assistant" else workbuddy_record_words("\n".join(blocks))
    if not text.strip() or (role == "user" and is_task_notification(text)):
        return None
    if role == "assistant" and _error_words(text, _workbuddy_error(provider)):
        # An error WorkBuddy showed in place of the model's reply (not signed in, a model or network failure): the
        # message carries that error and its words are the error's.  The model said nothing (WorkBuddy's agent
        # 2.147.0, not signed in, writes its notice as the reply).
        return None
    text = without_lone_surrogates(text)
    try:
        entry_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return Said(entry_id.strip(), role, text, occurred_at)


def workbuddy_error_reply(path: Path, reply: str) -> bool:
    """Whether a Stop's ``last_assistant_message`` is an error WorkBuddy showed in place of a reply: the model's last
    message in the record carries an error whose message is these words (``workbuddy_said`` skips that message).  A
    record that cannot be read, or whose last model message carries none, says no."""
    if not reply.strip():
        return False
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return False
    for raw in reversed(tail.splitlines()):
        try:
            row = json.loads(raw.decode("utf-8-sig"))  # a record's first line may carry a BOM
        except (UnicodeError, ValueError, RecursionError):
            continue  # the cut first line of the tail, or a line being written
        if isinstance(row, dict) and row.get("type") == "message" and row.get("role") == "assistant":
            provider = row.get("providerData") if isinstance(row.get("providerData"), dict) else {}
            return _error_words(reply, _workbuddy_error(provider))
    return False


#: The most of dsh's messages one Stop takes; its plugin sends the rest with a later one.
_DSH_LINES = 500


def dsh_lines(value: object) -> list[tuple[int, Said | None]]:
    """The messages dsh's plugin sends with a Stop (``distribution/dsh``), as a remote client's record lines.

    dsh keeps no record a hook can read (its session log is compressed), so the plugin keeps each turn's messages
    itself and sends them: ``{"id", "role", "text", "time"}``, ``time`` in milliseconds.  Each is a line numbered from 1;
    one that is not such a message is a line that shows nothing, still counted, so the plugin drops it with the rest."""
    if not isinstance(value, list):
        return []
    return [(index, _dsh_said(row)) for index, row in enumerate(value[:_DSH_LINES], start=1)]


def _dsh_said(row: object) -> Said | None:
    if not isinstance(row, dict):
        return None
    entry_id, role, text = row.get("id"), row.get("role"), row.get("text")
    occurred_at = _milliseconds(row.get("time"))
    if (
        type(entry_id) is not str
        or not entry_id.strip()
        or len(entry_id) > 100
        or role not in ("user", "assistant")
        or type(text) is not str
        or not text.strip()
        or occurred_at is None
    ):
        return None
    try:
        entry_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return Said(entry_id.strip(), role, without_lone_surrogates(text), occurred_at)


def workbuddy_projects() -> Path:
    """Where WorkBuddy keeps its session records, one folder per workspace: ``projects`` in the configuration folder
    its CLI takes from ``CODEBUDDY_CONFIG_DIR``, which a hook inherits; ``~/.workbuddy`` when that names none."""
    configured = os.environ.get("CODEBUDDY_CONFIG_DIR", "").strip()
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".workbuddy"
    return base / "projects"


def workbuddy_record_path(
    value: object, session_id: str, *, record_id: str | None = None, projects: Path | None = None
) -> Path | None:
    """A WorkBuddy session's record: the hook's ``transcript_path`` when it is an existing ``<id>.jsonl`` of this
    session, else that file in one of the workspace folders of ``projects`` (``workbuddy_projects``), else None.

    The record is named by the session's store id when it has one (``record_id``, the hook's ``agent_id``), else by
    the session id.  The hook's path has been reported wrong (``.json`` for ``.jsonl``, cut two characters short), so
    any other path is not read and the record is looked for by its name instead."""
    names = [
        f"{name.strip()}.jsonl"
        for name in (record_id, session_id)
        if type(name) is str
        and name.strip()
        and not any(mark in name for mark in "/\\:")
        and name.strip() not in (".", "..")
    ]
    if type(value) is str and value.strip():
        path = Path(value)
        if path.is_absolute() and path.name in names and path.is_file():
            return path
    root = projects if projects is not None else workbuddy_projects()
    try:
        folders = sorted(folder for folder in root.iterdir() if folder.is_dir())
    except OSError:
        return None
    for name in names:
        for folder in folders:
            if (folder / name).is_file():
                return folder / name
    return None


#: The longest message a client on another machine may send in one read (characters).
WIRE_TEXT_LIMIT = 1_000_000


def said_to_wire(entry: Said) -> dict[str, object]:
    """One message, as a client on another machine sends it to its entry's server."""
    return {
        "entry_id": entry.entry_id,
        "role": entry.role,
        "text": entry.text,
        "occurred_at": entry.occurred_at,
        "prompt_id": entry.prompt_id,
    }


def said_from_wire(value: object) -> Said | None:
    """The message a client sent, held to what ``said`` itself would have produced, or None."""
    if not isinstance(value, dict):
        return None
    entry_id, role, text = value.get("entry_id"), value.get("role"), value.get("text")
    occurred_at, prompt_id = _stamp(value.get("occurred_at")), value.get("prompt_id")
    if (
        type(entry_id) is not str
        or not entry_id.strip()
        or len(entry_id) > 100
        or role not in ("user", "assistant")
        or type(text) is not str
        or not text.strip()
        or len(text) > WIRE_TEXT_LIMIT
        or occurred_at is None
    ):
        return None
    if prompt_id is not None and (
        role != "user" or type(prompt_id) is not str or not prompt_id.strip() or len(prompt_id) > 240
    ):
        return None
    text = without_lone_surrogates(text)
    try:
        entry_id.encode("utf-8")
        if prompt_id is not None:
            prompt_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return Said(entry_id.strip(), role, text, occurred_at, prompt_id.strip() if prompt_id else None)


def record_path(value: object, session_id: str) -> Path | None:
    """The session's own record: an absolute path to an existing ``<session id>.jsonl``, or None."""
    if type(value) is not str or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute() or path.name != f"{session_id}.jsonl" or not path.is_file():
        return None
    return path


def read(
    path: Path, offset: int, *, limit: int = READ_BYTES, rows: Callable[[object], Said | None] = said
) -> list[tuple[int, Said | None]]:
    """The complete lines after ``offset``, each with the offset just past it and what it shows being said
    (``rows``: ``said`` for the claude-code host's record, ``workbuddy_said`` for WorkBuddy's).

    A last line without its newline is still being written and waits for the next read.
    """
    lines: list[tuple[int, Said | None]] = []
    with path.open("rb") as handle:
        handle.seek(offset)
        position = offset
        while position - offset < limit:
            line = handle.readline()
            if not line.endswith(b"\n"):
                break
            position += len(line)
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                # A line nested past what the parser takes would fail every later Stop of the session.
                row = None
            lines.append((position, rows(row)))
    return lines


def _head(path: Path, length: int) -> str:
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read(length)).hexdigest()


class Cursor:
    """Where the last read of one session's record stopped."""

    def __init__(self, home: Path, session_id: str, record: Path) -> None:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        self.path = home / "scope-recall" / "transcripts" / f"{name}.json"
        self.record = record

    def load(self) -> int:
        """The saved offset, or 0 when there is none or the record is no longer the one it was taken on."""
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            offset, head = saved["offset"], saved["head"]
            if type(offset) is not int or not 0 <= offset <= self.record.stat().st_size:
                return 0
            return offset if head == _head(self.record, min(offset, _HEAD_BYTES)) else 0
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def save(self, offset: int) -> None:
        """Keep ``offset`` for the next read; a cursor that cannot be written only costs a longer next read."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(f"{self.path.stem}.{os.getpid()}.tmp")
            pending.write_text(
                json.dumps({"offset": offset, "head": _head(self.record, min(offset, _HEAD_BYTES))}), encoding="utf-8"
            )
            os.replace(pending, self.path)
        except OSError:
            pass
