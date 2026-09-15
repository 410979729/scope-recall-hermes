"""Keep one copy of each distinct body, and say how many were folded away.

Half of TianShu's visible corpus is exact duplicates: 31,442 sources carry only
15,773 distinct bodies, and a single tool summary appears 1,140 times, because
a legacy import re-delivered the same content under fresh identities.  The
retrieval pipeline already de-duplicates -- but on ``(kind, ref, revision)``,
which is *identity*, not content -- so N copies of one document held N packet
slots.

Measured against the live store: a question whose answer ranked 12th came back
as six items of which four were the same paragraph, and the answer was never
delivered at all.  Collapsing copies alone moved the judged probe set from 7/8
answered to 8/8, and removed every one of the eight duplicate slots it had been
spending.  It was also the *only* change that helped -- weighting the lexical
channel by inverse document frequency was measured alongside it and made both
recall and abstention worse, so the scoring was left alone.

Collapsing is not truncation.  The surviving copy is byte-identical to the ones
removed, so no content is lost and coverage is not reduced; that is why this
gap has its own prefix rather than reusing ``coverage_truncated``.  It is still
recorded, because "six items from six sources" and "six items from three" are
different facts about the evidence, and a reader counting independent support
must not be misled by repetition.

A duplicate is *two different objects carrying the same body*.  Two revisions
of one object are not duplicates however identical their text: an episode whose
status changed but whose wording did not is still two facts, and ``history``
and ``as_of`` exist precisely to show those side by side.  So a collision is
folded only when the colliding items have different refs.

Not responsible for: stopping duplicates at capture, or choosing which copy is
canonical.  The caller passes candidates in the order it wants them kept and
the first occurrence wins, so the ranking already made that decision.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

#: Gap emitted when copies were folded away.  Deliberately *not* the
#: ``coverage_truncated`` vocabulary: nothing was cut, so coverage is intact.
DUPLICATE_GAP_PREFIX = "duplicates_collapsed"


def content_key(obj: Any) -> tuple[str, str]:
    """Identity of *what an item says*, independent of which row said it.

    Kind is part of the key so a claim is never folded into an event that
    happens to quote it verbatim -- they carry different authority.
    """
    kind = getattr(obj, "kind", "") or ""
    content = getattr(obj, "content", "") or ""
    if type(content) is not str:
        content = str(content)
    body = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    return str(kind), body


def duplicate_gap(collapsed: int) -> str | None:
    """One gap naming how many copies were folded, or ``None`` for none."""
    if type(collapsed) is not int or type(collapsed) is bool or collapsed <= 0:
        return None
    return f"{DUPLICATE_GAP_PREFIX}:{collapsed}"


def note_duplicates(gaps: list[str] | None, collapsed: int) -> None:
    """Append the collapse gap when there was one.  Accepts ``None`` for
    callers with no gap list, so a read path never branches around this."""
    if gaps is None:
        return
    gap = duplicate_gap(collapsed)
    if gap is not None and gap not in gaps:
        gaps.append(gap)


def parse_duplicate_gap(gap: Any) -> int | None:
    """Read a collapse gap back into its count, or ``None`` if it is not one."""
    if type(gap) is not str or not gap.startswith(DUPLICATE_GAP_PREFIX + ":"):
        return None
    try:
        return int(gap.split(":", 1)[1])
    except (ValueError, TypeError):
        return None


class DistinctContent:
    """Admits the first copy of each body and counts the rest.

    Stateful because a packet is assembled in two passes -- query evidence
    first, then background -- and a background item that merely repeats the
    evidence wastes a slot exactly as any other duplicate does.  One instance
    spans both passes, so the packet as a whole never says the same thing
    twice.
    """

    __slots__ = ("_spoken_by", "collapsed")

    def __init__(self) -> None:
        # content key -> ref of the object that first said it
        self._spoken_by: dict[tuple[str, str], str] = {}
        self.collapsed = 0

    def admits(self, obj: Any) -> bool:
        """True for a body not yet said, or for another revision of the object
        that already said it; False only for a copy from a different object."""
        key = content_key(obj)
        ref = getattr(obj, "ref", "") or ""
        ref = ref if type(ref) is str else str(ref)
        spoken_by = self._spoken_by.get(key)
        if spoken_by is None:
            self._spoken_by[key] = ref
            return True
        if ref and spoken_by == ref:
            return True
        self.collapsed += 1
        return False

    def filtered(self, pairs: Iterable[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
        """Keep ``(candidate, object)`` pairs whose object is a first copy."""
        return [pair for pair in pairs if self.admits(pair[1])]


__all__ = [
    "DUPLICATE_GAP_PREFIX",
    "DistinctContent",
    "content_key",
    "duplicate_gap",
    "note_duplicates",
    "parse_duplicate_gap",
]
