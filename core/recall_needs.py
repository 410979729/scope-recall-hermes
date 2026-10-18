"""Evidence slots a query leaves open, the one directed follow-up they justify, and the items that answer it.

A comparison needs both named sides, a "why" needs a reason clause that names
the target, a resume needs a grounded open episode.  A question that names
something is answered only by an item that names it too and says more of what
the question asks (``answering``).  These are read off the query and the
hydrated items; nothing here searches, and a need never manufactures evidence.
"""

from __future__ import annotations

import re
import unicodedata

from .events import lexical_terms, version_suffixes
from .fact_evidence import stem
from .recall_policy import asks_without_answering, hard_identifiers, meaningful_query_terms
from .retrieval import STALE_RESUME_GAPS, RetrievedObject, SearchContext, optional_json
from .source_qualification import UNASSERTED_CONTEXT, unquoted

COMPARE_MARKERS = ("比较", "对比", "差异", "哪个更", "compare", "versus", " vs ")
WHY_MARKERS = ("为什么", "为何", "原因", "why", "reason")
RESUME_MARKERS = ("继续", "接着", "恢复", "resume", "continue")
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


def _resume_unmet(items: tuple[object, ...]) -> bool:
    for item in items:
        if getattr(item, "kind", "") != "episode":
            continue
        gaps = optional_json(dict(getattr(item, "metadata", ())).get("gaps")) or ()
        if any(gap in gaps for gap in STALE_RESUME_GAPS):
            continue
        resume = optional_json(getattr(item, "content", ""))
        if type(resume) is not dict:
            continue
        goal = resume.get("goal")
        if type(goal) is not dict or not goal.get("text") or not goal.get("evidence_refs"):
            continue
        basis = str(resume.get("next_step_basis") or "").casefold()
        grounded_next_step = bool(resume.get("next_step") and basis in _GROUNDED_NEXT_STEP_BASES)
        if _grounded(resume.get("verified_progress")) or _grounded(resume.get("open_items")) or grounded_next_step:
            return False
    return True


def _resume_query(query: str, items: tuple[object, ...]) -> str:
    # One directed pass only, within the original shared budgets.
    terms = tuple(term for term in meaningful_query_terms(query) if term.casefold() not in RESUME_MARKERS)
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


def _asserted(content: str) -> str:
    """The sentences of a message its writer says hold, outside quotation marks (``unquoted``): without one that asks
    (``asks_without_answering``) or that supposes, guesses or reports (``UNASSERTED_CONTEXT``)."""
    return " ".join(
        sentence
        for sentence in _SENTENCE_BREAK.split(unquoted(content))
        if not asks_without_answering(sentence) and UNASSERTED_CONTEXT.search(sentence) is None
    )


def _answer_text(item: RetrievedObject) -> str | None:
    """What an item answers with: a person's own words (``human_direct``) as they assert them (``_asserted``), an
    active claim's statement, another kind's content.  Neither an assistant's or a tool's message nor a claim waiting
    to be judged answers: what a tool observed answers through an active claim drawn from it."""
    if item.kind in {"claim", "procedure"}:
        return statement_text(item) if dict(item.metadata).get("state") == "active" else None
    if item.kind == "event":
        return _asserted(item.content) if item.origin == "human_direct" else None
    return item.content


def answering(question: str, items: tuple[RetrievedObject, ...]) -> tuple[RetrievedObject, ...]:
    """The items that answer what ``question`` names (``_names``); every item when it names nothing.

    An item answers when what it says (``_answer_text``) holds one of the names and one more of the question's
    words, in any of that word's inflections (``stem``): another project's value shares every word of a question but
    its name, and the named project's other attributes share the name alone.  Every name must stand in an answering
    item, though not all in one, so that the two sides of a comparison answer it side by side; when one stands in
    none, nothing answers.
    """
    names = _names(question)
    if not names:
        return items
    named = {name: frozenset(lexical_terms(name)) for name in names}
    in_names = {stem(term) for parts in named.values() for term in parts}
    asked = {stem(term) for term in meaningful_query_terms(question)} - in_names
    found: list[RetrievedObject] = []
    covered: set[str] = set()
    for item in items:
        # Index terms are pieces of the text they come from, so an item that does not hold a name as written answers
        # nothing, and a long one is passed over without its sentences being read.
        written = unicodedata.normalize("NFKC", item.content).casefold()
        if not any(all(part in written for part in parts) for parts in named.values()):
            continue
        text = _answer_text(item)
        if not text:
            continue
        terms = frozenset(lexical_terms(text))
        if asked and asked.isdisjoint(terms) and asked.isdisjoint(stem(term) for term in terms):
            continue
        held = {name for name, parts in named.items() if parts <= terms}
        if held:
            found.append(item)
            covered |= held
    return tuple(found) if len(covered) == len(named) else ()


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
    if mentions(query, RESUME_MARKERS) and _resume_unmet(items):
        needs.append("resume_state")
    return tuple(needs)


def directed_followup_query(context: SearchContext, needs: tuple[str, ...], items: tuple[object, ...]) -> str | None:
    """The one follow-up query the first open need justifies, if any."""
    if not needs or context.limits.followups <= 0:
        return None
    builder = _FOLLOWUP_QUERY.get(needs[0])
    return builder(context.query, items) if builder is not None else None
