"""What a hook keeps beside an entry between its calls, by session: WorkBuddy's turns, and the threads Codex opened
to ask the model for suggestions.  Each hook is a process of its own, so what one call learns reaches the next only
through these small files; each is disposable, and losing one costs a match by words and moment, never a capture."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from scope_recall.contracts import ContractError, utc_timestamp
from scope_recall.core.file_lock import advisory_file_lock

from . import transcript
from .boundary import TURN_FIELD, turn_id_from_payload
from .config import SharedClientConfig

#: How long a thread that asked for suggestions stays marked.  Its tool calls, answer and end follow within minutes;
#: an older mark is removed the next time a thread is marked.
_SUGGESTIONS_THREAD_SECONDS = 24 * 3600


def mark_path(config, kind: str, session_id: str) -> Path:
    """One session's mark of ``kind``: beside the pointer for an entry of a shared store, in its data for a store of
    its own."""
    folder = (
        Path(config["state_dir"])
        if isinstance(config, dict)
        else Path(config.home) / "scope-recall"
        if isinstance(config, SharedClientConfig)
        else Path(config.data_directory)
    ) / kind
    return folder / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _read_compaction(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(value, dict)
            and (value.get("seq") is None or (type(value["seq"]) is int and 1 <= value["seq"] < 2**63))
            and type(value.get("epoch")) is str
            and 1 <= len(value["epoch"]) <= 64
            and type(value.get("at")) is str
        ):
            value["at"] = utc_timestamp(value["at"])
            value.setdefault("ordinal", value["seq"])
            if type(value["ordinal"]) is int and 1 <= value["ordinal"] < 2**63:
                return value
    except (OSError, UnicodeError, ValueError, ContractError):
        pass
    return None


def _write_compaction(path: Path, value: dict) -> None:
    """Publish a complete counter or pending mark atomically."""
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def compaction_marker(config, session_id: str) -> dict | None:
    """Read the newest pending mark, materializing concurrent signals when possible."""
    path = mark_path(config, "compactions", session_id)
    counter = mark_path(config, "compaction-counters", session_id)
    try:
        with advisory_file_lock(path.with_name(path.name + ".lock"), timeout_seconds=0):
            current = _read_compaction(path)
            state = _read_compaction(counter)
            for pending in sorted(path.parent.glob(path.name + ".*.pending")):
                item = _read_compaction(pending)
                if item is None:
                    continue
                state = {
                    "epoch": state["epoch"] if state else item["epoch"],
                    "seq": state["seq"] + 1 if state and state["seq"] is not None else None,
                    "ordinal": (state["ordinal"] if state else 0) + 1,
                    "at": item["at"],
                }
                counter.parent.mkdir(parents=True, exist_ok=True)
                _write_compaction(counter, state)
                _write_compaction(path, state)
                pending.unlink(missing_ok=True)
                current = state
            return current
    except (OSError, TimeoutError):
        try:
            marks = [_read_compaction(candidate) for candidate in (path, *path.parent.glob(path.name + ".*.pending"))]
            return max((mark for mark in marks if mark), key=lambda mark: mark["ordinal"], default=None)
        except OSError:
            return None


def compaction_at(config, session_id: str) -> str | None:
    """Read the recorded time of the newest pending compaction."""
    mark = compaction_marker(config, session_id)
    return mark["at"] if mark else None


def mark_compaction(config, session_id: str, at: str) -> int | None:
    """Advance the client-owned session counter without waiting for the truth writer.

    A lost counter has an unknown global base until the core returns its watermark;
    the generation and local ordinal still make every pending signal distinct.
    """
    try:
        at = utc_timestamp(at)
        path = mark_path(config, "compactions", session_id)
        counter = mark_path(config, "compaction-counters", session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        counter.parent.mkdir(parents=True, exist_ok=True)
        try:
            with advisory_file_lock(path.with_name(path.name + ".lock"), timeout_seconds=0):
                state = _read_compaction(counter)
                mark = {
                    "seq": state["seq"] + 1 if state and state["seq"] is not None else None,
                    "epoch": state["epoch"] if state else uuid.uuid4().hex,
                    "ordinal": (state["ordinal"] if state else 0) + 1,
                    "at": at,
                }
                _write_compaction(counter, mark)
                _write_compaction(path, mark)
                return mark["ordinal"]
        except TimeoutError:
            state = _read_compaction(counter)
            mark = {
                "seq": state["seq"] + 1 if state and state["seq"] is not None else None,
                "epoch": state["epoch"] if state else uuid.uuid4().hex,
                "ordinal": (state["ordinal"] if state else 0) + 1,
                "at": at,
            }
            _write_compaction(path.with_name(f"{path.name}.{uuid.uuid4().hex}.pending"), mark)
            return mark["ordinal"]
    except (OSError, ContractError):
        return None


def rebase_compaction(config, session_id: str, epoch: str, ordinal: int, watermark: int) -> None:
    """Restore the client counter from a verified server watermark without acknowledging delivery."""
    if type(watermark) is not int or not 0 <= watermark < 2**63:
        return
    path = mark_path(config, "compactions", session_id)
    try:
        with advisory_file_lock(path.with_name(path.name + ".lock"), timeout_seconds=0):
            counter_path = mark_path(config, "compaction-counters", session_id)
            counter = _read_compaction(counter_path)
            if counter is None or counter["epoch"] != epoch:
                return
            base = max((counter["seq"] or counter["ordinal"]) - counter["ordinal"], watermark - ordinal)
            counter["seq"] = base + counter["ordinal"]
            _write_compaction(counter_path, counter)
            for candidate in (path, *path.parent.glob(path.name + ".*.pending")):
                value = _read_compaction(candidate)
                if value is not None and value["epoch"] == epoch:
                    value["seq"] = base + value["ordinal"]
                    _write_compaction(candidate, value)
    except (OSError, TimeoutError):
        pass


def clear_compaction(
    config, session_id: str, acknowledged: int, epoch: str | None = None, watermark: int | None = None
) -> None:
    """Remove pending signals no newer than the confirmed client counter."""
    path = mark_path(config, "compactions", session_id)
    try:
        with advisory_file_lock(path.with_name(path.name + ".lock"), timeout_seconds=0):
            counter_path = mark_path(config, "compaction-counters", session_id)
            counter = _read_compaction(counter_path)
            if epoch is None:
                epoch = counter["epoch"] if counter else None
            if counter is not None and counter["epoch"] == epoch and type(watermark) is int and 0 <= watermark < 2**63:
                base = max((counter["seq"] or counter["ordinal"]) - counter["ordinal"], watermark - acknowledged)
                counter["seq"] = base + counter["ordinal"]
                _write_compaction(counter_path, counter)
            for candidate in (path, *path.parent.glob(path.name + ".*.pending")):
                value = _read_compaction(candidate)
                if value is not None and value["epoch"] == epoch and value["ordinal"] <= acknowledged:
                    candidate.unlink(missing_ok=True)
    except (OSError, TimeoutError):
        pass


def _thread_mark(config, session_id: str) -> Path:
    """One thread's mark (``mark_path``)."""
    return mark_path(config, "host-threads", session_id)


