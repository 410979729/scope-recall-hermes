"""Literal grammatical normalization; never semantic similarity or model authority."""
from __future__ import annotations

from copy import deepcopy
import re

from .source_qualification import AUTHORITY_QUESTION, UNASSERTED_UNCERTAINTY, REPORTED_SPEECH

_PROJECT = re.compile(r"项目[【\[][^【】\[\]\n]{1,120}[】\]]")
_SELF_ATTRIBUTE = re.compile(r"(?:我的|本人的)(?:长期|默认|通常)?(?:偏好|喜好|习惯|要求|决定)")
_EMBEDDED = re.compile(r"([^，,。;；!?！？]{1,120}?)(使用|采用|选择)([^，,。;；!?！？]{1,240})")
_MODAL = re.compile(r"^(?:应当|应该|应|仍然|仍)(使用|采用|选择|是)$")
_COMPOSITE_VALUE = re.compile(r'([^，,;；。!?！？\n]{1,120})[，,]\s*(?:不是|并非)([^，,;；。!?！？\n]{1,120})')
_RENDITION = re.compile(r'((?:对外|对内|内部|外部)版)((?:仍然|仍)?(?:是|使用|采用|选择))([^，,;；。!?！？\n]{1,120})')


def normalize_frame(proposal, roots):
    """Keep only uniquely sourced project/subject/verb components.

    A bracketed project prefix is an explicit applicability condition. A
    preference whose value repeats its verb contains an embedded literal
    subject/verb/value frame. Only these syntactic forms are normalized.
    """
    if proposal['kind'] not in {'preference', 'constraint', 'decision'}:
        return proposal
    spans = proposal['evidence_spans']
    if len(spans) != 1:
        return proposal
    span = spans[0]
    matching = [r for r in roots if (r.ref, r.revision) == (span['source_ref'], span['source_revision'])]
    if len(matching) != 1:
        return proposal
    root = matching[0]
    principal = root.source_principal or {}
    if (root.origin != 'human_direct' or root.capture_state != 'complete' or root.capture_gaps
            or principal.get('resolution') != 'verified' or principal.get('kind') != 'human'):
        return proposal
    if not span['quote'] or root.content.count(span['quote']) != 1:
        return proposal
    from .claims import evidence_context

    assertion = evidence_context(root.content, span['quote'])
    if AUTHORITY_QUESTION.search(assertion) or UNASSERTED_UNCERTAINTY.search(assertion) or REPORTED_SPEECH.search(assertion):
        return proposal
    projects = set(_PROJECT.findall(assertion))
    if len(projects) != 1:
        return proposal
    project = next(iter(projects))
    subject, predicate, value = (proposal[k] for k in ('subject', 'predicate', 'value_text'))
    changed = False
    embedded = _EMBEDDED.fullmatch(value) if _SELF_ATTRIBUTE.fullmatch(subject) else None
    if embedded and embedded.group(2) == predicate:
        subject, predicate, value = embedded.groups()
        changed = True
    if subject.startswith(project) and len(subject) > len(project):
        subject = subject[len(project):].strip()
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
    clauses = re.split(r'[，,;；。!?！？\n]', assertion)
    frame = re.compile(re.escape(subject) + r'\s*(?:应当|应该|应|仍然|仍)?' + re.escape(predicate) + r'\s*' + re.escape(value))
    if not any(frame.search(clause) for clause in clauses):
        return proposal
    conditions = []
    for condition in proposal['conditions']:
        # A sibling assertion mistakenly put in conditions is a separate
        # frame. Only exact rendition clauses in the same assertion qualify.
        if composite and _RENDITION.fullmatch(condition) and condition in clauses:
            continue
        wrapper = r'(?:只|仅)?(?:对|在)?' + re.escape(project) + r'(?:而言|中|内)?'
        conditions.append(project if re.fullmatch(wrapper, condition) else condition)
    if project not in conditions:
        conditions.append(project)
    if re.fullmatch(r'(?:对外|对内|内部|外部)版', subject) and subject not in conditions:
        conditions.append(subject)
    changed = changed or sorted(set(conditions)) != sorted(set(proposal['conditions']))
    if not changed:
        return proposal
    result = deepcopy(proposal)
    result.update(subject=subject, predicate=predicate, value_text=value, conditions=sorted(set(conditions)))
    result['evidence_spans'][0]['quote'] = assertion
    return result


