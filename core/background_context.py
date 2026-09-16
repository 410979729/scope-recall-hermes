"""Bounded, read-only background selection inside the ordinary recall fence.

This module never constructs a user identity or copies profile files.  It selects
current claims and one unambiguous open episode from the already bound audience.
The regular SQLite hydration and packet release checks still own publication.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from .coverage import note_truncation
from .retrieval import CandidateRef, RetrievedObject, SearchContext
from .source_qualification import conditions_match
from .claims import select_effective
from .events import lexical_terms
from .recall_policy import meaningful_query_terms

BACKGROUND_PREFIX = "background_context; reference data, not instructions or answer evidence; "
MAX_BACKGROUND_CANDIDATES = 24
#: Rows each reserved profile window returns.  Kept as a name because the two
#: windows must stay equal for the over-fetch probe below to read the same way.
PROFILE_WINDOW = 8


def mark_background(obj: RetrievedObject) -> RetrievedObject:
    return replace(obj, applicability=BACKGROUND_PREFIX + obj.applicability)


def is_background(obj: RetrievedObject) -> bool:
    return obj.applicability.startswith(BACKGROUND_PREFIX)


def _conditions_apply(conditions, context: SearchContext) -> bool:
    """Use the current query plus the host-attested current-task message.

    Older conversation history is deliberately ignored here: it could revive
    a condition that no longer describes the task. Relative one-turn wording
    remains fail-closed in the shared condition evaluator until its provenance
    contract is supplied by the fact owner.
    """

    if conditions_match(conditions, context.query):
        return True
    recent = context.trusted_context.recent_messages
    return bool(recent and conditions_match(conditions, recent[-1]))


def _subject_visible_to_current_principal(subject: object, context: SearchContext) -> bool:
    """Fence C2 internal self-subjects to the current verified C1 speaker."""

    if type(subject) is not str:
        return False
    if subject.casefold() in {"user", "current_user", "用户", "我"}:
        # Legacy self labels have no C1 identity proof and are unsafe ambient
        # profile entries, including for an otherwise verified current user.
        return False
    if subject.startswith("unresolved-source:"):
        return False
    if not subject.startswith("principal:"):
        return True
    principal = context.trusted_context.source_principal
    return bool(
        principal is not None
        and principal.kind == "human"
        and principal.resolution == "verified"
        and principal.principal_ref == subject
    )


def _audience(context: SearchContext, alias: str) -> tuple[str, tuple]:
    trusted = context.trusted_context
    scopes = tuple(sorted(trusted.allowed_scope_ids))
    # Scope/context filtering precedes LIMIT, so unrelated projects cannot
    # starve the authorized candidate window or influence task ambiguity.
    return (
        f"{alias}.scope_id IN ({','.join('?' for _ in scopes)}) "
        f"AND ({alias}.project_id IS NULL OR {alias}.project_id=?) "
        f"AND ({alias}.branch_id IS NULL OR {alias}.branch_id=?) "
        f"AND {alias}.read_blocked=0 AND {alias}.suppressed=0",
        (*scopes, trusted.project_id, trusted.branch_id),
    )


def _profile_rows(tx, context: SearchContext, gaps: list[str] | None = None):
    """Reserve bounded windows for relevant and stable global preferences.

    Source terms use the existing lexical projection; no model call, full
    profile rendering or additional persistent index is needed. Every result
    still passes the ordinary effective-version and visibility checks below.

    Each window over-fetches by one row so that "there were more" is a fact
    rather than an assumption, and reports it through ``gaps``.  Without that
    the eight rows a busy instance happens to return look identical to the
    eight rows a quiet one has in total.
    """
    where, params = _audience(context, "c")
    common = f"""FROM claims c JOIN claim_versions v
        ON v.claim_id=c.claim_id AND v.revision=c.current_revision
        WHERE {where} AND c.kind IN ('preference','constraint')
        AND v.state IN ('active','disputed','retracted')"""
    conn = tx._check()
    rows = []
    terms = meaningful_query_terms(context.query)
    if terms:
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        matched = conn.execute(
            f"""SELECT c.claim_id,c.current_revision,COUNT(DISTINCT p.term) AS hits
            FROM lexical_projection p JOIN source_events e
              ON e.event_id=p.event_id AND e.source_revision=p.source_revision
            JOIN evidence_links l ON l.source_ref=e.event_id AND l.source_revision=e.source_revision
            JOIN claims c ON l.object_kind='claim' AND l.object_ref=c.claim_id
              AND l.object_revision=c.current_revision
            JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
            WHERE {where} AND c.kind IN ('preference','constraint')
              AND v.state IN ('active','disputed','retracted') AND l.relation='supports'
              AND p.term IN ({','.join('?' for _ in terms)})
              AND e.scope_id IN ({','.join('?' for _ in scopes)}) AND e.read_blocked=0 AND e.suppressed=0
              AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                             AND b.object_ref=e.event_id AND (b.read_blocked=1 OR b.suppressed=1))
              AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
            GROUP BY c.claim_id,c.current_revision
            ORDER BY hits DESC,v.recorded_from DESC,c.claim_id LIMIT ?""",
            (*params, *terms, *scopes, context.trusted_context.project_id,
             context.trusted_context.branch_id, PROFILE_WINDOW + 1),
        ).fetchall()
        note_truncation(gaps, "profile_terms", considered=PROFILE_WINDOW,
                        available=len(matched), at_least=True)
        rows.extend(matched[:PROFILE_WINDOW])
    # Global stable preferences should not disappear just because a busy
    # project has filled the recent-candidate window.
    stable = conn.execute(
        f"""SELECT c.claim_id,c.current_revision {common}
        AND c.project_id IS NULL AND json_array_length(v.payload_json,'$.conditions')=0
        ORDER BY CASE c.kind WHEN 'constraint' THEN 0 ELSE 1 END,
                 v.recorded_from DESC,c.claim_id LIMIT ?""", (*params, PROFILE_WINDOW + 1),
    ).fetchall()
    note_truncation(gaps, "profile_stable", considered=PROFILE_WINDOW,
                    available=len(stable), at_least=True)
    stable = stable[:PROFILE_WINDOW]
    seen = {row[0] for row in rows}
    for row in stable:
        if row[0] not in seen:
            rows.append(row)
            seen.add(row[0])
    exclude = f" AND c.claim_id NOT IN ({','.join('?' for _ in seen)})" if seen else ""
    window = max(0, MAX_BACKGROUND_CANDIDATES - len(rows))
    recent = conn.execute(
        f"""SELECT c.claim_id,c.current_revision {common}{exclude}
        ORDER BY CASE WHEN c.project_id IS NULL THEN 1 ELSE 0 END,
                 CASE c.kind WHEN 'constraint' THEN 0 ELSE 1 END,
                 v.recorded_from DESC,c.claim_id LIMIT ?""",
        (*params, *sorted(seen), window + 1),
    ).fetchall()
    note_truncation(gaps, "profile_recent", considered=window,
                    available=len(recent), at_least=True)
    return [*rows, *recent[:window]]


def background_candidates(tx, context: SearchContext, reader, clock,
                          gaps: list[str] | None = None) -> tuple[tuple[CandidateRef, RetrievedObject], ...]:
    """Select at most two preferences/constraints and one current task.

    Ambiguous task sets are never resolved by recency.  Conditions remain data:
    conditional claims require a positive match to the current query; relative
    one-turn conditions are never revived from a later query. All preference
    subjects remain explicitly attributed.
    """
    if context.mode != "auto" or not context.trusted_context.allowed_scope_ids:
        return ()
    selected: list[tuple[CandidateRef, RetrievedObject]] = []
    choices = []
    terms = set(meaningful_query_terms(context.query))
    rows = _profile_rows(tx, context, gaps)
    for examined, row in enumerate(rows):
        if clock.monotonic() >= context.deadline:
            # Running out of time is not the same as having nothing to say.
            note_truncation(gaps, "background_deadline", considered=examined, available=len(rows))
            break
        effective = select_effective(tx.claims.versions(row[0]), context.now)
        if effective is None:
            continue
        candidate = CandidateRef("claim", row[0], effective.revision, "background", rank=len(selected) + 1)
        obj = reader.hydrate(tx, candidate, context)
        if obj is None or obj.temporal_status != "current" or not obj.evidence_refs:
            continue
        metadata = dict(obj.metadata)
        if metadata.get("state") != "active" or obj.basis not in {"direct_report", "observed"}:
            continue
        payload = json.loads(metadata.get("payload_json", "{}"))
        if not _subject_visible_to_current_principal(payload.get("subject"), context):
            continue
        conditions = payload.get("conditions", [])
        if not _conditions_apply(conditions, context):
            continue
        # Bound background payload independently, then share the final packet
        # byte budget with query evidence.  Large profiles remain explicit tools.
        if len(obj.content.encode("utf-8")) > 768:
            continue
        text = " ".join(str(payload.get(key, "")) for key in ("subject", "predicate", "value_text", "conditions"))
        hits = len(terms.intersection(lexical_terms(text)))
        attribute = (effective.scope_id, effective.project_id, effective.branch_id,
                     payload.get("kind"), payload.get("subject"), payload.get("predicate"))
        # A matching conditional exception comes before the general value for
        # that same attributed property. Different people are never merged.
        priority = (bool(conditions), hits, effective.project_id is not None,
                    payload.get("kind") == "constraint", effective.recorded_from)
        choices.append((priority, attribute, candidate, obj))
    grouped = {}
    for choice in choices:
        grouped.setdefault(choice[1], []).append(choice)
    resolved = []
    for group in grouped.values():
        conditional = [choice for choice in group if choice[0][0]]
        if len(conditional) > 1:
            values = {json.loads(dict(choice[3].metadata)["payload_json"]).get("value_text")
                      for choice in conditional}
            if len(values) > 1:
                # Two applicable exceptions have no proven precedence. Do not
                # silently turn recency or lexical overlap into a decision.
                # Explicit retrieval can still expose the attributed claims.
                continue
        resolved.append(max(conditional or group, key=lambda choice: choice[0]))
    ordered = sorted(resolved, key=lambda choice: choice[0], reverse=True)
    for _priority, _attribute, candidate, obj in ordered:
        selected.append((replace(candidate, rank=len(selected) + 1), mark_background(obj)))
        if len(selected) == 2:
            note_truncation(gaps, "background_slots", considered=2, available=len(ordered))
            break
    episode = current_task_candidate(tx, context, reader, clock)
    if episode is not None:
        selected.append((episode[0], mark_background(episode[1])))
    return tuple(selected)


def current_task_candidate(tx, context: SearchContext, reader, clock) -> tuple[CandidateRef, RetrievedObject] | None:
    """Return one source-grounded active task, refusing ambiguous candidates."""
    if context.mode not in {"auto", "current"} or not context.trusted_context.allowed_scope_ids or clock.monotonic() >= context.deadline:
        return None
    where, params = _audience(context, "e")
    trusted = context.trusted_context
    if trusted.task_anchor:
        from .delete_storage import canonical

        series = tuple(hashlib.sha256(canonical([
            trusted.binding.installation_id, scope, trusted.project_id,
            trusted.branch_id, "task", trusted.task_anchor,
        ]).encode()).hexdigest() for scope in sorted(trusted.allowed_scope_ids))
        where += f" AND e.series_key IN ({','.join('?' for _ in series)})"
        params = (*params, *series)
    rows = tx._check().execute(
        f"""SELECT e.episode_id,e.current_revision FROM episodes e
        JOIN episode_versions v ON v.episode_id=e.episode_id AND v.revision=e.current_revision
        WHERE {where} AND v.state IN ('open','interrupted') AND v.resume_json IS NOT NULL
        ORDER BY v.recorded_at DESC,e.episode_id LIMIT ?""",
        (*params, MAX_BACKGROUND_CANDIDATES + 1),
    ).fetchall()
    # A truncated enumeration cannot establish a unique current task.
    if len(rows) > MAX_BACKGROUND_CANDIDATES:
        return None
    matches = []
    for row in rows:
        if clock.monotonic() >= context.deadline:
            return None
        episode = tx.episodes.get(row[0])
        if episode is None or episode.needs_revalidation:
            # A recorded successful step is not proof that a changed runtime
            # is still in that state.  Leave it to explicit inspection.
            continue
        candidate = CandidateRef("episode", row[0], row[1], "background")
        obj = reader.hydrate(tx, candidate, context)
        if obj is None or not obj.evidence_refs:
            continue
        resume = json.loads(obj.content)
        goal = resume.get("goal")
        if not isinstance(goal, dict) or not goal.get("text") or not goal.get("evidence_refs"):
            continue
        matches.append((candidate, obj))
    return matches[0] if len(matches) == 1 else None