def mark_suggestions_thread(config, session_id: str) -> None:
    """Remember a thread Codex opened to ask for suggestions; a mark that cannot be written lets only its rest in."""
    mark = _thread_mark(config, session_id)
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
        cutoff = time.time() - _SUGGESTIONS_THREAD_SECONDS
        for count, old in enumerate(mark.parent.iterdir()):
            if count >= 256:
                break
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def in_suggestions_thread(config, session_id: str, *, ended: bool = False) -> bool:
    """Whether a hook belongs to a thread ``mark_suggestions_thread`` marked; the thread's end removes the mark."""
    mark = _thread_mark(config, session_id)
    try:
        marked = time.time() - mark.stat().st_mtime < _SUGGESTIONS_THREAD_SECONDS
    except OSError:
        return False
    if ended or not marked:
        try:
            mark.unlink(missing_ok=True)
        except OSError:
            pass
    return marked


# -- WorkBuddy's turns ---------------------------------------------------------
# WorkBuddy's hooks name no turn they share.  Its ``generation_id`` is the id of the session's latest model request,
# made anew for each request: a prompt carries the previous turn's last one (none on a session's first prompt) and its
# Stop carries this turn's.  So a prompt opens a turn, under that id when no turn of the session has it yet and else
# under one made from the session, the person's words and the moment; the turn is kept in a small file per session for
# the Stop that closes it and the read of the session record after, and the session's end removes it.  The file is
# disposable: without it a Stop makes a turn of its own, and the record is matched by words and moment.

#: How long a session's turns are kept; older ones are removed when a turn is kept.
_TURNS_SECONDS = 24 * 3600
#: Turns kept per session: a Stop's read of the record covers its own turn and any before it that fired no Stop.
_TURNS_KEPT = 16


def words_of(text: str) -> str:
    """What two copies of one message share whatever their line breaks: WorkBuddy's prompt hook takes the newlines
    out of the person's words, and its record keeps them."""
    return hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()


def derived_turn(session_id: str, text: str, moment: str) -> str:
    return "turn-" + hashlib.sha256("\x00".join((session_id, text, moment)).encode("utf-8")).hexdigest()[:32]


