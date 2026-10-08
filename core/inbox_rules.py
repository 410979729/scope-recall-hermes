"""The capture inbox's rules, which read no store: what a row's error code says (put off, given up, still replayed),
when a row put off is tried again, and how a delete knows a copy of a deleted message in a waiting row.
``capture_inbox`` writes and replays the rows; storage, the delete, the work queue, the scheduler and the doctor read
the rows by these rules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from .._version import __version__
from ..contracts import ContractError

#: What a release before 3.4.0rc10 wrote for every missing source, which names none.  One of its causes is gone: a
#: task whose episode was deleted refused every later capture of the task (``EpisodeStore.attach``).  Such a row is
#: replayed once more; a missing source is now written with its field (``_terminal_code``), so a failure after that
#: replay stays final.
LEGACY_SOURCE_MISSING = "SOURCE_MISSING"
#: Inbox rows a replay still stores, besides those never tried: a passing failure (``replay_inbox``), a row from
#: before the missing source was named, or a key another message already took (``resolve_conflicted_ingress``
#: stores it under a new key).  Any other code is terminal; the row stays for inspection only.
STILL_REPLAYED = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", LEGACY_SOURCE_MISSING, "VERSION_CONFLICT"})
#: The codes ``replay_inbox`` itself retries; a ``VERSION_CONFLICT`` row is ``resolve_conflicted_ingress``'s.
RETRIED = tuple(sorted(STILL_REPLAYED - {"VERSION_CONFLICT"}))


#: A row whose stored capture a replay could not check again is put off:
#: ``DEFERRED|<release>|<until>|<attempt>|<path>|<code>``.  A newer release's field in its context, a host whose check
#: fails for now, a secret screen that differs between releases: each can clear.  Given a final code at once, such a
#: row was never stored, and a Claude Code Stop that had counted it as waiting did not store the words either; left in
#: place, it stopped every row after it on every pass (reviews of 3.4.0rc10).  It is tried again after a minute,
#: doubling to an hour, by whichever release runs, and the tries are counted across releases: when its
#: ``DEFER_ATTEMPTS``-th try again fails the row is given up (``GAVE_UP|<release>|<failures>|<path>|<code>``), where
#: doctor and the patrol show it, and ``retry-failures --apply`` returns it to the replay once its cause is fixed.  ``path`` is the replay
#: that put it off: a key collision's new key (``rekey``) is retried only by ``resolve_conflicted_ingress``, since a
#: plain replay would only meet the collision again.
DEFERRED = "DEFERRED|"
GAVE_UP = "GAVE_UP|"
_PATHS = ("replay", "rekey")
DEFER_FIRST_SECONDS = 60.0
DEFER_MAX_SECONDS = 3600.0
#: About nineteen hours of tries.
DEFER_ATTEMPTS = 24
#: What a query takes for the rows ``replay_inbox`` may store: never tried, a code in ``RETRIED``, or put off.
REPLAY_CANDIDATES = (
    f"(last_error_code IS NULL OR last_error_code IN ({','.join('?' for _ in RETRIED)})"
    " OR last_error_code LIKE 'DEFERRED|%')"
)


def _kind(exc: BaseException) -> str:
    """What failed, for an operator: a contract error's code and field, or the exception's class."""
    if isinstance(exc, ContractError):
        text = f"{exc.code}:{exc.field}" if exc.field else exc.code
    else:
        text = type(exc).__name__
    return text.replace("|", "/")[:80]


def _parsed_deferral(code: object) -> tuple[datetime | None, int, str] | None:
    """``(until, attempt, path)`` of a ``DEFERRED`` code, or None for any other code.  A time that cannot be read as a
    UTC time is None: the row is due."""
    if type(code) is not str or not code.startswith(DEFERRED):
        return None
    parts = code.split("|", 5)
    if len(parts) != 6:
        return None, 0, "replay"
    _marker, _release, until, attempt, path, _kind_ = parts
    try:
        moment = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is not None and moment.tzinfo is None:
        moment = None
    return moment, int(attempt) if attempt.isdigit() else 0, path if path in _PATHS else "replay"


def deferral(previous: object, exc: BaseException, now: datetime, *, path: str) -> str:
    parsed = _parsed_deferral(previous)
    attempt = (parsed[1] if parsed is not None else 0) + 1
    if attempt > DEFER_ATTEMPTS:
        return f"{GAVE_UP}{__version__}|{attempt}|{path}|{_kind(exc)}"
    delay = min(DEFER_MAX_SECONDS, DEFER_FIRST_SECONDS * 2 ** (attempt - 1))
    until = (now + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{DEFERRED}{__version__}|{until}|{attempt}|{path}|{_kind(exc)}"


def deferred_until(code: object, now: datetime) -> datetime | None:
    """When a row put off is tried again, if that is still to come; None when it is due or not put off."""
    parsed = _parsed_deferral(code)
    if parsed is None or parsed[0] is None:
        return None
    until = parsed[0]
    # A time further off than any backoff was written while the clock ran ahead: due now.
    if until <= now or until - now > timedelta(seconds=2 * DEFER_MAX_SECONDS):
        return None
    return until


def deferred_path(code: object) -> str | None:
    """The replay a row was put off or given up by (``replay`` or ``rekey``); None for any other code."""
    parsed = _parsed_deferral(code)
    if parsed is not None:
        return parsed[2]
    if type(code) is str and code.startswith(GAVE_UP):
        parts = code.split("|", 4)
        return parts[3] if len(parts) == 5 and parts[3] in _PATHS else "replay"
    return None


def replayable(code: object, now: datetime) -> bool:
    """Whether a pass now stores a row with this code: never tried, a passing failure, a bare ``SOURCE_MISSING`` an
    older release left, or a row put off whose time has come.

    What wakes the worker, and all the doctor does not call blocked, is read from here: the wake had counted two of
    the three retried codes, so a row an older release left as ``SOURCE_MISSING`` waited for a pass something else
    started, and the doctor called it blocked.  A key collision (``VERSION_CONFLICT``) is taken by the next pass's
    ``resolve_conflicted_ingress``, which stores it under a new key, finds it final (``VERSION_CONFLICT:rekeyed``) or
    puts it off, so its wake is never in vain; counted blocked, a collision, and a given-up row returned to the rekey
    path, waited for a pass something else started (review of rc10)."""
    if code is None or code in RETRIED or code == "VERSION_CONFLICT":
        return True
    return type(code) is str and code.startswith(DEFERRED) and deferred_until(code, now) is None


def given_up(code: object) -> bool:
    """Whether a replay gave a row up (``GAVE_UP``): blocked until ``retry-failures --apply``."""
    return type(code) is str and code.startswith(GAVE_UP)


def waiting(code: object) -> bool:
    """Whether some pass will still store a row with this code: what a hook's record read counts as already said.  A
    row given up is not."""
    return code is None or code in STILL_REPLAYED or (type(code) is str and code.startswith(DEFERRED))


def taking_a_new_key(code: object) -> bool:
    """Whether a row is another message that took a stored one's key: a collision, or one the rekey path put off."""
    return code == "VERSION_CONFLICT" or deferred_path(code) == "rekey"


