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

#: How much of the record one read goes through.  A long session's first read spans several turns.
READ_BYTES = 16 * 1024 * 1024
#: The record's opening bytes identify it; a record rewritten under the same name starts over.
_HEAD_BYTES = 4096


@dataclass(frozen=True)
class Said:
    """One message as shown: the record's id for it, whose it is, what it says and when."""

    entry_id: str
    role: str
    text: str
    occurred_at: str


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
    return Said(entry_id.strip(), role, text, occurred_at) if text.strip() else None


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
            except ValueError:
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
