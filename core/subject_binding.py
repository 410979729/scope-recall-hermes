r"""Where a claim's subject is allowed to be found, verbatim, in its evidence.

The gate requires a subject to appear literally in what was quoted, which is
what stops a model from inventing one.  Measured on tianshu's 92 live
``subject_not_bound`` head versions, that is refusing three different things at
once:

    37  the subject appears nowhere in the source at all -- "memory
        configuration", "memory provider", "skills directory".  Invented; these
        must keep failing, and they do.
    48  the subject appears verbatim in the source, but in a neighbouring
        sentence rather than inside the quoted span.
     7  other (present but not bound by the identifier rule, or no subject).

The 48 are the mismatch.  A source says "此测试项目的名称为 SRLIVE-… 。… 不要写
业务文件"; the constraint quotes the directive clause, while its subject was
established a sentence earlier in the same message.  Nothing was inferred and
nothing was invented -- the evidence simply spans two sentences, and the check
looked at one.

Widening all the way to "anywhere in the source" would be wrong, and the same
measurement says so.  Sorted by distance between the subject and its quote, the
48 split cleanly:

    <= 246 chars   39 cases   "Kimi K3" / its spec line, "windows-ocr.ps1" /
                              its description, "此测试项目" / "唯一标识为 MEM-…"
    >= 524 chars    9 cases   "you", "the agent", "The user", "skill update" --
                              all pronouns lifted out of one 10 KB instruction
                              document and pinned to unrelated quotes in it

So the second rung is one sentence of context, not a whole document: the
quote's own sentence plus the one immediately before it.  A sentence is the
unit ``evidence_context`` already uses, and it is the unit in which a subject
and its predicate are actually stated together.

The second half is a guard on what may be a subject at all.  "you", "the
agent", "the user" are not entities, they are pointers, and in a long
instruction document they occur often enough to land inside almost any window.
Neither is a clause: replaying the corpus turned up one subject reading "If the
session ran smoothly with no corrections and produced no new technique", which
sat verbatim right beside its quote and would have passed on distance alone.
Nothing can ever be recalled *about* either of those, so both are refused at
any distance.

Monotone, like ``evidence_quote``: rung one is exactly today's rule, so nothing
that passes now can begin to fail, and rung two returns a real slice of the
stored source rather than anything derived.

Replaying ``qualify`` over all 92 on a shadow of the live database: 41 now get
past the subject gate, 51 still do not, and 2 reach ``active``.  The other 39
stop at the next gate -- 27 of them at ``fact_entailment_unproved``.  That is
the real bottleneck, and it is a different repair.
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
DEICTIC_SUBJECTS = frozenset({
    "you", "your", "yours", "yourself",
    "the agent", "this agent", "the assistant", "the model",
    "user", "current_user", "the user", "a user", "this user",
    "the caller", "the operator",
    "he", "she", "they", "it", "we", "us", "one", "someone", "anyone",
    "你", "您", "你们", "用户", "该用户", "这个用户", "助手", "该助手", "本助手",
    "代理", "该代理", "对方", "他", "她", "它", "他们", "她们", "我们", "大家",
})


#: A subject that opens with a subordinator is a clause, not a thing.  The live
#: corpus produced exactly one: "If the session ran smoothly with no corrections
#: and produced no new technique" -- verbatim and adjacent to its quote, so the
#: distance rule alone would admit it, but nothing can ever be recalled *about*
#: it and it would only add noise to the entity space.
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

    ``"quote"`` is today's rule.  ``"neighbourhood"`` is the widened one, and is
    only ever reached for a subject that names something.
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
    Kept separate from rung one so the caller's first rung stays literally the
    rule it has today -- the widened rule can only ever be reached as an
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
