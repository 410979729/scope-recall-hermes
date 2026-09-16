"""What makes this a different question, as opposed to more of the same answer.

A candidate is re-judged when its evidence set changes.  Keying that set on a
recency window (``ORDER BY observed_at DESC LIMIT 16``) meant every new tool
observation displaced an older one, so the fingerprint always differed and the
``UNIQUE(candidate, revision, fingerprint, rule)`` guard -- which exists to
stop a question being asked twice -- could never collide.  Nearly every
verdict bought that way was ``insufficient_evidence`` again.

The doubling *timer* this replaces was the wrong instrument: it made a
candidate wait out a clock even when it had just received exactly the
evidence that would settle it.  Nothing here limits how much work an instance
may do; it decides whether there is a *new question* to ask, and a candidate
holding unjudged new testimony is always asked immediately.

One thing makes a question new: **first-hand testimony changed** -- a person
said something this candidate had not heard.  That is what the qualification
gates are waiting for.  Accumulating non-first-hand support was tried as a
second trigger and, replayed over a live history, produced hundreds of model
calls and exactly one conclusion; a trigger that cannot answer the question is
not worth asking, which is a statement about yield, not a budget.

Not responsible for: choosing which evidence the model sees.  The selection
still sends the newest that fits; this only decides whether that selection is
a question already answered.
"""
from __future__ import annotations

import hashlib
import json

#: Origins that count as somebody testifying rather than the system observing
#: itself.  ``core/corroboration.py`` uses the same notion for promotion; kept
#: as one name here so the two cannot drift into disagreeing about what a
#: witness is.
FIRST_HAND_ORIGINS = frozenset({"human_direct"})


def is_first_hand(origin: object) -> bool:
    return origin in FIRST_HAND_ORIGINS


def question_digest(evidence: object) -> str:
    """Identity of the question this evidence set poses.

    ``evidence`` is an iterable of ``(source_ref, source_revision, origin)``.
    Equal digests mean "we already asked this and were told the answer"; a
    different digest means something changed that could change the verdict.
    """
    first_hand = sorted(
        f"{ref}@{int(revision)}"
        for ref, revision, origin in (evidence or ())
        if is_first_hand(origin)
    )
    payload = json.dumps({"first_hand": first_hand},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["FIRST_HAND_ORIGINS", "is_first_hand", "question_digest"]
