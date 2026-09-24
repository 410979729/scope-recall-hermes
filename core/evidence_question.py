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

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable, Mapping
import unicodedata

#: Origins that count as somebody testifying rather than the system observing
#: itself.  ``core/corroboration.py`` uses the same notion for promotion; kept
#: as one name here so the two cannot drift into disagreeing about what a
#: witness is.
FIRST_HAND_ORIGINS = frozenset({"human_direct"})

#: The sources a claim may be derived from.  Consolidation shows the model only these
#: (``worker_consolidation``); the evaluator asks nothing of a candidate none of them speaks to
#: (``rootless``) and writes no version their words do not carry (``rooted_verdict``).  Tool
#: output is not one.  A tool output is what an agent read or ran while working, and derived
#: claims from it were almost all file sizes, paths, ports and timestamps: one 2.5-hour task on
#: the pilot left 787 of them, 93% of the store's 3,175 claims rested on tool output alone, and
#: not one of the owner's 30 real questions was answered by one.  The host distils how a task was
#: done into skills; tool output stays a searchable source.
#: ``requalify.retire_rootless_proposals`` retires the unconfirmed claims derived before this changed.
DERIVATION_ROOT_ORIGINS = frozenset({"human_direct", "external_document", "imported"})
#: Why a candidate nothing in ``DERIVATION_ROOT_ORIGINS`` speaks to is set aside.
NO_DERIVATION_ROOT_REASON = "no_derivation_root"


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


# --- questions no answer could settle ----------------------------------------
#
# A new question is still not always a question worth a model call.  On one instance
# (2026-09-17) 2,019 evaluations in one day promoted 7 facts, and 54% of them
# carried evidence on which no verdict could pass ``claims.qualify``: the
# candidate's value appeared in none of the supplied sources, or no supplied
# source could lend the authority its kind needs.  Of all 10,602 evaluations the
# instance had run, none of the 27 that promoted a fact was among them.
#
# Each rule below is a necessary condition of ``claims.qualify``, checked no
# more strictly than qualification checks it, so it only skips calls whose
# verdict was already decided:
#
# * authority -- ``_cite`` keeps complete, gap-free roots and ``_authority``
#   needs a human, tool or document one (a human one for ``_HUMAN_ONLY_KINDS``);
# * value -- ``_value_preserved`` needs ``value_text`` inside the quotes for every
#   kind but procedure, intention and alias, and a quote is an exact slice of a
#   supplied source.  Compared here on letters and digits only, after NFKC and
#   casefolding, so punctuation, spacing and dotted dates cannot hide a match.
#
# Not covered: a verdict that changes the candidate's value.  The re-evaluation
# keeps kind, subject and predicate but not the value; a new value reaches memory
# through its own source's consolidation, and none of the 27 promotions changed it.

#: The claim kinds the rules below know; anything else is left to the model.
_CLAIM_KINDS = frozenset({"fact", "preference", "constraint", "decision", "procedure", "intention", "alias"})


@dataclass(frozen=True)
class EvidenceText:
    """What the promotion rules read from one supplied source."""

    origin: str
    complete: bool
    content: str


def evidence_text(source) -> EvidenceText:
    """The rule-relevant view of a stored source, imports resolved as ``claims.effective_origin`` does."""
    origin = source.event["origin"]
    if origin == "imported":
        original = source.event.get("source_original_origin")
        origin = (original or "origin_unknown") if source.import_provenance_sha256 is not None else "origin_unknown"
    complete = source.event.get("capture_state") == "complete" and not source.capture_gaps
    return EvidenceText(origin, complete, str(source.event.get("content") or ""))


