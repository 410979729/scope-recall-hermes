"""What a hook keeps beside an entry between its calls, by session: WorkBuddy's turns, and the threads Codex opened
to ask the model for suggestions.  Each hook is a process of its own, so what one call learns reaches the next only
through these small files; each is disposable, and losing one costs a match by words and moment, never a capture."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

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
        Path(config.home) / "scope-recall" if isinstance(config, SharedClientConfig) else Path(config.data_directory)
    ) / kind
    return folder / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


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
# disposable: without it a Stop makes a turn of its own, and the record is matched by words and moment as before.

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
