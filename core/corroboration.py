"""When the same assertion, stated again by someone else, becomes enough.

Every gate in this system asks one question of one piece of evidence: does
*this* text prove the claim?  A source that falls short is refused, and a second
source saying exactly the same thing is dropped as a duplicate -- the identical
assertion returns ``Mutation(..., "duplicate", ...)`` and its evidence is never
recorded.  So the system has no way to get stronger by hearing something twice,
which is why 401 claims on one instance produced 36 active ones.

This is the missing dimension, and it is deliberately the *only* one being
added.  Nothing here loosens a single judgement about a single source.  What it
adds is the ordinary human standard: two people, or the same person on two
separate occasions, independently stating the same thing is better evidence
than either statement alone.

Five conditions, and all of them are narrow on purpose:

* **The refusal must be about strength, not about kind.**  "This one source did
  not establish it" is cured by another source.  "This is a question", "this is
  hypothetical", "this quotes somebody else", "the subject is not in the text"
  are *not* -- asking a question twice does not make it an assertion, and
  repeating a sentence does not put a missing subject into it.  Hence a short
  allowlist rather than "anything still proposed".
* **The corroborating source must be first-hand and human.**  Two copies of the
  same tool dump are one observation, not two, and a model summarising its own
  earlier summary is the echo chamber this project already refuses elsewhere.
* **It must be a separate occasion.**  Witnesses are counted per speaker per
  session, not per stored row -- otherwise somebody saying the same thing twice
  in one breath is two witnesses, which is weaker than what this promises.
* **It must actually be independent.**  A different source event, not the same
  one arriving twice through a replay.
* **Both statements must be assertions in their own right.**  The incoming one
  is qualified normally first; corroboration only ever combines two refusals of
  the *eligible* kind.

Not responsible for: judging either statement (``core/claims.qualify`` does
that), or writing anything (``core/mutate.apply_claim`` owns the version).
"""
from __future__ import annotations

#: Independent first-hand statements required before a refusal is overturned.
#: Two is the ordinary standard for "not just one person's word"; a higher bar
#: would be unreachable in practice, since the corpus shows most facts are
#: stated once or twice and never again.
CORROBORATION_THRESHOLD = 2

#: Refusals that mean "one source was not enough", and nothing else.
#:
#: Each is cured by an independent statement of the same assertion:
#: ``fact_entailment_unproved`` is "this text does not visibly assert it",
#: ``no_independent_authority`` and ``requires_human_source`` are "nobody with
#: standing said it".  Every other refusal in ``qualify`` is about what kind of
#: statement this is, or about faithfulness to the quote, and repetition cannot
#: change either.
CORROBORATION_ELIGIBLE_REASONS = frozenset({
    "fact_entailment_unproved",
    "no_independent_authority",
    "requires_human_source",
})

#: Reason recorded on the version that corroboration promotes, so a reader can
#: tell an assertion that was proved by its own text from one that was accepted
#: because it was independently repeated.
CORROBORATED_REASON = "corroborated_by_independent_sources"


def _span_keys(payload) -> set[tuple[str, int]]:
    spans = (payload or {}).get("evidence_spans") or ()
    return {
        (str(span.get("source_ref")), int(span.get("source_revision", 0)))
        for span in spans
        if isinstance(span, dict) and span.get("source_ref")
    }


def _first_hand(roots):
    """Complete, first-hand human statements among the cited roots.

    ``capture_state``/``capture_gaps`` are checked because a partially captured
    source is a fragment of a statement, and half a sentence is not a witness.
    """
    from .claims import effective_origin

    return [
        root for root in roots
        if effective_origin(root) == "human_direct"
        and root.capture_state == "complete"
        and not root.capture_gaps
    ]


def independent_first_hand_sources(roots) -> set[tuple[str, int]]:
    """Which *sources* are first-hand, for deciding whether an arrival is new."""
    return {(root.ref, root.revision) for root in _first_hand(roots)}


def witness_occasions(roots) -> set[tuple[str | None, str]]:
    """Which *occasions* those sources represent: one per speaker per session.

    This is the half that was missing.  Counting source events made "somebody
    said the same thing twice in one breath" look like two witnesses -- the
    module promised "two people, or the same person on two separate occasions"
    and the code only checked that the two rows differed.

    A different speaker in the same session is still two witnesses; the same
    speaker in two sessions is too.  The same speaker twice in one session is
    one.
    """
    from .claims import _principal_ref

    return {(_principal_ref(root), root.session_id) for root in _first_hand(roots)}


def corroboration_promotes(
    *,
    existing,
    existing_reason: str,
    incoming_reason: str,
    incoming_roots,
    existing_roots,
    threshold: int = CORROBORATION_THRESHOLD,
) -> bool:
    """Whether this repeat of an already-refused assertion now establishes it.

    ``existing`` is the stored version the incoming proposal duplicates.
    """
    if existing is None or existing.state != "proposed":
        return False
    if existing_reason not in CORROBORATION_ELIGIBLE_REASONS:
        return False
    if incoming_reason not in CORROBORATION_ELIGIBLE_REASONS:
        return False

    incoming = independent_first_hand_sources(incoming_roots)
    if not incoming:
        return False
    # Every source the stored claim already rests on, whether or not it was
    # first-hand: a tool observation cannot become a witness, but it can still
    # make an "independent" arrival not independent at all.
    already = independent_first_hand_sources(existing_roots) | _span_keys(existing.payload)
    if not incoming - already:
        return False
    # Counted by occasion, not by row: see ``witness_occasions``.
    occasions = witness_occasions(existing_roots) | witness_occasions(incoming_roots)
    return len(occasions) >= threshold


__all__ = [
    "CORROBORATED_REASON",
    "CORROBORATION_ELIGIBLE_REASONS",
    "CORROBORATION_THRESHOLD",
    "corroboration_promotes",
    "independent_first_hand_sources",
    "witness_occasions",
]