def without_whitespace(text: object) -> str:
    """``text`` with no whitespace at all: how a delete compares a message's text (``holds``)."""
    return "".join(str(text).split())


def letters_and_digits(text: object) -> str:
    """``text`` with its letters and digits alone, in one case: how a delete compares a near copy (``holds``)."""
    return _NOT_A_LETTER.sub("", str(text)).casefold()


_NOT_A_LETTER = re.compile(r"[\W_]+")
#: Characters, whitespace aside, from which a deleted message's text is its own: a row holding all of it is a copy,
#: whatever else it says.  A shorter one ("好", "ok") is found inside unrelated messages, and deleting it cancelled
#: every waiting row that held it (review of rc10).
DISTINCT_TEXT = 24
#: Letters and digits from which a row with the same ones and at most a tenth more is a copy though its punctuation or
#: case differ ("我要辞职了。").  Below that, different messages compare the same ("C++" and "C#", "+1" and "-1").
NEAR_COPY = 4


def deleted_text(text: object) -> tuple[str, str]:
    """A deleted message's text as ``holds`` compares it: without whitespace, and its letters and digits."""
    bare = without_whitespace(text)
    return bare, letters_and_digits(bare)


def deleted_forms(text: object) -> frozenset[str]:
    """What a purge keeps of a deleted message's words to know a copy by once they are gone: digests of them without
    whitespace, and of their letters and digits when there are ``NEAR_COPY`` or more of them.  The same words spaced,
    cased or punctuated otherwise have the same forms; words added or taken away do not (review of rc13)."""
    bare, letters = deleted_text(text)
    forms = {"bare:" + hashlib.sha256(bare.encode("utf-8")).hexdigest()} if bare else set()
    if len(letters) >= NEAR_COPY:
        forms.add("letters:" + hashlib.sha256(letters.encode("utf-8")).hexdigest())
    return frozenset(forms)


