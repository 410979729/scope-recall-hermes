"""Claude Code's session record, read for what the person and the model said on screen.

Claude Code's hooks carry the person's prompt and the model's last message of a turn, not
what the model says while it works, and a hook that cannot write when it runs gets no second
chance.  Claude Code keeps a record of every session, the ``transcript_path`` each hook is
given.  At the end of a turn the lines added since the last read are read here, and the
handler records what they show being said:

* the person's messages, typed or sent while a turn was running: only entries the record
  itself marks as the person's (``origin.kind == "human"``), as the record has them; the
  reminders Claude's desktop app puts in them are taken out where they are stored
  (``record_reader``), so that a client on another machine is held to the same rule;
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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ...contracts import ContractError
from ...core.capture_filters import without_private
from ...core.private import normalize_project_path
from ..host_profiles import HOSTS, HostProfile
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
    cwd: str | None = None
    private_spans_removed: int = 0


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
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    # skipping the line would lose the message.  An entry id that cannot be encoded still skips its line.
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


def codex_said(row: object) -> Said | None:
    """Read only visible response-item messages from a Codex rollout.

    event_msg repeats those messages and is deliberately ignored. Developer instructions, reasoning and function
    calls are not visible speaker evidence. The byte offset supplies an identity where the rollout omits one.
    """
    from ..host_profiles import host_prompt, is_codex_suggestions_reply

    if not isinstance(row, dict) or row.get("type") != "response_item":
        return None
    payload = row.get("payload")
    stamp = _stamp(row.get("timestamp"))
    if not isinstance(payload, dict) or payload.get("type") != "message" or stamp is None:
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    if role == "assistant" and payload.get("channel") not in (None, "final", "commentary"):
        return None
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None
    text = "\n".join(
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") in ("input_text", "output_text")
        and type(block.get("text")) is str
    )
    if not text.strip() or (role == "user" and host_prompt("codex", text)):
        return None
    if role == "assistant" and is_codex_suggestions_reply(text):
        return None
    entry = payload.get("id") or row.get("id")
    if type(entry) is not str or not entry.strip() or len(entry) > 100:
        entry = "rollout:" + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return Said(entry, role, without_lone_surrogates(text), stamp)


def codex_record_path(value: object, session_id: str) -> Path | None:
    """Accept an absolute rollout only when its own session metadata agrees with the hook."""
    if type(value) is not str:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.suffix != ".jsonl":
        return None
    try:
        with path.open("rb") as stream:
            row = json.loads(stream.readline(65537))
        payload = row.get("payload", {})
        if row.get("type") == "session_meta" and payload.get("id") == session_id:
            return path
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _milliseconds(value: object) -> str | None:
    """A WorkBuddy record's moment, milliseconds since the epoch (an ISO time is taken as well)."""
    if type(value) is str:
        return _stamp(value)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value / 1000, UTC)
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
        # An application error notice can occupy the reply field. Its status text does not attest an assistant message.
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
    removed = row.get("private_spans_removed", 0)
    if type(removed) is not int or not 0 <= removed <= 1000000:
        return None
    return Said(entry_id.strip(), role, without_lone_surrogates(text), occurred_at, private_spans_removed=removed)


