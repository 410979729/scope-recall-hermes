"""A claim's vector work comes back after a provider failed it.

Every claim head is queued for the vector index, and the automatic recovery read every embed's subject as a source: a
claim embed a provider failed was made obsolete instead of reopened.  On the shared store 114 readable heads had an
obsolete embed and no vector, and one head an earlier conversion never queued had none either: recall reached those
claims by their words alone (review of 3.7.4).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from scope_recall.contracts import ContractError
from scope_recall.core.schema import SCHEMA_VERSION
from test_claims import app, capture, initial  # noqa: F401  (fixtures)
from test_deletion import authorize, request


def _embed(core, ref: str, revision: int):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            """SELECT state,last_error_code FROM work_items WHERE work_type='embed'
                               AND subject_ref=? AND subject_revision=?""",
            (ref, revision),
        ).fetchone()


def _fail_embed(core, ref: str, revision: int, code: str = "network_error", state: str = "failed") -> None:
    """The shape the recovery met: three attempts, each under a lease of its own."""
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            """UPDATE work_items SET state=?,attempt=3,lease_token=3,last_error_code=? WHERE work_type='embed'
                        AND subject_ref=? AND subject_revision=?""",
            (state, code, ref, revision),
        )


def _counts(report) -> tuple[int, int]:
    return report["claim_embeds_reopened"], report["claim_embeds_queued"]


def _rows(core, ref: str):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            """SELECT subject_revision,state,last_error_code,attempt FROM work_items
                               WHERE work_type='embed' AND subject_ref=? ORDER BY subject_revision""",
            (ref,),
        ).fetchall()


class Port:
    """An embedding port for single subjects; with ``fail`` it refuses every request so."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.written: list[tuple] = []

    def _prepare(self, kind, subject):
        if self.fail:
            raise ContractError(self.fail)
        return (kind, subject.ref, subject.revision)

    def prepare_source(self, source, *, remaining_seconds=1.0):
        return self._prepare("source", source)

    def prepare_claim(self, claim, *, remaining_seconds=1.0):
        return self._prepare("claim", claim)

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert lease_guard()
        self.written.append(prepared)

    def publish_claim(self, prepared, *, claim, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert lease_guard()
        self.written.append(prepared)


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
    preview = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["claim_embeds_reopened"], preview["claim_embeds_queued"]) == (1, 0)
    assert _embed(core, item.ref, item.revision)[0] == "obsolete", "a preview changes nothing"
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["claim_embeds_reopened"] == 1
    assert _embed(core, item.ref, item.revision) == ("pending", f"retried:{SCHEMA_VERSION}|authority_revoked")
    again = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (again["claim_embeds_reopened"], again["claim_embeds_queued"]) == (0, 0)


def test_retry_failures_queues_a_head_that_never_had_vector_work(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM work_items WHERE work_type='embed' AND subject_ref=?", (item.ref,))
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 1)
    assert _embed(core, item.ref, item.revision)[0] == "pending"


def test_retry_failures_leaves_the_vector_work_of_an_old_revision(app):
    core, ctx = app
    item, _source = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    _correct(core, ctx)
    _fail_embed(core, item.ref, 1, code="authority_revoked", state="obsolete")
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 0)
    assert _embed(core, item.ref, 1)[0] == "obsolete"
    assert _embed(core, item.ref, 2)[0] == "pending", "the head's own is queued as ever"


def test_retry_failures_leaves_a_head_refused_on_purpose(app):
    """A head whose embed failed for a reason no retry changes stays as it is: only what the recovery dropped."""
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="sensitive_request")
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (report["claim_embeds_reopened"], report["claim_embeds_queued"]) == (0, 0)
    assert _embed(core, item.ref, item.revision)[0] == "failed"


