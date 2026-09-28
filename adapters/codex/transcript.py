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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from .boundary import without_lone_surrogates

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
    return "\n".join(block["text"] for block in value
                     if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))


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
    kind = row.get("type")
    if kind == "user" and _human(row.get("origin")) and message.get("role") == "user":
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                             for block in content):
            return None
        role, text = "user", _text(content)
    elif kind == "attachment":
        # A message the person sent while a turn was running reaches the model as a queued command.
        attachment = row.get("attachment") if isinstance(row.get("attachment"), dict) else {}
        if (attachment.get("type") != "queued_command" or attachment.get("commandMode") != "prompt"
                or not _human(attachment.get("origin"))):
            return None
        role, text = "user", _text(attachment.get("prompt"))
    elif kind == "assistant" and message.get("role") == "assistant":
        if row.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            return None
        role, text = "assistant", _text(message.get("content"))
    else:
        return None
    if not text.strip():
        return None
    # Half of a broken emoji is kept as U+FFFD, with the rest of the message (``boundary.without_lone_surrogates``);
    # the line was skipped and the message lost.  An entry id that cannot be encoded still skips its line.
    text = without_lone_surrogates(text)
    try:
        entry_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    prompt_id = row.get("promptId") if role == "user" else None
    if type(prompt_id) is not str or not prompt_id.strip() or len(prompt_id) > 240:
        prompt_id = None
    else:
        try:
            prompt_id.encode("utf-8")
        except UnicodeEncodeError:
            # An id the store cannot bind would stop every later read at this line; the message is still
            # matched by its words and moment.
            prompt_id = None
    return Said(entry_id.strip(), role, text, occurred_at, prompt_id.strip() if prompt_id else None)


#: The longest message a client on another machine may send in one read (characters).
WIRE_TEXT_LIMIT = 1_000_000


def said_to_wire(entry: Said) -> dict[str, object]:
    """One message, as a client on another machine sends it to its entry's server."""
    return {"entry_id": entry.entry_id, "role": entry.role, "text": entry.text,
            "occurred_at": entry.occurred_at, "prompt_id": entry.prompt_id}


def said_from_wire(value: object) -> Said | None:
    """The message a client sent, held to what ``said`` itself would have produced, or None."""
    if not isinstance(value, dict):
        return None
    entry_id, role, text = value.get("entry_id"), value.get("role"), value.get("text")
    occurred_at, prompt_id = _stamp(value.get("occurred_at")), value.get("prompt_id")
    if (type(entry_id) is not str or not entry_id.strip() or len(entry_id) > 100 or role not in ("user", "assistant")
            or type(text) is not str or not text.strip() or len(text) > WIRE_TEXT_LIMIT or occurred_at is None):
        return None
    if prompt_id is not None and (role != "user" or type(prompt_id) is not str or not prompt_id.strip()
                                  or len(prompt_id) > 240):
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


def read(path: Path, offset: int, *, limit: int = READ_BYTES) -> list[tuple[int, Said | None]]:
    """The complete lines after ``offset``, each with the offset just past it and what it shows being said.

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
                # A line nested past what the parser takes failed every later Stop of the session (review of rc11).
                row = None
            lines.append((position, said(row)))
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
            pending.write_text(json.dumps({"offset": offset, "head": _head(self.record, min(offset, _HEAD_BYTES))}),
                               encoding="utf-8")
            os.replace(pending, self.path)
        except OSError:
            pass
