"""Literal grammatical normalization; never semantic similarity or model authority."""

from __future__ import annotations

import re
from copy import deepcopy

from .claims import evidence_context, rejects_other_value
from .source_qualification import AUTHORITY_QUESTION, REPORTED_SPEECH, UNASSERTED_UNCERTAINTY

PROJECT_TAG = re.compile(r"项目[【\[][^【】\[\]\n]{1,120}[】\]]")
_SELF_ATTRIBUTE = re.compile(r"(?:我的|本人的)(?:长期|默认|通常)?(?:偏好|喜好|习惯|要求|决定)")
_EMBEDDED = re.compile(r"([^，,。;；!?！？]{1,120}?)(使用|采用|选择)([^，,。;；!?！？]{1,240})")
_MODAL = re.compile(r"^(?:应当|应该|应|仍然|仍)(使用|采用|选择|是)$")
_COMPOSITE_VALUE = re.compile(r"([^，,;；。!?！？\n]{1,120})[，,]\s*(?:不是|并非)([^，,;；。!?！？\n]{1,120})")
_RENDITION = re.compile(r"((?:对外|对内|内部|外部)版)((?:仍然|仍)?(?:是|使用|采用|选择))([^，,;；。!?！？\n]{1,120})")


def _first_hand_assertion(proposal, roots) -> str | None:
    """The assertion around the proposal's one quote, when the quote comes once from one complete statement a verified
    person made directly, and that statement asserts (no question of authority, no uncertainty, no reported speech);
    None otherwise."""
    spans = proposal["evidence_spans"]
    if len(spans) != 1:
        return None
    span = spans[0]
    matching = [r for r in roots if (r.ref, r.revision) == (span["source_ref"], span["source_revision"])]
    if len(matching) != 1:
        return None
    root = matching[0]
    principal = root.source_principal or {}
    if (
        root.origin != "human_direct"
        or root.capture_state != "complete"
        or root.capture_gaps
        or principal.get("resolution") != "verified"
        or principal.get("kind") != "human"
    ):
        return None
    if not span["quote"] or root.content.count(span["quote"]) != 1:
        return None
    assertion = evidence_context(root.content, span["quote"])
    if (
        AUTHORITY_QUESTION.search(assertion)
        or UNASSERTED_UNCERTAINTY.search(assertion)
        or REPORTED_SPEECH.search(assertion)
    ):
        return None
    return assertion


def normalize_frame(proposal, roots):
    """Keep only uniquely sourced project/subject/verb components.

    A bracketed project prefix is an explicit applicability condition. A
    preference whose value repeats its verb contains an embedded literal
    subject/verb/value frame. Only these syntactic forms are normalized.
    """
    if proposal["kind"] not in {"preference", "constraint", "decision"}:
        return proposal
    assertion = _first_hand_assertion(proposal, roots)
    if assertion is None:
        return proposal
    projects = set(PROJECT_TAG.findall(assertion))
    if len(projects) != 1:
        return proposal
    project = next(iter(projects))
    subject, predicate, value = (proposal[k] for k in ("subject", "predicate", "value_text"))
    changed = False
    embedded = _EMBEDDED.fullmatch(value) if _SELF_ATTRIBUTE.fullmatch(subject) else None
    if embedded and embedded.group(2) == predicate:
        subject, predicate, value = embedded.groups()
        changed = True
    if subject.startswith(project) and len(subject) > len(project):
        subject = subject[len(project) :].strip()
        changed = True
    modal = _MODAL.fullmatch(predicate)
    if modal:
        predicate = modal.group(1)
        changed = True
    composite = _COMPOSITE_VALUE.fullmatch(value)
    if composite:
        positive = composite.group(1).strip()
        check = dict(proposal, subject=subject, predicate=predicate, value_text=positive)
        if not rejects_other_value(assertion, check):
            return proposal
        value = positive
        changed = True
    # All three components must form an ordered assertion in the SAME clause.
    # A model cannot splice a project, subject and value from different rows.
    clauses = re.split(r"[，,;；。!?！？\n]", assertion)
    frame = re.compile(
        re.escape(subject) + r"\s*(?:应当|应该|应|仍然|仍)?" + re.escape(predicate) + r"\s*" + re.escape(value)
    )
    if not any(frame.search(clause) for clause in clauses):
        return proposal
    conditions = []
    for condition in proposal["conditions"]:
        # A sibling assertion mistakenly put in conditions is a separate
        # frame. Only exact rendition clauses in the same assertion qualify.
        if composite and _RENDITION.fullmatch(condition) and condition in clauses:
            continue
        wrapper = r"(?:只|仅)?(?:对|在)?" + re.escape(project) + r"(?:而言|中|内)?"
        conditions.append(project if re.fullmatch(wrapper, condition) else condition)
    if project not in conditions:
        conditions.append(project)
    if re.fullmatch(r"(?:对外|对内|内部|外部)版", subject) and subject not in conditions:
        conditions.append(subject)
    changed = changed or sorted(set(conditions)) != sorted(set(proposal["conditions"]))
    if not changed:
        return proposal
    result = deepcopy(proposal)
    result.update(subject=subject, predicate=predicate, value_text=value, conditions=sorted(set(conditions)))
    result["evidence_spans"][0]["quote"] = assertion
    return result


