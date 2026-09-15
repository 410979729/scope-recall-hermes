"""Trusted importer preservation of the official legacy ordinary-recall policy.

This is an installation operation backed by original row metadata, not a new
human forget command. It uses Core's existing suppression transaction, group
fence, dependency traversal and receipts. It never deletes source content.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import json

from scope_recall.contracts import ContractError
from scope_recall.core.storage import Transaction


POLICY = 'legacy-ordinary-recall-lifecycle-preservation-v1'
HIDDEN = frozenset({'archived', 'obsolete', 'rejected', 'superseded', 'candidate', 'scratch', 'in_progress'})


def lifecycle_metadata(row):
    value = row.get('metadata') or '{}'
    metadata = json.loads(value) if isinstance(value, str) else value
    if not isinstance(metadata, dict):
        raise ValueError('legacy lifecycle metadata must be an object')
    return metadata


def ordinary_lifecycle_visible(row):
    lifecycle = str(lifecycle_metadata(row).get('lifecycle') or '').strip().lower()
    target = str(row.get('target') or '').strip().lower()
    return lifecycle not in HIDDEN or (lifecycle == 'scratch' and target == 'general')


def plan_lifecycle_suppression(tx, memory_rows, *, require_same_scope=False):
    """Read source identities and run the actual Core closure without writes."""
    conn = tx._check()
    groups = defaultdict(list)
    lifecycle_counts = Counter()
    hidden_counts = Counter()
    for row in memory_rows:
        lifecycle = str(lifecycle_metadata(row).get('lifecycle') or '').strip().lower()
        lifecycle_counts[lifecycle] += 1
        if ordinary_lifecycle_visible(row):
            continue
        hidden_counts[lifecycle] += 1
        legacy_id = str(row['id'])
        source = conn.execute('SELECT event_id,source_revision,scope_id,project_id,branch_id,extra_json '
            'FROM source_events WHERE source_event_key=?', ('legacy:memories:' + legacy_id,)).fetchall()
        if len(source) != 1:
            raise ValueError('legacy hidden memory must map to exactly one original source')
        source = source[0]
        extra = json.loads(source['extra_json'])
        expected = extra.get('legacy_metadata', {})
        if (extra.get('legacy_table') != 'memories' or str(extra.get('legacy_id')) != legacy_id
                or str(expected.get('lifecycle') or '').strip().lower() != lifecycle
                or str(extra.get('target') or '').strip().lower() != str(row.get('target') or '').strip().lower()):
            raise ValueError('legacy lifecycle source evidence mismatch')
        if require_same_scope and source['scope_id'] != row['scope_id']:
            raise ValueError('legacy lifecycle source scope mismatch')
        groups[(source['project_id'], source['branch_id'])].append(source['event_id'])
    partitions = []
    for (project, branch), refs in sorted(groups.items(), key=lambda item: repr(item[0])):
        context = replace(tx.context, project_id=project, branch_id=branch)
        scoped = Transaction(conn, context, writable=False)
        try:
            targets = tuple(scoped.deletions.target(ref) for ref in sorted(set(refs)))
            # Core refuses unresolved dependencies, cross-partition dependencies,
            # foreign scope access and overlarge closures. Never omit these edges.
            affected = scoped.deletions.closure(targets, delete=False)
            partitions.append({'project_id': project, 'branch_id': branch,
                'target_refs': [t.ref for t in targets],
                'expected_revisions': {t.ref: t.revision for t in targets},
                'affected': [{'kind': t.kind, 'ref': t.ref, 'revision': t.revision, 'scope_id': t.scope_id} for t in affected]})
        finally:
            scoped._finish()
    return {'policy': POLICY, 'mode': 'suppress', 'lifecycle_counts': dict(lifecycle_counts),
            'hidden_lifecycle_counts': dict(hidden_counts), 'target_count': sum(hidden_counts.values()),
            'affected_count': sum(len(p['affected']) for p in partitions), 'partitions': partitions}


def apply_lifecycle_suppression(tx, memory_rows, *, now, require_same_scope=False, expected_plan=None):
    """Apply metadata-attested suppression atomically inside the caller's writer."""
    plan = plan_lifecycle_suppression(tx, memory_rows, require_same_scope=require_same_scope)
    if expected_plan is not None and plan != expected_plan:
        raise ValueError('lifecycle suppression plan changed')
    conn = tx._check(write=True)
    receipts = []
    for part in plan['partitions']:
        context = replace(tx.context, project_id=part['project_id'], branch_id=part['branch_id'])
        scoped = Transaction(conn, context, writable=True)
        try:
            request = {'mode': 'suppress', 'target_refs': part['target_refs'],
                       'expected_revisions': part['expected_revisions']}
            targets = tuple(scoped.deletions.target(ref) for ref in request['target_refs'])
            affected = scoped.deletions.closure(targets, delete=False)
            op = scoped.deletions.block(request, affected, now=now)
            receipt = scoped.deletions.receipt(op)
            if not receipt or receipt['read_blocked'] or not receipt['suppressed'] or receipt['active_content_removed']:
                raise ContractError('STORAGE_UNAVAILABLE', 'legacy_suppression_receipt')
            # Confirm every member is fenced even on a resumed/idempotent call.
            from scope_recall.core.visibility import allowed
            for member in affected:
                if allowed(scoped, member.kind, member.ref, automatic=True):
                    raise ContractError('STORAGE_UNAVAILABLE', 'legacy_suppression_missing')
            receipts.append(receipt)
        finally:
            scoped._finish()
    return {**plan, 'receipts': receipts, 'source_content_retained': True, 'automatic_recall_allowed': False,
            'visible_effective_claims_allowed': False, 'authorization': 'trusted_installer_original_lifecycle_metadata'}
