"""retire-rootless-claims: the unconfirmed claims tool output left behind, and nothing else.

Until 3.2.0rc6 consolidation derived claims from tool output; on the pilot 2,773 proposals rested on
it alone.  ``retire_rootless_proposals`` retires proposals no derivation root supports and leaves
proved claims, proposals a person supports, and mixed evidence as they are.
"""
from __future__ import annotations

import sqlite3

from scope_recall.core.requalify import ROOTLESS_REASON
from test_v11_claims import accept, app, capture, draft  # noqa: F401 - app is a fixture


def _claims(core, ctx):
    """One claim of each kind the retirement must tell apart."""
    tool = capture(core, ctx, "TEST-project 的配色决定是蓝色。", origin="tool_observation")
    rootless = accept(core, ctx, draft(tool)).items[0]
    seen = capture(core, ctx, "2026年8月1日TEST-project 报价 100单位，这是当日报价。", origin="tool_observation",
                   when="2026-08-01T00:00:00Z")
    proved = accept(core, ctx, draft(seen, "100单位", kind="fact", predicate="报价")).items[0]
    said = capture(core, ctx, "TEST-project 的配色也许是银色？")
    person = accept(core, ctx, draft(said, "银色", predicate="备选配色")).items[0]
    both = capture(core, ctx, "TEST-project 的配色决定是绿色。")
    mixed_draft = draft(both, "绿色", predicate="新配色")
    tool_green = capture(core, ctx, "TEST-project 的配色决定是绿色。", origin="tool_observation")
    mixed_draft["evidence_spans"].append(dict(source_ref=tool_green.ref, source_revision=tool_green.revision,
                                              quote=tool_green.event["content"]))
    mixed = accept(core, ctx, mixed_draft).items[0]
    return rootless, proved, person, mixed


def test_retire_rootless_retires_only_proposals_resting_on_tool_output(app):
    core, ctx = app
    rootless, proved, person, mixed = _claims(core, ctx)
    assert (rootless.state, proved.state) == ("proposed", "active")
    assert person.state == "proposed" and mixed.state in {"proposed", "active"}
    before = sqlite3.connect(core.storage.path).execute("SELECT count(*) FROM claim_versions").fetchone()[0]

    preview = core.retire_rootless_proposals(ctx, limit=32, dry_run=True)
    assert [entry["ref"] for entry in preview["changed"]] == [rootless.ref]
    assert preview["changed"][0]["origins"] == ["tool_observation"] and not preview["applied"]
    assert sqlite3.connect(core.storage.path).execute("SELECT count(*) FROM claim_versions").fetchone()[0] == before
    # Refs and verdicts only: a report is printed by an operator command and never carries claim text.
    assert all(set(entry) <= {"ref", "was", "now", "origins", "revision"} for entry in preview["changed"])

    applied = core.retire_rootless_proposals(ctx, limit=32, dry_run=False)
    assert [entry["ref"] for entry in applied["changed"]] == [rootless.ref] and applied["applied"]
    head = core.claim_history(ctx, rootless.ref)[-1]
    assert (head.state, head.reason) == ("retracted", ROOTLESS_REASON)
    assert core.claim_history(ctx, proved.ref)[-1].state == "active"
    assert core.claim_history(ctx, person.ref)[-1].state == "proposed"
    assert core.claim_history(ctx, mixed.ref)[-1].state == mixed.state
    with sqlite3.connect(core.storage.path) as conn:
        waiting = conn.execute(
            """SELECT count(*) FROM candidate_lifecycle WHERE candidate_ref=? AND candidate_revision<?
               AND processing_state IN ('pending_evaluation','waiting_evidence')""",
            (rootless.ref, head.revision)).fetchone()[0]
        queued = conn.execute("SELECT count(*) FROM candidate_evaluations WHERE candidate_ref=? AND state='queued'",
                              (rootless.ref,)).fetchone()[0]
    assert (waiting, queued) == (0, 0), "a retired proposal no longer waits for an evaluation"
    assert core.retire_rootless_proposals(ctx, limit=32, dry_run=False)["changed"] == []


def test_a_proposal_a_person_has_since_said_is_left_to_its_evaluation(app):
    """A person who says a tool-derived proposal again is heard in its evaluation, not in its own
    evidence: ``apply_claim`` takes the restatement for a duplicate.  Retiring it would drop them."""
    from scope_recall.core.claims import Qualification

    core, ctx = app
    tool = capture(core, ctx, "TEST-project 磁盘剩余 42GB。", origin="tool_observation")
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append("TEST-scope", draft(tool, "42GB", kind="fact", predicate="磁盘剩余"),
                                 Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                 recorded_at=core.clock.utc_now())
        tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now(), schedule_initial=False)
    said = capture(core, ctx, "TEST-project 磁盘剩余 42GB。")
    with sqlite3.connect(core.storage.path) as conn:
        heard = conn.execute("SELECT count(*) FROM candidate_evidence WHERE candidate_ref=? AND source_ref=?",
                             (saved.ref, said.ref)).fetchone()[0]
    assert heard == 1
    preview = core.retire_rootless_proposals(ctx, limit=32, dry_run=True)
    assert preview["changed"] == []
    assert preview["skipped"] == [{"ref": saved.ref, "why": "restated_in_evaluation"}]
    assert core.retire_rootless_proposals(ctx, limit=32, dry_run=False)["changed"] == []
    assert core.claim_history(ctx, saved.ref)[-1].state == "proposed"
