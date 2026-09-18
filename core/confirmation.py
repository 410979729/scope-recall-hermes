r"""Recognising a person explicitly adopting a claim the system would not assert.

Every gate in ``qualify`` protects against one thing: the *model* asserting
something the evidence does not support.  None of them is a reason to overrule
a person who reads a proposal and says "yes, that one, keep it".  Until now
there was no way for them to say it: ``revise`` targets claims that are already
active, ``propose_memory`` makes new proposals, and the 366 refused proposals on
one instance had no route forward at all.

This is the mirror of ``capture_correction``: model-free, human-only, and
recognised in ordinary conversation rather than behind a tool call, because
that is how people actually confirm things.

Because this is the one path that reaches ``active`` without passing a single
text gate, the recognition has to be narrow in three independent ways, and the
first version of it was narrow in only one.  A review found five ordinary
sentences that all read as adoption:

    记住 references/topic.md 这条是错的，不要用。
    上次你记住的那条 configuration 值是错的
    别把 topic.md 那条记住，它已经过时了
    remember this: the port was wrong
    我记住了，下次注意

The first one is the worst case in miniature: the person is *rejecting* the
claim by name, and the only thing the old rule looked for was the word 记住
somewhere in the message.  So:

* **A clause, not a message.**  The adoption marker and the name of the thing
  being adopted must sit in the same clause.  "记住昨天那件事，另外 topic.md
  那条删掉" is two instructions, and reading them as one is how the second gets
  inverted.
* **That clause must not be negative.**  Both kinds of negative count: polarity
  (``不要``, ``never``) via the same ``POLARITY`` the gates use, and evaluation
  (``是错的``, ``过时``, ``wrong``) via the vocabulary below.  "Remember this:
  the port was wrong" is a person telling you the opposite of what it looks
  like.
* **It must be an instruction, not a report.**  "我记住了" is the person saying
  *they* remembered.  It is not addressed to the memory at all.

The remaining safety property is unchanged and still does most of the work: the
clause must literally contain the claim's reference, subject, predicate or
value, and must match exactly one proposal.  A bare "对，记住" names nothing,
and this deliberately does *not* resolve it from conversational context --
guessing which of several proposals somebody meant is exactly the class of
inference this project refuses everywhere else.

Not responsible for: writing (``core/mutate.capture_confirmation`` owns the
version), or for what a host shows the person before they confirm.
"""
from __future__ import annotations

import re

from .source_qualification import CLAUSE_BREAK, POLARITY

#: Explicit adoption.  Deliberately narrow: these are phrases that name an act
#: of keeping something, not general agreement.  "好的" and "ok" are absent on
#: purpose -- they acknowledge a message, they do not adopt a claim.
CONFIRMATION = re.compile(
    r"记住|记下来|记录下来|存下来|保存这条|保留这条|确认这条|采信|"
    r"以后就(?:这样|按这个)|就按这个|没错，?(?:记|存)|"
    r"\b(?:remember\s+(?:this|that)|keep\s+(?:this|that)\s+(?:one|fact|in\s+memory)|"
    r"save\s+(?:this|that)|confirm\s+(?:this|that)|note\s+(?:this|that)\s+down)\b",
    re.I,
)

#: Phrases that look like confirmation but are not this person adopting it.
#: A reported, hypothetical or interrogative "remember" belongs to somebody
#: else's sentence, or to a question -- "should I remember this?" asks, it does
#: not adopt, so any question mark disqualifies the whole utterance.
NOT_CONFIRMATION = re.compile(
    r"不要记住|别记|不用记|先别记|不要保存|"
    r"如果|假如|假设|除非|要不要|是否|吗[。.!?？！]?$|[?？]|"
    r"他说|她说|他们说|客户说|同事说|引用|原文|举例|示例|"
    r"\b(?:do\s*not|don[’']t|never)\s+remember\b|\b(?:if|unless|suppose|whether)\b|"
    r"\b(?:he|she|they|someone|customer|colleague)\s+said\b|\bfor example\b",
    re.I,
)

#: Saying the claim is wrong, stale or unwanted.  Distinct from ``POLARITY``,
#: which catches grammatical negation: "这条是错的" negates nothing
#: grammatically and is the clearest possible rejection.  ``别`` lives here
#: rather than in the polarity set because it is a prohibitive imperative --
#: "别把…记住" -- and POLARITY does not list it.
NEGATIVE_EVALUATION = re.compile(
    r"错|不对|有误|失误|过时|作废|失效|无效|废弃|删掉|删除|去掉|撤回|撤销|别|"
    r"\b(?:wrong|incorrect|mistaken|outdated|obsolete|stale|invalid|"
    r"deprecated|delete|remove|discard|drop)\b",
    re.I,
)

#: The person reporting that *they* remembered.  Not addressed to the memory.
SELF_REPORT = re.compile(
    r"我记住了|我记下了|我记得了|我知道了|我已经记|"
    r"\bI\s+(?:have\s+)?(?:remember(?:ed)?|noted|got\s+it)\b",
    re.I,
)

#: Reason recorded on a version promoted this way, so a reader can always tell
#: an assertion the evidence proved from one a person vouched for.
CONFIRMED_REASON = "confirmed_by_user"


def adoption_clause(text: object) -> str | None:
    """The clause in which this person adopts something, or ``None``.

    Returning the clause rather than a boolean is the point: everything
    downstream -- which claim was named, whether the sentence was negative --
    has to be judged on the same span, and handing back the whole message is
    what let "记住 X 这条是错的" read as adoption of X.
    """
    if type(text) is not str or not text.strip():
        return None
    if NOT_CONFIRMATION.search(text) or SELF_REPORT.search(text):
        return None
    for clause in _clauses(text):
        if not CONFIRMATION.search(clause):
            continue
        if POLARITY.search(clause) or NEGATIVE_EVALUATION.search(clause):
            return None
        return clause
    return None


def _clauses(text: str) -> list[str]:
    """Split on the same clause boundaries the evidence gates use."""
    parts, start = [], 0
    for match in CLAUSE_BREAK.finditer(text):
        parts.append(text[start:match.start()])
        start = match.end()
    parts.append(text[start:])
    return [part for part in parts if part.strip()]


def is_confirmation(text: object) -> bool:
    """Whether this text is somebody explicitly adopting something."""
    return adoption_clause(text) is not None


def confirmation_targets(text: str, versions, *, bound_literal) -> tuple:
    """The proposals this adoption literally names, within its own clause.

    ``bound_literal`` is injected rather than imported so the identifier
    boundary rule stays defined in exactly one place
    (``core/source_qualification``), and so a caller cannot accidentally use a
    looser match here than the gates use everywhere else.
    """
    clause = adoption_clause(text)
    if clause is None:
        return ()
    matches = []
    for version in versions:
        payload = version.payload or {}
        fields = (version.ref, payload.get("subject"), payload.get("predicate"),
                  payload.get("value_text"))
        if any(field and bound_literal(clause, str(field)) for field in fields):
            matches.append(version)
    return tuple(matches)


__all__ = [
    "CONFIRMATION",
    "CONFIRMED_REASON",
    "NEGATIVE_EVALUATION",
    "NOT_CONFIRMATION",
    "SELF_REPORT",
    "adoption_clause",
    "confirmation_targets",
    "is_confirmation",
]
