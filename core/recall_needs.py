"""Evidence slots a query leaves open, the one directed follow-up they justify, and the items that answer it.

A comparison needs both named sides, a "why" needs a reason clause that names
the target, a resume needs a grounded open episode.  A question that names
something is judged: only an item that names it too and says more of what the
question asks answers it (``answering``); one that names nothing is not.  These
are read off the query and the hydrated items; nothing here searches, and a need
never manufactures evidence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .claims import is_denial
from .corroboration import CORROBORATION_ELIGIBLE_REASONS
from .events import lexical_terms, version_suffixes
from .fact_evidence import stem
from .recall_policy import (
    QUESTION_MAX_CHARS,
    asked_terms,
    asks_without_answering,
    hard_identifiers,
    meaningful_query_terms,
)
from .retrieval import STALE_RESUME_GAPS, RetrievedObject, SearchContext, optional_json
from .source_qualification import FENCE, UNASSERTED_CONTEXT, unquoted

COMPARE_MARKERS = ("比较", "对比", "差异", "哪个更", "compare", "versus", " vs ")
WHY_MARKERS = ("为什么", "为何", "原因", "why", "reason")
_RESUME_QUERY_TERMS = frozenset({"继续", "接着", "恢复", "resume", "continue", "pick", "up"})
#: A task request: an imperative, a suggestion or a question, at the start of a prompt or after a sentence break, with
#: a short acknowledgement allowed before it (OK, So, 好的).  请你 and 可以/能否 frame one; 继续吧, 继续上次 and
#: 恢复上次 name no further task.  A subject stating what will continue, a negation, and 继续 plus any other verb match
#: none.
_RESUME_LEAD = r"(?:ok|okay|sure|yes|yeah|right|so|well|now|好的?|行|嗯)"
_RESUME_REQUEST = re.compile(
    r"(?:^|[\n。！？!?.])\s*"
    rf"(?:{_RESUME_LEAD}\b\s*[,，:：]?\s*)?"
    r"(?:please\s+)?(?:let['’]s\s+|(?:can|could|would|shall)\s+(?:we|you|i)\s+)?(?:please\s+)?"
    r"(?:(?:continue|resume|pick\s+up)\s+(?!using\b)"
    r"(?:(?:with|on|working\s+on)\s+\S|(?:the|our|my|this|that|where)\b|(?-i:[A-Z])[\w.-]*)"
    r"|where\s+(?:were\s+we|did\s+(?:we|i)\s+leave\s+off)\b"
    r"|(?:continue|resume)\W*(?:please\W*)?$)"
    r"|(?:^|[\n。！？!?.])\s*"
    rf"(?:{_RESUME_LEAD}\b\s*[,，:：]?\s*)?"
    r"(?:(?:(?:请|麻烦)(?:你|帮我)?|帮我|请帮我)\s*)?"
    r"(?:(?:我们|咱们|你)?(?:可以|能否|能不能|可否)\s*)?"
    r"(?:继续\s*(?:[吧呀啊]\s*[。.!！?？]*(?:$|[,，])|[。.!！?？\s]*$"
    r"|(?:做|搞|干|弄|写|改)\s*\S|(?:上次|之前|刚才|昨天|当前)\S?"
    r"|(?:任务|工作|迁移|部署|项目)|[A-Za-z0-9_.-]+)"
    r"|接着(?:做|干|来|弄|写|改|上次|刚才|之前)"
    r"|恢复\s*(?:(?:上次|之前|刚才|昨天)\S?|(?:[\w.-]+\s*的?)?(?:任务|工作|迁移|部署)))",
    re.I,
)
CHOICE_MARKERS = ("哪个", "哪一个", "which", "choose")
_CAUSAL = re.compile(r"因为|由于|原因是|because|reason is|due to", re.I)
_NEGATED = re.compile(
    r"未知|不明|不清楚|未(?:知|记录|说明)|没有证据|无证据|不能确定|无法确定|no evidence|unknown|unclear", re.I
)
_CLAUSE_END = re.compile(r"[。！？!?;；\n]")
_REASON_PREDICATES = frozenset({"原因", "理由", "reason", "why", "rationale"})
_GROUNDED_NEXT_STEP_BASES = frozenset({"user_requested", "tool_observation", "observed", "direct_report", "evidence"})


def mentions(query: str, markers: tuple[str, ...]) -> bool:
    text = query.casefold()
    return any(marker in text for marker in markers)


def asks_to_resume(query: str) -> bool:
    """Whether ``query`` asks to resume a task (``_RESUME_REQUEST``), which its grounded resume answers.  The resume
    need and its follow-up, the order of a resume packet, its background task and what answers it read a query this
    one way."""
    return _RESUME_REQUEST.search(unicodedata.normalize("NFKC", query)) is not None


def evidence_roots(item: object) -> frozenset[str]:
    """Source refs (without revision) an item ultimately rests on."""
    roots: set[str] = set()
    if getattr(item, "kind", "") == "event":
        roots.add(str(getattr(item, "ref", "")))
    for ref in getattr(item, "evidence_refs", ()) or ():
        if type(ref) is str and "@" in ref:
            roots.add(ref.split("@", 1)[0])
    return frozenset(roots)


def _claim_payload(item: object) -> dict | None:
    payload = None
    for key, value in getattr(item, "metadata", ()):
        if key == "payload_json":
            payload = optional_json(value)
    return payload if isinstance(payload, dict) else None


# -- comparison ---------------------------------------------------------------


def _comparison_targets(query: str) -> frozenset[str]:
    """One identifier per compared object.

    A suffixed version yields its suffix beside its own identifier ("rc28" and
    "0rc28" for 3.1.0rc28).  Counted as a side of its own, two sources about
    one release would pass for both sides, so the suffix is dropped wherever
    the fuller identifier names the same object.
    """
    targets = hard_identifiers(query)
    aliases = {
        suffix
        for suffix in version_suffixes(query)
        if any(target != suffix and target.endswith(suffix) for target in targets)
    }
    return targets - aliases


def _comparison_sides(targets: frozenset[str], items: tuple[object, ...]) -> dict[str, frozenset[str]]:
    """Evidence roots that mention each compared identifier."""
    sides: dict[str, frozenset[str]] = {}
    for target in sorted(targets):
        roots: set[str] = set()
        for item in items:
            if target in hard_identifiers(getattr(item, "content", "")):
                roots.update(evidence_roots(item))
        if roots:
            sides[target] = frozenset(roots)
    return sides


def _comparison_unmet(query: str, items: tuple[object, ...]) -> bool:
    targets = _comparison_targets(query)
    if len(targets) < 2:
        return True
    sides = _comparison_sides(targets, items)
    if len(sides) < 2:
        return True
    # One source can explicitly cover both named objects.  Requiring two
    # independent roots would turn a direct shared statement into a missing side.
    if any(targets <= hard_identifiers(getattr(item, "content", "")) for item in items):
        return False
    return len({min(roots) for roots in sides.values()}) < 2


def _second_side_query(query: str, items: tuple[object, ...]) -> str | None:
    targets = _comparison_targets(query)
    if len(targets) < 2:
        return None
    sides = _comparison_sides(targets, items)
    missing = [target for target in sorted(targets) if target not in sides]
    return missing[0] if len(missing) == 1 else None


# -- why ----------------------------------------------------------------------


def _reason_window_supported(content: str, targets: frozenset[str], marker: re.Match[str]) -> bool:
    """Accept a causal phrase only when its clause names the requested target.

    The whole sentence/clause is used so a long qualifier cannot be clipped,
    while a semicolon-separated reason for another object does not become
    evidence for this target.
    """
    prior = [match.end() for match in _CLAUSE_END.finditer(content, 0, marker.start())]
    following = _CLAUSE_END.search(content, marker.end())
    window = content[prior[-1] if prior else 0 : following.start() if following else len(content)]
    if _NEGATED.search(window):
        return False
    return not targets or bool(targets.intersection(hard_identifiers(window)))


def _states_reason(item: object, content: str) -> bool:
    payload = _claim_payload(item)
    if payload is None or str(payload.get("predicate", "")).casefold() not in _REASON_PREDICATES:
        return False
    return not _NEGATED.search(content)


def _why_unmet(query: str, items: tuple[object, ...]) -> bool:
    topic_terms = set(meaningful_query_terms(query))
    if not topic_terms:
        return True
    targets = hard_identifiers(query)
    for item in items:
        content = getattr(item, "content", "")
        if not topic_terms.intersection(lexical_terms(content)):
            continue
        if _states_reason(item, content):
            return False
        if any(_reason_window_supported(content, targets, causal) for causal in _CAUSAL.finditer(content)):
            return False
    return True


def _reason_query(query: str, items: tuple[object, ...]) -> str | None:
    terms = meaningful_query_terms(query)
    return f"{' '.join(terms[:3])} 原因" if terms else None


# -- resume -------------------------------------------------------------------


def _grounded(entries: object) -> bool:
    return any(isinstance(entry, dict) and entry.get("text") and entry.get("evidence_refs") for entry in entries or ())


def _resumes(item: object) -> bool:
    """Whether an item is a grounded task's resume: an episode whose resume names its goal and holds progress, an open
    item or a next step on a basis that grounds it, each with the evidence it was drawn from, and that no gap marks
    stale (``STALE_RESUME_GAPS``)."""
    if getattr(item, "kind", "") != "episode":
        return False
    gaps = optional_json(dict(getattr(item, "metadata", ())).get("gaps")) or ()
    if any(gap in gaps for gap in STALE_RESUME_GAPS):
        return False
    resume = optional_json(getattr(item, "content", ""))
    if type(resume) is not dict:
        return False
    goal = resume.get("goal")
    if type(goal) is not dict or not goal.get("text") or not goal.get("evidence_refs"):
        return False
    basis = str(resume.get("next_step_basis") or "").casefold()
    grounded_next_step = bool(resume.get("next_step") and basis in _GROUNDED_NEXT_STEP_BASES)
    return _grounded(resume.get("verified_progress")) or _grounded(resume.get("open_items")) or grounded_next_step


def _resume_unmet(items: tuple[object, ...]) -> bool:
    return not any(_resumes(item) for item in items)


def _resume_query(query: str, items: tuple[object, ...]) -> str:
    # One directed pass only, within the original shared budgets.
    terms = tuple(term for term in meaningful_query_terms(query) if term.casefold() not in _RESUME_QUERY_TERMS)
    return f"{' '.join(terms[:3])} 未完成任务 下一步".strip()


# -- what answers -------------------------------------------------------------

#: A Latin word, with the joints a name may hold: Chart.js, Vega-Lite, TEST-project.
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*")
#: Where a sentence opens: at the text's start, or after a full stop, a question or exclamation mark, a colon, a
#: semicolon or a line break, past spaces, quotation marks and brackets.
_OPENING = re.compile(r"(?:^|[.!?:;。！？：；\n])[\s\"'“”‘’(\[]*")
#: A Latin letter after spaces or an apostrophe: the sentence a word opens goes on in English ("What's", "Did we").
_LATIN_NEXT = re.compile(r"[^\S\n]*['’]?[A-Za-z]")
#: Months and weekdays, in full and short: in capitals they name a time, not what a question is about.
_CALENDAR = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec "
    "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun".split()
)
#: Where a message's sentences part: after a closing mark, or after a full stop before a space, so that a version or
#: a domain name (3.11, beat.example.app) stays whole.  A semicolon parts none: a supposition's consequence often
#: follows one ("Suppose we shard it; the host would then be ...").
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?\n])|(?<=\.)(?=\s)")
#: The most things a question the rule judges names: one, or two it compares.  More, in a prompt as short as a
#: question, make a list, and its answer need not name every one.
_QUESTION_NAMES = 2


def statement_text(obj: RetrievedObject) -> str:
    """What a claim asserts, for term matching; other kinds match on content."""
    if obj.kind != "claim":
        return obj.content
    payload = optional_json(obj.content)
    if not isinstance(payload, dict):
        return obj.content
    return " ".join(str(payload.get(key, "")) for key in ("subject", "predicate", "value_text", "conditions"))


def _names(question: str) -> tuple[str, ...]:
    """What a question names: its Latin words that begin with a capital, but for the first word of a sentence that
    goes on in English, which takes one whatever it is.  Chinese has no capitals, so a Latin word opening a question
    in Chinese still names something ("Orchard 的端口是多少？").  A word of capitals alone (I, API, CDNs: the first
    person and abbreviations), a month and a weekday name nothing."""
    text = unicodedata.normalize("NFKC", question)
    openings = {match.end() for match in _OPENING.finditer(text)}
    names = []
    for match in _LATIN_WORD.finditer(text):
        word = match.group(0)
        if (
            not word[0].isupper()
            or word.casefold() in _CALENDAR
            or (word.isalpha() and word.removesuffix("s").isupper())
        ):
            continue
        if match.start() in openings and _LATIN_NEXT.match(text, match.end()):
            continue
        names.append(word)
    return tuple(dict.fromkeys(names))


def _question_names(question: str) -> tuple[str, ...]:
    """The names a prompt's items are held to (``_names``), when it reads as a question about named things: no longer
    than a bare question (``QUESTION_MAX_CHARS``), without a fenced block (``FENCE``) and naming at most
    ``_QUESTION_NAMES`` things.  A pasted log, trace or block of code runs longer or sits in a fence, and its
    capitalised words are not things the person asks about; like a long message or a list, it names nothing here."""
    text = unicodedata.normalize("NFKC", question).strip()
    if len(text) > QUESTION_MAX_CHARS or FENCE.search(text):
        return ()
    names = _names(text)
    return names if len(names) <= _QUESTION_NAMES else ()


def _asserted(content: str) -> str:
    """The sentences of a message its writer says hold, outside quotation marks (``unquoted``): without one that asks
    (``asks_without_answering``) or that supposes, guesses or reports (``UNASSERTED_CONTEXT``)."""
    return " ".join(
        sentence
        for sentence in _SENTENCE_BREAK.split(unquoted(content))
        if not asks_without_answering(sentence) and UNASSERTED_CONTEXT.search(sentence) is None
    )


def _answer_text(item: RetrievedObject, *, resume: bool) -> str | None:
    """What an item answers with: a person's own words (``human_direct``) as they assert them (``_asserted``), or a
    claim's statement.  A claim answers when it is active, or when a person said it, its qualification stopped only at
    the strength of its one source (``CORROBORATION_ELIGIBLE_REASONS``) and its first judgement has not reached it
    (``judgement``, ``retrieval_hydration``): such a proposal waits to be judged.  One the evaluator found
    insufficient, one asleep or blocked, and one held back for what it is (a supposition, a quotation, a subject its
    quote does not bind, a condition it drops) answer nothing, nor does a proposed denial (``claims.is_denial``): the
    store holds a denial as active only as the person's own answer (``mutate.denial_verdict``), and any other says what
    its slot is not.  An assistant's or a tool's message answers nothing either: what a tool observed answers through a
    claim drawn from it.

    An episode answers only a question that asks to resume (``resume``), with the resume of a grounded task
    (``_resumes``): the task's state is what such a question asks for.  Asked anything else it answers nothing, since
    its resume is a summary drawn from messages that answer for themselves, the assistant's and a tool's among them.
    An artifact's label, a name a tool observed, and a reference, an alias resolution drawn from the messages it cites,
    answer nothing."""
    if item.kind in {"claim", "procedure"}:
        metadata = dict(item.metadata)
        payload = optional_json(item.content)
        waits = (
            metadata.get("state") == "proposed"
            and metadata.get("qualification_reason") in CORROBORATION_ELIGIBLE_REASONS
            and metadata.get("judgement") == "awaited"
            and item.origin == "human_direct"
            and not (isinstance(payload, dict) and is_denial(payload))
        )
        return statement_text(item) if metadata.get("state") == "active" or waits else None
    if item.kind == "event":
        return _asserted(item.content) if item.origin == "human_direct" else None
    if item.kind == "episode" and resume and _resumes(item):
        return item.content
    return None


@dataclass(frozen=True)
class Answers:
    """What answers a question (``answering``).  A judged question names what answers it, and ``items`` are the items
    that name it, perhaps none; an unjudged one names nothing to hold them to, and ``items`` are all of them."""

    judged: bool
    items: tuple[RetrievedObject, ...]


def answering(question: str, items: tuple[RetrievedObject, ...]) -> Answers:
    """What answers ``question``: judged when it names one thing or two (``_question_names``), unjudged, with every
    item, when it names nothing.

    An item answers when what it says (``_answer_text``) holds one of the names and one more of the words the
    question asks (``asked_terms``), in any of that word's inflections (``stem``): another project's value shares
    every word of a question but its name, and the named project's other attributes share the name alone.  A question
    that asks nothing past its names ("What is Orchard?", "Tell me about Orchard") is answered by any item that names
    them.  Every name must stand in an answering item, though not all in one, so that the two sides of a comparison
    answer it side by side; when one stands in none, nothing answers.  A grounded task's resume answers a question
    that asks to resume it (``asks_to_resume``) by naming it alone: its words are the task's, not the question's
    ("Continue the Orchard migration" for a task its person called moving Orchard).
    """
    names = _question_names(question)
    if not names:
        return Answers(False, items)
    resume = asks_to_resume(question)
    named = {name: frozenset(lexical_terms(name)) for name in names}
    in_names = {stem(term) for parts in named.values() for term in parts}
    asked = {stem(term) for term in asked_terms(question)} - in_names
    found: list[RetrievedObject] = []
    covered: set[str] = set()
    for item in items:
        # Index terms are pieces of the text they come from, so an item that does not hold a name as written answers
        # nothing, and a long one is passed over without its sentences being read.
        written = unicodedata.normalize("NFKC", item.content).casefold()
        if not any(all(part in written for part in parts) for parts in named.values()):
            continue
        text = _answer_text(item, resume=resume)
        if not text:
            continue
        terms = frozenset(lexical_terms(text))
        # An episode answers only as a resume, which a question that asks to resume it needs to name alone.
        says_more = (
            not asked
            or item.kind == "episode"
            or not asked.isdisjoint(terms)
            or not asked.isdisjoint(stem(term) for term in terms)
        )
        held = {name for name, parts in named.items() if parts <= terms}
        if says_more and held:
            found.append(item)
            covered |= held
    return Answers(True, tuple(found) if len(covered) == len(named) else ())


# -- public -------------------------------------------------------------------

_FOLLOWUP_QUERY = {
    "comparison_second_side": _second_side_query,
    "reason_evidence": _reason_query,
    "resume_state": _resume_query,
}


def unmet_needs(query: str, items: tuple[object, ...]) -> tuple[str, ...]:
    """Describe bounded evidence slots that P09 cannot fill by searching."""
    needs: list[str] = []
    if mentions(query, COMPARE_MARKERS) and _comparison_unmet(query, items):
        needs.append("comparison_second_side")
    if mentions(query, WHY_MARKERS) and _why_unmet(query, items):
        needs.append("reason_evidence")
    if asks_to_resume(query) and _resume_unmet(items):
        needs.append("resume_state")
    return tuple(needs)


def directed_followup_query(context: SearchContext, needs: tuple[str, ...], items: tuple[object, ...]) -> str | None:
    """The one follow-up query the first open need justifies, if any."""
    if not needs or context.limits.followups <= 0:
        return None
    builder = _FOLLOWUP_QUERY.get(needs[0])
    return builder(context.query, items) if builder is not None else None