def _kept(config, session_id: str) -> dict[str, Any]:
    """A session's kept turns, oldest first, each ``[turn id, words or None]``, and the words of its last reply."""
    path = mark_path(config, "turns", session_id)
    try:
        if time.time() - path.stat().st_mtime >= _TURNS_SECONDS:
            return {"turns": [], "reply": None, "error": None}
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"turns": [], "reply": None, "error": None}
    if not isinstance(kept, dict):
        return {"turns": [], "reply": None, "error": None}
    turns = [
        list(item)
        for item in kept.get("turns") or ()
        if isinstance(item, list)
        and len(item) == 2
        and type(item[0]) is str
        and (item[1] is None or type(item[1]) is str)
    ]
    reply, error = kept.get("reply"), kept.get("error")
    return {
        "turns": turns[-_TURNS_KEPT:],
        "reply": reply if type(reply) is str else None,
        "error": error if type(error) is str else None,
    }


def _keep(config, session_id: str, kept: dict[str, Any]) -> None:
    """Write a session's turns; one that cannot be written leaves the next Stop to make its own."""
    path = mark_path(config, "turns", session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        pending.write_text(json.dumps(kept), encoding="utf-8")
        os.replace(pending, path)
        cutoff = time.time() - _TURNS_SECONDS
        for count, old in enumerate(path.parent.iterdir()):
            if count >= 256:
                break
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def forget_turns(config, session_id: str) -> None:
    try:
        mark_path(config, "turns", session_id).unlink(missing_ok=True)
    except OSError:
        pass


def open_turn(config, session_id: str, payload: dict[str, Any], words: str | None, moment: str) -> str:
    """The turn a WorkBuddy prompt opens, kept for its Stop; ``words`` are the person's, None for a notice."""
    kept = _kept(config, session_id)
    given, _gaps = turn_id_from_payload(payload, required=False, field=TURN_FIELD["workbuddy"])
    turn = (
        given
        if given is not None and all(given != known for known, _words_of in kept["turns"])
        else derived_turn(session_id, words or "", moment)
    )
    kept["turns"] = [*kept["turns"], [turn, words_of(words) if words and words.strip() else None]][-_TURNS_KEPT:]
    _keep(config, session_id, kept)
    return turn


def close_turn(config, session_id: str, payload: dict[str, Any], reply: str, moment: str) -> tuple[str, bool]:
    """The turn a WorkBuddy Stop closes: the last one a prompt opened, else its own (its ``generation_id``, or one
    made from the reply); and whether the reply is the one the session's last Stop had."""
    kept = _kept(config, session_id)
    if kept["turns"]:
        turn = kept["turns"][-1][0]
    else:
        given, _gaps = turn_id_from_payload(payload, required=False, field=TURN_FIELD["workbuddy"])
        turn = given or derived_turn(session_id, reply, moment)
    words = words_of(reply) if reply.strip() else None
    # The last reply, or the error WorkBuddy showed in place of one since: a stopped turn hands either.
    repeated = words is not None and words in (kept["reply"], kept["error"])
    if words is not None and not repeated:
        kept["reply"], kept["error"] = words, None
        _keep(config, session_id, kept)
    return turn, repeated


def note_error_reply(config, session_id: str, reply: str) -> None:
    """Keep the words of an error WorkBuddy showed in place of a reply, which a later stopped turn may hand its Stop."""
    kept = _kept(config, session_id)
    kept["error"] = words_of(reply)
    _keep(config, session_id, kept)


def kept_turn(config, session_id: str, text: str) -> str | None:
    """The latest kept turn of the session opened for these words."""
    words = words_of(text)
    return next((turn for turn, kept in reversed(_kept(config, session_id)["turns"]) if kept == words), None)


def replied_entry(said: list["transcript.Said"], closed: tuple[str, str] | None) -> tuple[str, str] | None:
    """The record id of the model's message with the words of the reply a Stop closed its turn with, among those after
    the person's last message of the read, and that turn."""
    if closed is not None:
        turn, words = closed
        for entry in reversed(said):
            if entry.role == "user":
                break
            if words_of(entry.text) == words:
                return entry.entry_id, turn
    return None


def record_turns(config, session_id: str, said: list["transcript.Said"]) -> dict[str, str]:
    """The kept turn of each of the person's record messages that has one, by record id: the latest messages take the
    latest turns of the same words, each turn one message."""
    open_turns = list(reversed(_kept(config, session_id)["turns"]))
    found: dict[str, str] = {}
    for entry in reversed(said):
        if entry.role != "user":
            continue
        words = words_of(entry.text)
        match = next((index for index, (_turn, kept) in enumerate(open_turns) if kept == words), None)
        if match is not None:
            found[entry.entry_id] = open_turns.pop(match)[0]
    return found