def workbuddy_session(path: Path) -> str | None:
    """Read the first message's attested session ID under a byte limit; filenames do not attest identity."""
    try:
        with path.open("rb") as stream:
            consumed = 0
            while consumed < READ_BYTES:
                line = stream.readline(65537)
                if not line or len(line) > 65536:
                    return None
                consumed += len(line)
                row = json.loads(line)
                if isinstance(row, dict) and row.get("type") == "message":
                    session = row.get("sessionId")
                    return session if type(session) is str and session.strip() and len(session) <= 240 else None
    except (OSError, ValueError):
        pass
    return None


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
    the session id.  The hook's path can be wrong (``.json`` for ``.jsonl``, cut two characters short), so
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
    text, count = without_private(entry.text)
    return {
        "entry_id": entry.entry_id,
        "role": entry.role,
        "text": text,
        **(
            {"private_spans_removed": count + entry.private_spans_removed}
            if count or entry.private_spans_removed
            else {}
        ),
        "occurred_at": entry.occurred_at,
        "prompt_id": entry.prompt_id,
        **({"cwd": entry.cwd} if entry.cwd is not None else {}),
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
    directory = value.get("cwd")
    directory = directory if type(directory) is str and 0 < len(directory) <= 8192 else None
    removed = value.get("private_spans_removed", 0)
    if type(removed) is not int or not 0 <= removed <= 1000000:
        return None
    return Said(entry_id.strip(), role, text, occurred_at, prompt_id.strip() if prompt_id else None, directory, removed)


#: The 16 MiB tail bounds per-prompt I/O while leaving room for records after the latest compaction; if a larger
#: session record has no compaction in that tail, ``context_since`` returns ``None`` instead of treating the whole
#: session record as in context.
_CONTEXT_TAIL_BYTES = 16 * 1024 * 1024
#: What a record line naming a compaction holds: the summary that replaces what came before, or its boundary.  Only
#: the names: a line is parsed and its values checked (``_compaction``) whatever spacing its JSON has.
_COMPACTION_MARKS = (b'"isCompactSummary"', b'"compact_boundary"')


def _compaction(row: object) -> str | None:
    """The moment of a record line that is a compaction of the main session (not of a subagent's), or None."""
    if not isinstance(row, dict) or row.get("isSidechain"):
        return None
    boundary = row.get("type") == "system" and row.get("subtype") == "compact_boundary"
    if row.get("isCompactSummary") is True or boundary:
        return _stamp(row.get("timestamp"))
    return None


def _line(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def context_since(path: Path) -> str | None:
    """Since when Claude Code's model still holds the session's own turns word for word: the session record's last
    compaction, or its first line when it has had none.  None when that cannot be told: a record that cannot be read,
    or one longer than ``_CONTEXT_TAIL_BYTES`` with no compaction in that much of its end.

    What was said before a compaction reaches the model only as the compaction's summary, so an automatic recall may
    bring it back; what was said after it is still there, and recalled it would be a second copy."""
    try:
        with path.open("rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - _CONTEXT_TAIL_BYTES)
            handle.seek(start)
            lines = handle.read().split(b"\n")
    except OSError:
        return None
    if start > 0:
        lines = lines[1:]  # cut by where the read began
    for raw in reversed(lines):
        if any(mark in raw for mark in _COMPACTION_MARKS) and (moment := _compaction(_line(raw))) is not None:
            return moment
    if start > 0:
        return None
    for raw in lines:
        row = _line(raw)
        if isinstance(row, dict) and not row.get("isSidechain") and (moment := _stamp(row.get("timestamp"))):
            return moment
    return None


def record_path(value: object, session_id: str) -> Path | None:
    """The session's own record: an absolute path to an existing ``<session id>.jsonl``, or None."""
    if type(value) is not str or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute() or path.name != f"{session_id}.jsonl" or not path.is_file():
        return None
    return path


PARSERS = {"claude-code": said, "workbuddy": workbuddy_said, "codex": codex_said}


class RecordPage(list[tuple[int, Said | None]]):
    """A bounded page and the directory attested at each context-change offset.

    Directory checkpoints describe source provenance, never grants. Keeping context-only lines lets a later page
    continue an interrupted turn without treating the host home or the next turn's directory as the original one.
    """

    def __init__(self, offset: int, directory: str | None) -> None:
        super().__init__()
        self.directories: list[tuple[int, str | None]] = [(offset, directory)]

    def directory_at(self, offset: int) -> str | None:
        """Return the last attested directory at or before the acknowledged byte offset."""
        return next((value for end, value in reversed(self.directories) if end <= offset), None)

    def observe_directory(self, end: int, row: object, profile: HostProfile | None, *, visible: bool) -> None:
        """Interpret only the host-declared context records or visible message fields."""
        if profile is None or not isinstance(row, dict):
            return
        if profile.record_directory_events is not None:
            kind = row.get("type")
            if type(kind) is not str or kind not in profile.record_directory_events:
                return
        elif not visible:
            return
        value: object = row
        for key in profile.record_directory:
            if not isinstance(value, dict) or key not in value:
                return
            value = value[key]
        try:
            directory = normalize_project_path(value, require_identity=True) if type(value) is str else None
        except ContractError:
            directory = None
        self.directories.append((end, directory))


def read(
    path: Path,
    offset: int,
    *,
    limit: int = READ_BYTES,
    rows: Callable[[object], Said | None] = said,
    directory: str | None = None,
    session_id: str | None = None,
) -> RecordPage:
    """Read complete lines under a byte budget, carrying host-attested directory changes through the page.

    A final line without its newline is still being written. Its offset and context wait for the next read.
    """
    lines = RecordPage(offset, directory)
    profile = next((HOSTS[host] for host, parser in PARSERS.items() if parser is rows), None)
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
                row = None
            if (
                rows is workbuddy_said
                and session_id is not None
                and isinstance(row, dict)
                and row.get("type") == "message"
                and row.get("sessionId") != session_id
            ):
                raise ValueError("record_session_mismatch")
            entry = rows(row)
            lines.observe_directory(position, row, profile, visible=entry is not None)
            if entry is not None:
                entry = replace(entry, cwd=lines.directory_at(position))
                if rows is codex_said and entry.entry_id.startswith("rollout:"):
                    entry = replace(entry, entry_id=f"rollout:{position}")
            lines.append((position, entry))
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

    def directory(self) -> str | None:
        """Return cached provenance only while the saved byte offset still belongs to this record."""
        if self.load() <= 0:
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("directory")
            return value if type(value) is str and Path(value).is_absolute() else None
        except (OSError, ValueError, AttributeError):
            return None

    def save(self, offset: int, *, directory: str | None = None) -> None:
        """Keep ``offset`` for the next read; a cursor that cannot be written only costs a longer next read."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(f"{self.path.stem}.{os.getpid()}.tmp")
            pending.write_text(
                json.dumps(
                    {"offset": offset, "head": _head(self.record, min(offset, _HEAD_BYTES)), "directory": directory}
                ),
                encoding="utf-8",
            )
            os.replace(pending, self.path)
        except OSError:
            pass

    def save_state(self, **fields: object) -> None:
        """Keep the capture inbox's logical outcome beside the disposable offset, without source content."""
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            saved.update(fields)
            pending = self.path.with_name(f"{self.path.stem}.{os.getpid()}.tmp")
            pending.write_text(json.dumps(saved), encoding="utf-8")
            os.replace(pending, self.path)
        except (OSError, ValueError):
            pass