def test_a_deleted_head_gets_no_vector_work_back(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    authorize(core, ctx, item)
    core.forget(ctx, request(item), remaining_seconds=10)
    assert _counts(core.operations.retry_failed_work(ctx, limit=64, dry_run=False)) == (0, 0)
    assert _embed(core, item.ref, item.revision)[0] == "obsolete"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM work_items WHERE work_type='embed' AND subject_ref=?", (item.ref,))
    assert _counts(core.operations.retry_failed_work(ctx, limit=64, dry_run=False)) == (0, 0), "nor is one queued"


def test_a_deleted_claim_s_failed_embed_is_still_made_obsolete(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision)
    authorize(core, ctx, item)
    core.forget(ctx, request(item), remaining_seconds=10)
    _recover(core, ctx)
    assert _embed(core, item.ref, item.revision) == ("obsolete", "authority_revoked")


def test_a_muted_head_gets_its_vector_work_back_as_every_head_has_it(app):
    """Muted (suppressed, still readable on request): the live path queues every head's vector work, and automatic
    recall leaves a muted claim out on its own."""
    core, ctx = app
    item, _source = initial(core, ctx)
    authorize(core, ctx, item, mode="suppress")
    core.forget(ctx, request(item, mode="suppress"), remaining_seconds=10)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    reopened = _counts(core.operations.retry_failed_work(ctx, limit=64, dry_run=True))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM work_items WHERE work_type='embed' AND subject_ref=?", (item.ref,))
    queued = _counts(core.operations.retry_failed_work(ctx, limit=64, dry_run=True))
    assert (reopened, queued) == ((1, 0), (0, 1))


def test_another_project_s_context_touches_nothing(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    assert _counts(
        core.operations.retry_failed_work(replace(ctx, project_id="TEST-other"), limit=64, dry_run=False)
    ) == (0, 0)
    assert _embed(core, item.ref, item.revision)[0] == "obsolete"


def _clone_claim(core, ref: str, new_ref: str, *, project_id) -> None:
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        claim = dict(conn.execute("SELECT * FROM claims WHERE claim_id=?", (ref,)).fetchone())
        versions = [dict(row) for row in conn.execute("SELECT * FROM claim_versions WHERE claim_id=?", (ref,))]
        claim.update(claim_id=new_ref, slot_key="TEST-slot-" + new_ref, project_id=project_id, branch_id=project_id)
        for version in versions:
            version["claim_id"] = new_ref
            conn.execute(
                f"INSERT INTO claim_versions({','.join(version)}) VALUES ({','.join('?' * len(version))})",
                tuple(version.values()),
            )
        conn.execute(
            f"INSERT INTO claims({','.join(claim)}) VALUES ({','.join('?' * len(claim))})", tuple(claim.values())
        )


def test_heads_another_context_owns_never_hide_one_this_context_takes(app):
    """A page scanned claims in order and checked their context afterwards: eight heads with no project, ahead of an
    eligible one, filled every page with refusals (review of 3.7.5)."""
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    clones = [f"claim-{index:064d}" for index in range(8)]
    assert all(clone < item.ref for clone in clones)
    for clone in clones:
        _clone_claim(core, item.ref, clone, project_id=None)
    assert _counts(core.operations.retry_failed_work(ctx, limit=1, dry_run=False)) == (1, 0)
    assert _embed(core, item.ref, item.revision)[0] == "pending"


def test_a_reopened_head_corrected_before_the_worker_reaches_it_is_embedded_at_both_revisions(app):
    """The reopened row has held leases, so a correction spares it (3.7.4); the old revision costs one more vector."""
    core, ctx = app
    item, _source = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    assert _counts(core.operations.retry_failed_work(ctx, limit=64, dry_run=False)) == (1, 0)
    _correct(core, ctx)
    assert [row[1] for row in _rows(core, item.ref)] == ["pending", "pending"]
    port = Port()
    core.drain_worker(ctx, max_items=64, remaining_seconds=30, owner_id="TEST-w", embed=port)
    assert [row[1] for row in _rows(core, item.ref)] == ["done", "done"]
    assert sorted(entry for entry in port.written if entry[0] == "claim") == [
        ("claim", item.ref, 1),
        ("claim", item.ref, 2),
    ]


def test_a_reopened_head_that_keeps_failing_stops_and_is_never_made_obsolete(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    _fail_embed(core, item.ref, item.revision, code="authority_revoked", state="obsolete")
    core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    start = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    history = []
    for step in range(11):
        core.clock.now = (start + timedelta(hours=2 * step)).isoformat().replace("+00:00", "Z")
        if step >= 8:
            core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
        core.drain_worker(
            ctx, max_items=64, remaining_seconds=30, owner_id=f"TEST-f{step}", embed=Port(fail="network_error")
        )
        history.append(_rows(core, item.ref)[0][1:4])
    assert all(state != "obsolete" for state, _code, _attempt in history), history
    assert history[-1][0] == "failed" and history[-1][2] <= 8, history