def _letters_and_digits(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def unanswerable_reason(payload: Mapping, evidence: Iterable[EvidenceText]) -> str | None:
    """Why no verdict on ``evidence`` could promote this candidate, or ``None``."""
    # The qualification rules own these sets; importing them keeps the two in step.
    from .claims import _AUTHORITY_ORIGINS, _HUMAN_ONLY_KINDS, _VALUE_FREE_KINDS

    kind = payload.get("kind") if isinstance(payload, Mapping) else None
    if kind not in _CLAIM_KINDS:
        return None
    items = tuple(evidence)
    needed = ("human_direct",) if kind in _HUMAN_ONLY_KINDS else _AUTHORITY_ORIGINS
    if not any(item.complete and item.origin in needed for item in items):
        return "no_authoritative_evidence"
    value = _letters_and_digits(payload.get("value_text"))
    if kind in _VALUE_FREE_KINDS or not value:
        return None
    if not any(value in _letters_and_digits(item.content) for item in items):
        return "value_not_in_evidence"
    return None


# --- claims rest on what a person or a document said -------------------------
#
# Since 3.2.0 tool output is no derivation root: consolidation never shows it to
# the model.  The evaluator is the other automatic writer, and the two rules below
# hold it to the same line.  ``claims.qualify`` alone would not: it lends a tool
# output authority and reads a value from any quote it cites, so a verdict quoting
# a tool output's value beside any fragment of a person's message was written as
# that person's report.

def rootless(cited_origins: Iterable[str], evidence: Iterable[EvidenceText]) -> str | None:
    """``NO_DERIVATION_ROOT_REASON`` for a candidate nothing a claim may be derived from speaks to.

    Neither the sources it cites (imports resolved) nor its evidence hold a person's or a
    document's words: a proposal an earlier release derived from tool output alone, which no
    verdict may now confirm.  It waits for a person, without a model call.  A candidate whose
    own words are cited is never set aside here, whatever its evidence window holds.
    """
    origins = set(cited_origins) | {item.origin for item in evidence}
    return None if origins & DERIVATION_ROOT_ORIGINS else NO_DERIVATION_ROOT_REASON


def rooted_verdict(proposal: Mapping, quoted: Iterable[tuple[EvidenceText, str]]) -> bool:
    """Whether a verdict quoting ``quoted`` -- (source, quote) pairs -- may write a version.

    It must quote a complete root, and what the claim says must be in the root's words: the
    value, inside a quote from a root and bound as ``qualify`` binds a literal (``80`` is not in
    ``8080``), or every step of a procedure, in a root it quotes -- and a procedure names at
    least one.  An agent's echo of a tool output's value, or a person's fragment beside it,
    carries nothing.  Kinds proved without a value (intention, alias) need the root quote
    alone; ``qualify`` already asks a person of them.
    """
    from .claims import _VALUE_FREE_KINDS
    from .source_qualification import bound_literal

    roots = [(text, quote) for text, quote in quoted if text.complete and text.origin in DERIVATION_ROOT_ORIGINS]
    if not roots:
        return False
    kind = proposal.get("kind") if isinstance(proposal, Mapping) else None
    if kind == "procedure":
        method = (proposal.get("procedure") or {}).get("method") or ()
        return bool(method) and all(any(step in text.content for text, _quote in roots) for step in method)
    value = str(proposal.get("value_text") or "")
    if kind in _VALUE_FREE_KINDS or not value.strip():
        return True
    return any(bound_literal(quote, value) for _text, quote in roots)


# --- questions worth asking at most so often ---------------------------------
#
# Even an answerable question is not worth asking again and again.  Replayed over
# one instance's 10,650 evaluations (2026-09-13..17), the candidate loop cost 81-97% of
# every day's model tokens, 60-96% of each day's evaluations re-asked a candidate
# already judged, and 307 candidates were asked ten times or more.  Of the 27
# verdicts that promoted a fact, 19 came from a candidate's first verdict, 4 from
# its second, and 4 from the fifth or later.  Two rules follow:
#
# * a kind only a person can establish (``claims._HUMAN_ONLY_KINDS``) that was
#   proposed from sources where no person spoke is not a candidate at all: 2,034
#   of those evaluations promoted nothing, and when the person does say it, the
#   consolidation of their own words proposes it with the authority it needs;
# * a candidate gets ``AUTOMATIC_VERDICTS`` model verdicts; after that it is asked
#   again only when a source that arrived since its last verdict restates it.
#   Replayed, this keeps 24 of the 27 promotions and 20% of the model calls.

#: Origins whose sources never carry a person's own words.
IMPERSONAL_ORIGINS = frozenset({"tool_observation", "external_document"})

#: Model verdicts a candidate receives before only a restatement can reopen it.
AUTOMATIC_VERDICTS = 2

#: The recorded reasons for the two rules, as they appear on lifecycle and evaluation rows.
PERSON_ABSENT_REASON = "person_kind_without_person"
REPEAT_WITHOUT_RESTATEMENT_REASON = "repeat_without_restatement"


def needs_absent_person(payload: Mapping, cited_origins: Iterable[str]) -> bool:
    """A kind only a person can establish, proposed where no person spoke.

    ``cited_origins`` are the effective origins of the sources the proposal
    cites.  An empty or unknown origin set is never judged absent.
    """
    from .claims import _HUMAN_ONLY_KINDS

    kind = payload.get("kind") if isinstance(payload, Mapping) else None
    origins = frozenset(cited_origins)
    return kind in _HUMAN_ONLY_KINDS and bool(origins) and origins <= IMPERSONAL_ORIGINS


def restatement_needle(payload: Mapping) -> str:
    """What a later source must contain to restate this candidate, compared on letters and digits.

    The value for kinds whose promotion quotes it; the subject for the kinds
    proved otherwise.  Empty when neither has a letter or digit.
    """
    from .claims import _VALUE_FREE_KINDS

    if not isinstance(payload, Mapping):
        return ""
    value = _letters_and_digits(payload.get("value_text"))
    if payload.get("kind") in _VALUE_FREE_KINDS or not value:
        return _letters_and_digits(payload.get("subject"))
    return value


def restates(payload: Mapping, contents: Iterable[str]) -> bool:
    """Whether any of ``contents`` restates the candidate; unknown needles count as restated."""
    needle = restatement_needle(payload)
    if not needle:
        return True
    return any(needle in _letters_and_digits(content) for content in contents)


__all__ = ["AUTOMATIC_VERDICTS", "DERIVATION_ROOT_ORIGINS", "FIRST_HAND_ORIGINS", "IMPERSONAL_ORIGINS",
           "NO_DERIVATION_ROOT_REASON", "PERSON_ABSENT_REASON", "rooted_verdict", "rootless",
           "REPEAT_WITHOUT_RESTATEMENT_REASON", "EvidenceText", "evidence_text", "is_first_hand",
           "needs_absent_person", "question_digest", "restatement_needle", "restates", "unanswerable_reason"]