#: Naming a thing, in the literal forms a person says it: "我的猫咪叫年糕", "…的名字是…", "My cat is called …".
_NAMING = (
    re.compile(
        r"(?P<subject>[^，,;；。!?！？\n]{1,60}?)\s*(?P<verb>叫做|名叫|名字叫|的名字是|名字是|叫)\s*(?P<name>[^，,;；。!?！？\n]{1,60})"
    ),
    re.compile(r"(?P<subject>[^,;.!?\n]{1,80}?)\s+(?P<verb>is called|is named)\s+(?P<name>[^,;.!?\n]{1,60})", re.I),
)
#: A name said under a condition, or as an example, was not given: "如果我养猫的话，我的猫叫年糕".
_UNASSERTED_NAMING = re.compile(
    r"假设|假如|如果|要是|倘若|万一|除非|设想|虚构|假定|比如|例如|举例|"
    r"\b(?:if|unless|suppose|supposing|imagine|hypothetical|fictional|for example)\b",
    re.I,
)


def name_frame(proposal, roots):
    """An alias that names nothing already known is the thing's name: a fact, framed from the quote.

    An alias attaches another name to an existing fact (``alias.target_ref`` names a claim), and is
    held back until that link is proved.  Told "我的猫咪叫年糕" with nothing yet known about the cat,
    the consolidation model filed "年糕" as an alias whose target was the message itself: it
    could never be proved, stayed proposed, and no agent recalled the cat's name.  Only the literal
    naming forms above are re-framed, and only from one complete first-hand statement; the subject,
    verb and name are the quote's own words, so the fact is qualified like any other.
    """
    alias = proposal.get("alias")
    if (
        proposal["kind"] != "alias"
        or not isinstance(alias, dict)
        or str(alias.get("target_ref") or "").startswith("claim-")
    ):
        return proposal
    assertion = _first_hand_assertion(proposal, roots)
    if assertion is None or _UNASSERTED_NAMING.search(assertion):
        return proposal
    name = str(alias.get("name") or "").strip()
    for clause in re.split(r"[，,;；。!?！？\n]", assertion):
        clause = clause.strip()
        for form in _NAMING:
            found = form.fullmatch(clause)
            if found and found.group("name").strip() == name:
                result = {key: deepcopy(value) for key, value in proposal.items() if key != "alias"}
                result.update(
                    kind="fact", subject=found.group("subject").strip(), predicate=found.group("verb"), value_text=name
                )
                result["evidence_spans"][0]["quote"] = clause
                return result
    return proposal


def expand_frames(proposal, roots):
    """Recover explicit sibling frames misplaced in a composite correction.

    No free extraction: only verbatim rendition assertions supplied in the
    model's conditions and independently present in the grounded assertion.
    """
    primary = name_frame(normalize_frame(proposal, roots), roots)
    result = [primary]
    if not _COMPOSITE_VALUE.fullmatch(proposal["value_text"]) or primary["value_text"] == proposal["value_text"]:
        return result
    assertion = primary["evidence_spans"][0]["quote"]
    clauses = re.split(r"[，,;；。!?！？\n]", assertion)
    for condition in proposal["conditions"]:
        match = _RENDITION.fullmatch(condition)
        if not match or condition not in clauses or condition in primary["conditions"]:
            continue
        subject, predicate, value = match.groups()
        sibling = deepcopy(primary)
        sibling.update(
            subject=subject,
            predicate=predicate,
            value_text=value,
            conditions=sorted(set(primary["conditions"] + [subject])),
        )
        result.append(normalize_frame(sibling, roots))
    return result


def human_owner(roots):
    owners = {
        (r.source_principal or {}).get("principal_ref")
        for r in roots
        if r.origin == "human_direct" and (r.source_principal or {}).get("resolution") == "verified"
    }
    return next(iter(owners)) if len(owners) == 1 and None not in owners else None


def source_order(tx, roots):
    """Trusted ingestion order for live human sources with unknown event time."""
    if not roots or any(r.origin != "human_direct" for r in roots) or human_owner(roots) is None:
        return None
    rows = [
        tx._check()
        .execute("SELECT rowid FROM source_events WHERE event_id=? AND source_revision=?", (r.ref, r.revision))
        .fetchone()
        for r in roots
    ]
    return max(row[0] for row in rows) if all(rows) else None
