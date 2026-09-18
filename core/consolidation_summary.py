"""Consecutive fragment coverage and grounded, bounded summary assembly."""
from __future__ import annotations

import json

from ..contracts import ContractError, validate_payload


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(values):
    return list({_json(value): value for value in values}.values())


def resume_seed(tx, work_id):
    goals = []
    for row in tx._check().execute("SELECT proposals_json FROM consolidation_fragments WHERE work_id=? ORDER BY start_offset", (work_id,)):
        for p in json.loads(row[0])["resume_proposals"]:
            proposed = _unique([*goals, p["goal"]])
            if len(_json(proposed).encode("utf-8")) <= 2048:
                goals = proposed
    return tuple(goals)


def validate_fragment(tx, value, fence, content):
    # A goal from an earlier *accepted* page may be carried forward. Every
    # other proposed item must be grounded in the current page; final Core
    # qualification still uses the complete original including negations.
    seeds = resume_seed(tx, fence.work_id)
    page = content[fence.chunk.start:fence.chunk.end]
    for proposal in value["resume_proposals"]:
        if proposal["goal"] not in seeds and proposal["goal"]["text"] not in page:
            raise ContractError("DERIVATION_INVALID", "fragment_goal")
        for field in ("decisions", "verified_progress", "open_items", "blockers"):
            if any(item["text"] not in page for item in proposal[field]):
                raise ContractError("DERIVATION_INVALID", "fragment_resume")
        if proposal["next_step"] and proposal["next_step"] not in page:
            raise ContractError("DERIVATION_INVALID", "fragment_next_step")
    if any(p["mention"] not in page for p in value["reference_proposals"]):
        raise ContractError("DERIVATION_INVALID", "fragment_reference")


def stage_fragment(tx, value, fence, now):
    """Store this page's proposals; on the final page, merge every page into one bounded result."""
    conn = tx._check(write=True)
    chunk = fence.chunk
    encoded = _json({k: value[k] for k in ("resume_proposals", "reference_proposals")})
    if len(encoded) > 131072:
        raise ContractError("DERIVATION_INVALID", "fragment_summary_budget")
    conn.execute("INSERT INTO consolidation_fragments VALUES (?,?,?,?,?)", (fence.work_id, chunk.start, chunk.end, chunk.total, encoded))
    result = dict(value, resume_proposals=[], reference_proposals=[])
    if not chunk.final:
        return result
    resumes, references = _covered_fragments(conn, fence.work_id, chunk.total)
    gaps = []
    result["resume_proposals"] = _merge_resumes(resumes, gaps)
    result["reference_proposals"] = _merge_references(references, gaps)
    validate_payload("consolidation_result", result)
    conn.execute("INSERT OR REPLACE INTO consolidation_outcomes VALUES (?,?,?,?)",
                 (fence.work_id, "partial" if gaps else "complete", ",".join(sorted(set(gaps))) or "all_fragments_covered", now))
    conn.execute("DELETE FROM consolidation_fragments WHERE work_id=?", (fence.work_id,))
    return result


def _covered_fragments(conn, work_id, total):
    """Every accepted page in order with no gap or overlap, or the coverage is invalid."""
    rows = conn.execute("SELECT * FROM consolidation_fragments WHERE work_id=? ORDER BY start_offset", (work_id,)).fetchall()
    cursor, resumes, references = 0, [], []
    for row in rows:
        if row["start_offset"] != cursor or row["total"] != total or row["end_offset"] <= cursor:
            raise ContractError("DERIVATION_INVALID", "fragment_coverage")
        cursor = row["end_offset"]
        part = json.loads(row["proposals_json"])
        resumes.extend(part["resume_proposals"])
        references.extend(part["reference_proposals"])
    if cursor != total:
        raise ContractError("DERIVATION_INVALID", "fragment_coverage")
    return resumes, references


def _merge_resumes(resumes, gaps):
    """One resume proposal when every page agreed on the goal, else none."""
    goals = _unique(p["goal"] for p in resumes)
    if len(goals) > 1:
        gaps.append("multiple_goals")
    if len(goals) != 1:
        return []
    merged = dict(resumes[0])
    for field in ("decisions", "verified_progress", "open_items", "blockers", "artifact_refs"):
        limit = 32 if field == "artifact_refs" else 12
        values = _unique(item for p in resumes for item in p[field])
        if len(values) > limit:
            gaps.append("summary_capacity")
        merged[field] = values[:limit]
    steps = _unique((p["next_step"], p["next_step_basis"]) for p in resumes if p["next_step"])
    if len(steps) == 1:
        merged["next_step"], merged["next_step_basis"] = steps[0]
    elif len(steps) > 1:
        merged.update(next_step=None, next_step_basis="unknown")
        gaps.append("next_step_ambiguous")
    return [merged]


def _merge_references(references, gaps):
    """One reference proposal per mention, ambiguous when pages resolved it differently."""
    mentions = {}
    for proposal in references:
        mentions.setdefault(proposal["mention"], []).append(proposal)
    merged_all = []
    for proposals in list(mentions.values())[:16]:
        candidates = _unique(ref for p in proposals for ref in p["candidate_refs"])
        merged = dict(proposals[0], candidate_refs=candidates[:16])
        resolved = _unique(p["resolved_ref"] for p in proposals if p["resolved_ref"] is not None)
        if len(resolved) > 1:
            merged.update(resolved_ref=None, resolution="ambiguous")
            gaps.append("reference_ambiguous")
        elif len(resolved) == 1:
            merged.update(resolved_ref=resolved[0], resolution="resolved")
        if len(candidates) > 16:
            gaps.append("reference_capacity")
            merged.update(resolved_ref=None, resolution="ambiguous")
        merged_all.append(merged)
    if len(mentions) > 16:
        gaps.append("reference_capacity")
    return merged_all


def apply_summary(tx, fence, kind, proposal, scope_id, now):
    apply = tx.episodes.apply_resume if kind == "resume" else tx.references.apply
    if fence is None:
        return apply(proposal, scope_id, now)
    # A worker's summary that does not qualify is dropped and named; the claims
    # accepted beside it stay.  Only paged results did this once: a single page
    # rolled back whole, and another instance paid a second model call for each of 208
    # unqualified goals in one day, losing the page's claims when that failed too.
    conn = tx._check(write=True)
    conn.execute("SAVEPOINT consolidation_summary")
    try:
        item = apply(proposal, scope_id, now)
    except ContractError as exc:
        conn.execute("ROLLBACK TO consolidation_summary")
        if exc.code not in {"DERIVATION_INVALID", "INPUT_INVALID"}:
            raise
        detail = kind + "_qualification_failed"
        if fence.chunk is None:
            conn.execute("INSERT OR REPLACE INTO consolidation_outcomes VALUES (?,?,?,?)", (fence.work_id, "partial", detail, now))
        else:
            conn.execute("UPDATE consolidation_outcomes SET disposition='partial',detail=? WHERE work_id=?", (detail, fence.work_id))
        conn.execute("INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
                     (fence.work_id, fence.lease_token, "summary", exc.code, exc.field, now))
        return None
    finally:
        conn.execute("RELEASE consolidation_summary")
    return item
