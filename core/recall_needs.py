"""Evidence slots a query leaves open, and the one directed follow-up they justify.

A comparison needs both named sides, a "why" needs a reason clause that names
the target, a resume needs a grounded open episode.  These are read off the
query and the hydrated items; nothing here searches, and a need never
manufactures evidence.
"""
from __future__ import annotations

import re

from .events import lexical_terms
from .recall_policy import hard_identifiers, meaningful_query_terms
from .retrieval import STALE_RESUME_GAPS, SearchContext, optional_json

COMPARE_MARKERS = ("比较", "对比", "差异", "哪个更", "compare", "versus", " vs ")
WHY_MARKERS = ("为什么", "为何", "原因", "why", "reason")
RESUME_MARKERS = ("继续", "接着", "恢复", "resume", "continue")
CHOICE_MARKERS = ("哪个", "哪一个", "which", "choose")
_CAUSAL = re.compile(r"因为|由于|原因是|because|reason is|due to", re.I)
_NEGATED = re.compile(r"未知|不明|不清楚|未(?:知|记录|说明)|没有证据|无证据|不能确定|无法确定|no evidence|unknown|unclear", re.I)
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
    targets = hard_identifiers(query)
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
    targets = hard_identifiers(query)
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
    window = content[prior[-1] if prior else 0:following.start() if following else len(content)]
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