def holds(
    payload_json: object,
    digests: frozenset[str],
    groups: frozenset[str],
    texts: frozenset[tuple[str, str]] = frozenset(),
    *,
    rekeyed: bool = False,
    versions: frozenset[tuple[str, int]] = frozenset(),
) -> bool:
    """Whether an inbox row's capture holds a deleted message.  A row that cannot be read is taken to (a delete then
    cancels it, as it cancels every row it cannot look into).  It holds one when:

    - one of its segments is a deleted one as stored (``content_sha256``), or it is of the deleted source's group
      (not for a row being given a new key, ``rekeyed``: another message that took a stored one's key, whose group
      is not its own; deleting the first message cancelled the second).  Of a deleted version (``versions``, its
      group and revision), only a part sent without the message's first is: a whole message there is a copy by its
      words or another message under the same key, as storage tells them (``storage.refuse_under_a_deleted_key``).
      Codex sends a message into a running turn under the turn's key, and a message still waiting there when
      another one under the key was deleted was cancelled with it (review of 3.4.6);
    - whitespace aside, it holds all of a deleted text of ``DISTINCT_TEXT`` characters or more, or is one with at most
      a tenth more; or, letters and digits compared, it is a deleted text of ``NEAR_COPY`` or more of them with at most
      a tenth more.  A message that quotes a short deleted one among other words, only part of a long one, or a long
      one written otherwise than whitespace (a quote reformatted, its punctuation changed), is kept.

    Compared by digest alone, the same words with a line break or a full stop more were kept and stored after the
    delete; compared more loosely, deleting a short message cancelled unrelated rows, and a character-by-character
    normalisation of every waiting row held the writer lease for seconds (reviews of rc10)."""
    try:
        return holds_events(
            json.loads(payload_json)["events"], digests, groups, texts, rekeyed=rekeyed, versions=versions
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return True


def holds_events(
    events,
    digests: frozenset[str],
    groups: frozenset[str],
    texts: frozenset[tuple[str, str]] = frozenset(),
    *,
    rekeyed: bool = False,
    versions: frozenset[tuple[str, int]] = frozenset(),
) -> bool:
    """``holds`` for a capture's events already read: the parts of one message, or one of them.  Storage asks it of a
    message under a deleted message's key (``storage.put_source``)."""
    from .events import stored_content_digest

    indexes = {event["segment"].get("index") for event in events if isinstance(event.get("segment"), dict)}
    for event in events:
        segment = event.get("segment")
        group = segment.get("group_key") if isinstance(segment, dict) else event.get("source_event_key")
        of_group = group in groups and (
            (group, event.get("source_revision")) not in versions or bool(indexes and 0 not in indexes)
        )
        if (not rekeyed and of_group) or stored_content_digest(event["content"]) in digests:
            return True
    texts = [text for text in texts if text[0]]
    if not texts:
        return False
    ordered = sorted(events, key=lambda event: (event.get("segment") or {}).get("index", 0))
    bare = without_whitespace("".join(event["content"] for event in ordered))
    letters = None
    for text, text_letters in texts:
        if (len(text) >= DISTINCT_TEXT or len(bare) - len(text) <= len(bare) // 10) and text in bare:
            return True
        if len(text_letters) >= NEAR_COPY:
            letters = letters_and_digits(bare) if letters is None else letters
            if len(letters) - len(text_letters) <= len(letters) // 10 and text_letters in letters:
                return True
    return False


#: Marker spliced into a re-keyed capture's source_event_key. Self-documenting on
#: purpose: the stored key keeps the host's original and appends the content
#: fingerprint, so the collision stays visible to anyone reading the row rather
#: than being filed in a side table nobody queries.
REKEY_MARKER = "#rekey:"
