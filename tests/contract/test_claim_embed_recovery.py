"""A claim's vector work comes back after a provider failed it.

Every claim head is queued for the vector index, and the automatic recovery read every embed's subject as a source: a
claim embed a provider failed was made obsolete instead of reopened.  On the shared store 114 readable heads had an
obsolete embed and no vector, and one head an earlier conversion never queued had none either: recall reached those
claims by their words alone (review of 3.7.4).
"""
from __future__ import annotations

import sqlite3

from scope_recall.core.schema import SCHEMA_VERSION

from test_v11_claims import app, capture, initial  # noqa: F401  (fixtures)


def _embed(core, ref: str, revision: int):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("""SELECT state,last_error_code FROM work_items WHERE work_type='embed'
                               AND subject_ref=? AND subject_revision=?""", (ref, revision)).fetchone()


def _fail_embed(core, ref: str, revision: int, code: str = "network_error", state: str = "failed") -> None:
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""UPDATE work_items SET state=?,attempt=3,last_error_code=? WHERE work_type='embed'
                        AND subject_ref=? AND subject_revision=?""", (state, code, ref, revision))


def _recover(core, ctx) -> int:
    core.clock.now = "2026-09-07T12:00:00Z"  # past the recovery's cooldown
    with core.storage.write(ctx) as tx:
        return tx.work.recover_transient_failures(now=core.clock.utc_now(), allowed_work_types=frozenset({"embed"}))


def _correct(core, ctx) -> None:
    """Supersede ``initial(value="H100", ...)`` with revision 2, as the person's own correction does."""
    capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")


def test_a_claim_embed_a_provider_failed_is_reopened_not_made_obsolete(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision)
    assert _recover(core, ctx) == 1
    assert _embed(core, item.ref, item.revision)[0] == "pending"


def test_an_old_revision_s_failed_embed_is_still_made_obsolete(app):
    core, ctx = app
    item, _source = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    _fail_embed(core, item.ref, item.revision)
    _correct(core, ctx)
    assert core.current_claim(ctx, item.ref).revision == 2
    assert _recover(core, ctx) == 0
    assert _embed(core, item.ref, 1)[0] == "obsolete", "only the head is worth a vector"


def test_retry_failures_brings_back_the_vector_work_the_recovery_dropped(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    preview = core.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["claim_embeds_reopened"], preview["claim_embeds_queued"]) == (1, 0)
    assert _embed(core, item.ref, item.revision)[0] == "obsolete", "a preview changes nothing"
    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["claim_embeds_reopened"] == 1
    assert _embed(core, item.ref, item.revision) == ("pending", f"retried:{SCHEMA_VERSION}|authority_revoked")
    again = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (again["claim_embeds_reopened"], again["claim_embeds_queued"]) == (0, 0)


def test_retry_failures_queues_a_head_that_never_had_vector_work(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM work_items WHERE work_type='embed' AND subject_ref=?", (item.ref,))
    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 1)
    assert _embed(core, item.ref, item.revision)[0] == "pending"


def test_retry_failures_leaves_the_vector_work_of_an_old_revision(app):
    core, ctx = app
    item, _source = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    _correct(core, ctx)
    _fail_embed(core, item.ref, 1, code="authority_revoked", state="obsolete")
    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 0)
    assert _embed(core, item.ref, 1)[0] == "obsolete"
    assert _embed(core, item.ref, 2)[0] == "pending", "the head's own is queued as ever"


def test_retry_failures_leaves_a_head_refused_on_purpose(app):
    """A head whose embed failed for a reason no retry changes stays as it is: only what the recovery dropped."""
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="sensitive_request")
    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 0)
    assert _embed(core, item.ref, item.revision)[0] == "failed"
