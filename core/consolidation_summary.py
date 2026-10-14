"""Consecutive fragment coverage and grounded, bounded summary assembly."""

from __future__ import annotations

import json

from ..contracts import ContractError, validate_payload
from .evidence_quote import resolve_fragment_summaries


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(values):
    return list({_json(value): value for value in values}.values())


def resume_seed(tx, work_id):
    goals = []
    for row in tx._check().execute(
        "SELECT proposals_json FROM consolidation_fragments WHERE work_id=? ORDER BY start_offset", (work_id,)
    ):
        for p in json.loads(row[0])["resume_proposals"]:
            proposed = _unique([*goals, p["goal"]])
            if len(_json(proposed).encode("utf-8")) <= 2048:
                goals = proposed
    return tuple(goals)


def validate_fragment(tx, value, fence, content):
    """Drop what this page proposes but does not ground, and name what was dropped.

    A goal from an earlier *accepted* page may be carried forward.  Every other proposed item must be grounded in the
    current page; final Core qualification still uses the complete original including negations.  A summary that is
    not is dropped, as a whole message's summary is when it does not qualify (``apply_summary``): refusing the page
    for it lost the page's claims, checked on their own, and every page after it.
    """
    seeds = resume_seed(tx, fence.work_id)
    page = content[fence.chunk.start : fence.chunk.end]
    resolve_fragment_summaries(value, page, seeds)
    dropped, resumes = [], []
    for proposal in value["resume_proposals"]:
        field = _ungrounded(proposal, seeds, page)
        if field is None:
            resumes.append(proposal)
        else:
            dropped.append(field)
    references = [p for p in value["reference_proposals"] if p["mention"] in page]
    if len(references) < len(value["reference_proposals"]):
        dropped.append("fragment_reference")
    value["resume_proposals"], value["reference_proposals"] = resumes, references
    return dropped


def _ungrounded(proposal, seeds, page):
    """The field of the first part of a resume proposal the page does not ground, or None."""
    if proposal["goal"] not in seeds and proposal["goal"]["text"] not in page:
        return "fragment_goal"
    for field in ("decisions", "verified_progress", "open_items", "blockers"):
        if any(item["text"] not in page for item in proposal[field]):
            return "fragment_resume"
    if proposal["next_step"] and proposal["next_step"] not in page:
        return "fragment_next_step"
    return None


def stage_fragment(tx, value, fence, now, *, dropped=()):
    """Store this page's proposals and what it dropped; on the final page, merge every page into one bounded result,
    partial when any page dropped a summary."""
    conn = tx._check(write=True)
    chunk = fence.chunk
    staged = {k: value[k] for k in ("resume_proposals", "reference_proposals")}
    encoded = _json({**staged, "dropped": sorted(set(dropped))} if dropped else staged)
    if len(encoded) > 131072:
        raise ContractError("DERIVATION_INVALID", "fragment_summary_budget")
    conn.execute(
        "INSERT INTO consolidation_fragments VALUES (?,?,?,?,?)",
        (fence.work_id, chunk.start, chunk.end, chunk.total, encoded),
    )
    result = dict(value, resume_proposals=[], reference_proposals=[])
    if not chunk.final:
        return result
    resumes, references, gaps = _covered_fragments(conn, fence.work_id, chunk.total)
    result["resume_proposals"] = _merge_resumes(resumes, gaps)
    result["reference_proposals"] = _merge_references(references, gaps)
    validate_payload("consolidation_result", result)
    conn.execute(
        "INSERT OR REPLACE INTO consolidation_outcomes VALUES (?,?,?,?)",
        (fence.work_id, "partial" if gaps else "complete", ",".join(sorted(set(gaps))) or "all_fragments_covered", now),
    )
    conn.execute("DELETE FROM consolidation_fragments WHERE work_id=?", (fence.work_id,))
    return result


def _covered_fragments(conn, work_id, total):
    """Every accepted page in order with no gap or overlap, or the coverage is invalid; with what the pages dropped."""
    rows = conn.execute(
        "SELECT * FROM consolidation_fragments WHERE work_id=? ORDER BY start_offset", (work_id,)
    ).fetchall()
    cursor, resumes, references, dropped = 0, [], [], []
    for row in rows:
        if row["start_offset"] != cursor or row["total"] != total or row["end_offset"] <= cursor:
            raise ContractError("DERIVATION_INVALID", "fragment_coverage")
        cursor = row["end_offset"]
        part = json.loads(row["proposals_json"])
        resumes.extend(part["resume_proposals"])
        references.extend(part["reference_proposals"])
        dropped.extend(part.get("dropped", ()))
    if cursor != total:
        raise ContractError("DERIVATION_INVALID", "fragment_coverage")
    return resumes, references, sorted(set(dropped))


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


def mark_partial(conn, work_id, detail, now):
    """A consolidation's outcome is partial, with ``detail`` added to what it already names: each part a result
    lost (a page's summary, a claim, the whole message's summary) stays named, whichever was recorded first."""
    row = conn.execute("SELECT detail FROM consolidation_outcomes WHERE work_id=?", (work_id,)).fetchone()
    details = set(row[0].split(",")) - {"all_fragments_covered"} if row is not None else set()
    conn.execute(
        "INSERT OR REPLACE INTO consolidation_outcomes VALUES (?,?,?,?)",
        (work_id, "partial", ",".join(sorted(details | {detail})), now),
    )


def apply_summary(tx, fence, kind, proposal, scope_id, now):
    apply = tx.episodes.apply_resume if kind == "resume" else tx.references.apply
    if fence is None:
        return apply(proposal, scope_id, now)
    # A worker's summary that does not qualify is dropped and named; the claims
    # accepted beside it stay.  Rolled back whole, a single page would cost a
    # second model call for each unqualified goal (hundreds a day in one store),
    # losing the page's claims when that failed too.
    conn = tx._check(write=True)
    conn.execute("SAVEPOINT consolidation_summary")
    try:
        item = apply(proposal, scope_id, now)
    except ContractError as exc:
        conn.execute("ROLLBACK TO consolidation_summary")
        if exc.code not in {"DERIVATION_INVALID", "INPUT_INVALID"}:
            raise
        mark_partial(conn, fence.work_id, kind + "_qualification_failed", now)
        conn.execute(
            "INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
            (fence.work_id, fence.lease_token, "summary", exc.code, exc.field, now),
        )
        return None
    finally:
        conn.execute("RELEASE consolidation_summary")
    return item