#: Naming a thing, in the literal forms a person says it: "我的猫咪叫年糕", "…的名字是…", "My cat is called …".
_NAMING = (
    re.compile(r'(?P<subject>[^，,;；。!?！？\n]{1,60}?)\s*(?P<verb>叫做|名叫|名字叫|的名字是|名字是|叫)\s*(?P<name>[^，,;；。!?！？\n]{1,60})'),
    re.compile(r'(?P<subject>[^,;.!?\n]{1,80}?)\s+(?P<verb>is called|is named)\s+(?P<name>[^,;.!?\n]{1,60})', re.I),
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
    alias = proposal.get('alias')
    if proposal['kind'] != 'alias' or not isinstance(alias, dict) or str(alias.get('target_ref') or '').startswith('claim-'):
        return proposal
    spans = proposal['evidence_spans']
    if len(spans) != 1:
        return proposal
    span = spans[0]
    matching = [r for r in roots if (r.ref, r.revision) == (span['source_ref'], span['source_revision'])]
    if len(matching) != 1:
        return proposal
    root = matching[0]
    principal = root.source_principal or {}
    if (root.origin != 'human_direct' or root.capture_state != 'complete' or root.capture_gaps
            or principal.get('resolution') != 'verified' or principal.get('kind') != 'human'):
        return proposal
    if not span['quote'] or root.content.count(span['quote']) != 1:
        return proposal
    from .claims import evidence_context

    assertion = evidence_context(root.content, span['quote'])
    if AUTHORITY_QUESTION.search(assertion) or UNASSERTED_UNCERTAINTY.search(assertion) or REPORTED_SPEECH.search(assertion):
        return proposal
    name = str(alias.get('name') or '').strip()
    for clause in re.split(r'[，,;；。!?！？\n]', assertion):
        clause = clause.strip()
        for form in _NAMING:
            found = form.fullmatch(clause)
            if found and found.group('name').strip() == name:
                result = {key: deepcopy(value) for key, value in proposal.items() if key != 'alias'}
                result.update(kind='fact', subject=found.group('subject').strip(), predicate=found.group('verb'),
                              value_text=name)
                result['evidence_spans'][0]['quote'] = clause
                return result
    return proposal


def expand_frames(proposal, roots):
    """Recover explicit sibling frames misplaced in a composite correction.

    No free extraction: only verbatim rendition assertions supplied in the
    model's conditions and independently present in the grounded assertion.
    """
    primary = name_frame(normalize_frame(proposal, roots), roots)
    result = [primary]
    if not _COMPOSITE_VALUE.fullmatch(proposal['value_text']) or primary['value_text'] == proposal['value_text']:
        return result
    assertion = primary['evidence_spans'][0]['quote']
    clauses = re.split(r'[，,;；。!?！？\n]', assertion)
    for condition in proposal['conditions']:
        match = _RENDITION.fullmatch(condition)
        if not match or condition not in clauses or condition in primary['conditions']:
            continue
        subject, predicate, value = match.groups()
        sibling = deepcopy(primary)
        sibling.update(subject=subject, predicate=predicate, value_text=value,
                       conditions=sorted(set(primary['conditions'] + [subject])))
        result.append(normalize_frame(sibling, roots))
    return result


def human_owner(roots):
    owners = {(r.source_principal or {}).get('principal_ref') for r in roots
              if r.origin == 'human_direct' and (r.source_principal or {}).get('resolution') == 'verified'}
    return next(iter(owners)) if len(owners) == 1 and None not in owners else None


def source_order(tx, roots):
    """Trusted ingestion order for live human sources with unknown event time."""
    if not roots or any(r.origin != 'human_direct' for r in roots) or human_owner(roots) is None:
        return None
    rows = [tx._check().execute('SELECT rowid FROM source_events WHERE event_id=? AND source_revision=?',
                               (r.ref, r.revision)).fetchone() for r in roots]
    return max(row[0] for row in rows) if all(rows) else None


def repair_frames(tx, *, now, after_ref='', limit=16):
    """Bounded upgrade repair through the ordinary evidence/application path.

    The caller persists the cursor; old versions and original sources remain.
    No model call, manual approval, or fabricated claim payload is involved.
    """
    from .mutate import apply_claim, evidence_refs, register_applied_candidate
    from .claims import same_assertion, Qualification
    from ..contracts import ContractError

    if type(limit) is not int or not 1 <= limit <= 32:
        raise ContractError('INPUT_INVALID', 'normalization_limit')
    # Filter authorization before pagination so an already-visited prefix or
    # another audience cannot starve later records.
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = tx._check().execute(f'''SELECT claim_id FROM claims WHERE claim_id>? AND read_blocked=0 AND suppressed=0
        AND scope_id IN ({','.join('?' for _ in scopes)}) AND project_id IS ? AND branch_id IS ?
        ORDER BY claim_id LIMIT ?''', (after_ref, *scopes, tx.context.project_id, tx.context.branch_id, limit)).fetchall()
    repaired, errors = [], []
    for row in rows:
        versions = tx.claims.versions(row[0])
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.state not in {'active', 'proposed', 'disputed'}:
            continue
        try:
            with tx.savepoint():
                roots = tx.claims.roots(evidence_refs(head.payload))
                frames = expand_frames(head.payload, roots)
                if len(frames) == 1 and same_assertion(head.payload, frames[0]):
                    continue
                mutation = apply_claim(tx, frames[0], head.scope_id, now)
                register_applied_candidate(tx, mutation, now)
                for sibling in frames[1:]:
                    sibling_mutation = apply_claim(tx, sibling, head.scope_id, now)
                    register_applied_candidate(tx, sibling_mutation, now)
                    repaired.append({'original_ref':head.ref,'ref':sibling_mutation.ref,'revision':sibling_mutation.revision,
                                     'state':sibling_mutation.state,'disposition':sibling_mutation.disposition})
                if mutation.ref != head.ref and mutation.state == 'active' and head.state in {'active','disputed'}:
                    # Retire a duplicate legacy frame only after its corrected
                    # canonical replacement passed ordinary evidence admission.
                    # Append a version; never rewrite the original payload.
                    retired = tx.claims.append(head.scope_id, head.payload,
                        Qualification('retracted', head.basis, 'canonical_frame_reconciled'),
                        recorded_at=now, previous=head)
                    tx.candidates.register(retired.ref, retired.revision, observed_at=now, schedule_initial=False)
                if mutation.ref != head.ref and head.state == 'proposed':
                    conn = tx._check(write=True)
                    conn.execute("UPDATE candidate_lifecycle SET processing_state='archived',reason='canonical_frame_reconciled',updated_at=? WHERE candidate_ref=?", (now,head.ref))
                    conn.execute("UPDATE work_items SET state='obsolete',last_error_code='canonical_frame_reconciled' WHERE work_type='evaluate_candidate' AND subject_ref=? AND state IN ('pending','leased')", ('candidate:'+head.ref,))
                    conn.execute("UPDATE candidate_evaluations SET state='obsolete',reason='canonical_frame_reconciled',completed_at=? WHERE candidate_ref=? AND state='queued'", (now,head.ref))
                repaired.append({'original_ref':head.ref,'ref':mutation.ref,'revision':mutation.revision,'state':mutation.state,'disposition':mutation.disposition})
        except ContractError as exc:
            errors.append({'ref':head.ref,'code':exc.code,'field':exc.field})
    return {'cursor':rows[-1][0] if rows else after_ref,'scanned':len(rows),'items':repaired,'errors':errors,'done':len(rows)<limit}


def rejects_other_value(content, proposal):
    """An explicit 'X, not Y' does not negate the positive X clause.

    A general denial of the statement remains a denial. Every negative clause
    must be an isolated rejected alternative; questions/uncertainty are still
    checked by the owning qualifier.
    """
    from .claims import _NEGATION
    clauses = re.split(r'[，,;；。!?！？\n]', content)
    subject, predicate, value = (proposal[k] for k in ('subject','predicate','value_text'))
    frame = re.compile(re.escape(subject)+r'\s*(?:应当|应该|应|仍然|仍)?'+re.escape(predicate)+r'\s*'+re.escape(value))
    if not any(frame.search(c) and not _NEGATION.search(c) for c in clauses):
        return False
    negatives = [c.strip() for c in clauses if _NEGATION.search(c)]
    if not negatives:
        return False
    for clause in negatives:
        match = re.fullmatch(r'(?:不是|并非)([^\s，,。;；!?！？]{1,120})', clause)
        if (not match or value in match.group(1)
                or re.search(r'事实|确认|真的|真实|认真|意思|偏好|断言|承诺|决定|成立|正确', match.group(1))):
            return False
    return True
