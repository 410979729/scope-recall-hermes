"""Where a claim's subject may appear verbatim in its evidence.

The first rung applies the literal identifier rule to the quote.  The second
inspects source windows starting one sentence before each quote occurrence and
ending at the first sentence terminator after the quote, or at the end of the
source.  If the quote already includes its sentence terminator, this also
includes the following sentence.

Only the second rung requires a subject that names an entity: bare pointers
such as "you" and "the user", and subjects beginning with a subordinator,
are refused there.  Passing the first rung remains sufficient.

Like ``evidence_quote``, the check is monotone: a subject bound by the literal
rule continues to pass.  The second rung inspects source sentences rather than
inferred or generated text.
"""

from __future__ import annotations

import re

from .source_qualification import bound_literal

#: Sentence terminators, matching ``claims.evidence_context``.
_SENTENCE_END = re.compile(r"[。！？!?;；\n]")
#: Occurrences of one quote to look at, matching ``claims.evidence_context``.
#: A quote repeated more often than this is pathological input rather than
#: evidence, and an unbounded scan of a 200 KB tool envelope is its own outage.
_MAX_OCCURRENCES = 128

#: Subjects that point rather than name.  Deliberately only bare pronouns and
#: their bare determiner forms: "the user's laptop" names a thing and is left to
#: the ordinary rules, while "the user" names whoever happens to be reading.
#: First person is excluded on purpose -- ``claims.bind_self_subject`` already
#: has a stricter, evidence-bound path for it.
DEICTIC_SUBJECTS = frozenset(
    {
        "you",
        "your",
        "yours",
        "yourself",
        "the agent",
        "this agent",
        "the assistant",
        "the model",
        "user",
        "current_user",
        "the user",
        "a user",
        "this user",
        "the caller",
        "the operator",
        "he",
        "she",
        "they",
        "it",
        "we",
        "us",
        "one",
        "someone",
        "anyone",
        "你",
        "您",
        "你们",
        "用户",
        "该用户",
        "这个用户",
        "助手",
        "该助手",
        "本助手",
        "代理",
        "该代理",
        "对方",
        "他",
        "她",
        "它",
        "他们",
        "她们",
        "我们",
        "大家",
    }
)


#: A subject beginning with a subordinator is a clause rather than an entity, even when it is adjacent to its quote.
_SUBORDINATOR = re.compile(
    r"^\s*(?:if|when|whenever|unless|while|because|since|although|though|after|before|"
    r"in\s+case|as\s+long\s+as|provided\s+that)\b|^\s*(?:如果|若|倘若|假如|除非|当|每当|因为|由于|虽然|尽管)",
    re.I,
)


def names_a_thing(subject: object) -> bool:
    """False when the subject points at a participant or opens a clause.

    Both are refused at any distance: a pointer resolves to whoever happens to
    be reading, and a clause has no identity to recall anything about.
    """
    if type(subject) is not str or not subject.strip():
        return False
    return not is_deictic(subject) and not _SUBORDINATOR.search(subject)


def is_deictic(subject: object) -> bool:
    """True when the subject points at a participant instead of naming a thing."""
    if type(subject) is not str:
        return False
    return subject.strip().strip("的 ").casefold() in DEICTIC_SUBJECTS


def quote_neighbourhood(content: object, quote: object) -> str:
    """The quote's own sentence plus the one immediately before it.

    Returns an empty string when the quote is not in the content, so a caller
    can never widen a check onto text the quote does not actually come from.
    """
    if type(content) is not str or type(quote) is not str or not quote:
        return ""
    parts: list[str] = []
    start = 0
    for _ in range(_MAX_OCCURRENCES):
        at = content.find(quote, start)
        if at < 0:
            break
        breaks = list(_SENTENCE_END.finditer(content, 0, at))
        # One sentence back: the second-to-last terminator before the quote.
        left = breaks[-2].end() if len(breaks) >= 2 else 0
        end = at + len(quote)
        following = _SENTENCE_END.search(content, end)
        right = following.end() if following else len(content)
        parts.append(content[left:right])
        start = at + max(1, len(quote))
    return "\n".join(dict.fromkeys(parts))


def subject_binding(subject: object, *, quote: object, content: object) -> str | None:
    """Which rung binds this subject, or ``None`` if none does.

    ``"quote"`` is the literal rule.  ``"neighbourhood"`` is the widened one, and
    is only ever reached for a subject that names something.
    """
    if type(subject) is not str or not subject:
        return None
    if type(quote) is str and quote and bound_literal(quote, subject):
        return "quote"
    if not names_a_thing(subject):
        return None
    neighbourhood = quote_neighbourhood(content, quote)
    if neighbourhood and bound_literal(neighbourhood, subject):
        return "neighbourhood"
    return None


def neighbourhood_binds(subject: object, pairs) -> bool:
    """True when the second rung alone binds the subject for some cited span.

    ``pairs`` yields ``(quote, stored_content)`` for the spans a proposal cites.
    Kept separate from rung one so the caller's first rung stays exactly the
    literal rule -- the widened rule can only ever be reached as an
    additional ``or``, never as a replacement.
    """
    for quote, content in pairs:
        if subject_binding(subject, quote=quote, content=content) == "neighbourhood":
            return True
    return False


__all__ = [
    "DEICTIC_SUBJECTS",
    "is_deictic",
    "names_a_thing",
    "neighbourhood_binds",
    "quote_neighbourhood",
    "subject_binding",
]
