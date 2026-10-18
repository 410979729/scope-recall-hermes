"""One bounded re-look at failed work per release, granted by an operator, stamped once.

Covers ``core/failure_retry.py``, ``work.retry_failed`` and the terminal count
the store's status gives the worker and the doctor.  The properties that matter: a fault can
be cleared so an instance can return to healthy, a by-design terminal outcome
is not re-run by accident, and nothing can loop.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from scope_recall._version import __version__
from scope_recall.contracts import ContractError
from scope_recall.core.failure_retry import (
    AUTO_RECOVERABLE_ERRORS,
    RETRY_GENERATION,
    RETRY_MARKER,
    TERMINAL_FAILURES,
    already_retried,
    failure_kind,
    marked,
    retry_class,
    selects,
)
from scope_recall.runtime.models import AuxiliaryModelError
from test_candidate_lifecycle import _candidate, _candidate_rows, _finish_source_work
from test_claims import app, capture
from test_consolidation_chunks import FIRST, LAST, claims, long_source, proposal, row
from test_worker import FakeConsolidation, consolidation_payload, worker_app

# --------------------------------------------------------------------------
# Reading a decorated error code
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,kind",
    [
        ("timeout", "timeout"),
        ("auto_retry:1|timeout", "timeout"),
        ("budget_checked:1108|input_invalid", "input_invalid"),
        ("DERIVATION_INVALID", "derivation_invalid"),
        ("", ""),
        (None, ""),
    ],
)
def test_the_kind_is_the_last_segment(code, kind):
    """Codes accumulate history; the failure itself is always on the end."""
    assert failure_kind(code) == kind


@pytest.mark.parametrize("kind", sorted(TERMINAL_FAILURES))
def test_by_design_outcomes_need_the_explicit_flag(kind):
    assert retry_class(kind) == "terminal"
    assert selects(kind, include_terminal=False, generation=RETRY_GENERATION) is False
    assert selects(kind, include_terminal=True, generation=RETRY_GENERATION) is True


@pytest.mark.parametrize(
    "code",
    [
        # An embedding route's unusable answer (``runtime/embedding_models.py``).
        "vector_dimension_mismatch",
        "vector_nonfinite",
        "vector_zero",
        "unsupported_response_shape",
        # The HTTP helper's and a model client's own codes, a status and codes no list names, one with history.
        "transport_worker_protocol",
        "invalid_json",
        "http_4220",
        "something_else",
        "auto_retry:2|a_code_no_release_has_written",
        "",
        None,
    ],
)
def test_a_failure_no_list_names_is_a_fault(code):
    """Only by-design outcomes are listed.  The worker records whatever code an adapter raises, and the status counts
    every failed row outside that list as a fault: one this command could not select stayed failed for good."""
    assert retry_class(code) == "actionable"
    assert selects(code, include_terminal=False, generation=RETRY_GENERATION) is True


def test_the_re_look_is_the_release_s():
    assert RETRY_GENERATION == __version__


def test_a_row_stamped_by_this_release_is_skipped():
    """The stamp is what stops a second run from doing the same work again."""
    stamped = marked("timeout", generation=RETRY_GENERATION)
    assert stamped == f"{RETRY_MARKER}:{RETRY_GENERATION}|timeout"
    assert already_retried(stamped, generation=RETRY_GENERATION) is True
    assert selects(stamped, include_terminal=True, generation=RETRY_GENERATION) is False


def test_a_stamp_from_another_release_does_not_block():
    """A later release is a later decision; it may grant its own re-look."""
    stamped = marked("timeout", generation="0.0.1")
    assert already_retried(stamped, generation=RETRY_GENERATION) is False
    assert selects(stamped, include_terminal=False, generation=RETRY_GENERATION) is True
    assert already_retried(stamped, generation="0.0.11") is False, "a release is matched whole"


def test_a_schema_generation_stamp_never_blocks_a_release():
    """#233: rows re-opened under schema 1110 before a fix kept a stamp that blocked every later release keeping the
    schema, the one that fixed their validation included.  The code is one of the two items reported there."""
    code = (
        "retried:1110|derivation_retry:1|retried:1109|auto_retry:2|timeout|timeout|"
        "DERIVATION_INVALID|DERIVATION_INVALID|DERIVATION_INVALID"
    )
    assert already_retried(code, generation=RETRY_GENERATION) is False
    assert selects(code, include_terminal=False, generation=RETRY_GENERATION) is False
    assert selects(code, include_terminal=True, generation=RETRY_GENERATION) is True
    assert marked(code, generation=RETRY_GENERATION) == (
        f"retried:{RETRY_GENERATION}|derivation_retry:1|auto_retry:2|timeout|timeout|"
        "DERIVATION_INVALID|DERIVATION_INVALID|DERIVATION_INVALID"
    )


def test_a_new_stamp_replaces_the_earlier_one_and_keeps_the_rest():
    """Only this release's stamp decides; stacked, one per release, the stamps would push the failure kind off the
    end of the bounded code.  Every other mark, and a stamp kept behind an operator retry's ``prior:``, stays."""
    again = marked(marked("auto_retry:1|timeout", generation="3.10.0"), generation="3.10.1")
    assert again == "retried:3.10.1|auto_retry:1|timeout"
    assert failure_kind(again) == "timeout"
    behind = "operator_retry:TEST-op|prior:retried:3.10.0|derivation_retry:1|derivation_invalid"
    assert marked(behind, generation="3.10.1") == f"retried:3.10.1|{behind}"
    long = "derivation_retry:1|" + "timeout|" * 120 + "derivation_invalid"
    assert len(long) + 4 * len("retried:3.10.1|") > 1024, "four stamps stacked would cut the kind off"
    stamped = long
    for release in ("3.10.1", "3.10.2", "3.11.0", "4.0.0"):
        stamped = marked(stamped, generation=release)
    assert stamped == f"retried:4.0.0|{long}"
    assert retry_class(stamped) == "terminal"


def test_a_code_past_the_bound_keeps_its_marks_and_its_kind():
    """A release stamp longer than the schema's pushed a full code past the bound, and the cut took the end of the
    failure kind (``derivation_inval``: no class, no doctor count; audit of 3.10.1).  Past the bound the history
    between the marks and the kind gives way, and every budget mark stays, the automatic count among them: cut in
    the middle, a count there was lost and its budget began again (audit of 3.10.1)."""
    from scope_recall.core.work_storage import CODE_BOUND, _auto_count, _operator_retry_ids, _within

    code = "retried:1110|derivation_retry:1|" + "timeout|" * 120 + "network_error|derivation_invalid"
    assert len(code) == CODE_BOUND
    shortened = _within(marked(code, generation="3.10.1"))
    assert shortened == "retried:3.10.1|derivation_retry:1|derivation_invalid"
    deferred = "operator_retry:TEST-op|prior:auto_retry:3|interrupted_retry:1|timeout|" + "http_402|" * 200 + "http_402"
    assert _within(deferred) == "operator_retry:TEST-op|interrupted_retry:1|auto_retry:3|http_402"
    assert (_auto_count(_within(deferred)), _operator_retry_ids(_within(deferred))) == (3, ("TEST-op",))
    assert _within("short|code") == "short|code"
    # A kind longer than any the worker writes no longer pushes the marks out (audit of 3.10.1).
    assert _within("operator_retry:TEST-op|prior:" + "x" * CODE_BOUND) == "operator_retry:TEST-op|" + "x" * 128
    # The one repair of an old oversized consolidation is a budget too: kept, it is not given again.
    repaired = "chunk_upgrade:1106|INPUT_INVALID|" + "http_402|" * 120 + "http_402"
    assert _within(repaired) == "chunk_upgrade:1106|http_402"


def test_a_stamp_kept_behind_an_explicit_retry_still_counts():
    """An explicit retry of one item keeps the code it found behind ``prior:``; the release's stamp there was not
    seen, and a second run of the same release re-opened the row again (audit of 3.10.1)."""
    from scope_recall.core.work_storage import _budget_marks, _preserve_retry_history

    code = "operator_retry:TEST-op|prior:retried:3.10.1|http_400|http_400"
    assert already_retried(code, generation="3.10.1") and not already_retried(code, generation="3.10.2")
    assert _budget_marks(code) == "operator_retry:TEST-op|retried:3.10.1|"
    assert _preserve_retry_history("retried:3.10.1|http_400") and not _preserve_retry_history("http_400")
    assert _preserve_retry_history("chunk_upgrade:1106|INPUT_INVALID")
    assert not _preserve_retry_history("chunk_checked:1106|INPUT_INVALID")
    assert not _preserve_retry_history("budget_checked:1110|input_invalid")
    assert _budget_marks("http_400|sretried:3.10.1x|timeout") == "", "a mark inside another segment is no mark"


@pytest.mark.parametrize(
    "code,rebuilt",
    [
        ("retried:3.10.1|timeout", "retried:3.10.1|auto_retry:1|timeout"),
        (
            "derivation_retry:1|retried:3.10.1|derivation_invalid|timeout",
            "derivation_retry:1|retried:3.10.1|auto_retry:1|timeout",
        ),
        (
            "retried:3.10.1|operator_retry:TEST-op|prior:timeout|timeout",
            "retried:3.10.1|operator_retry:TEST-op|auto_retry:1|timeout",
        ),
        (
            "retried:3.10.1|interrupted_retry:1|candidate_attempt_interrupted|timeout",
            "retried:3.10.1|interrupted_retry:1|auto_retry:1|timeout",
        ),
        ("auto_retry:1|timeout", "auto_retry:2|timeout"),
        ("derivation_retry:1|auto_retry:1|timeout", "derivation_retry:1|auto_retry:2|timeout"),
    ],
)
def test_an_automatic_recovery_keeps_every_budget_mark(code, rebuilt):
    """Rebuilt from its count, the code lost the release's stamp, so the release re-opened the row again, and the
    interrupted attempt's mark, so a second interrupted retry was granted (audits of 3.10.1)."""
    from scope_recall.core.work_storage import _auto_retry_stamp

    assert _auto_retry_stamp(code) == rebuilt


# --------------------------------------------------------------------------
# Against real storage
# --------------------------------------------------------------------------


class _Failing:
    def __init__(self, code="network_error"):
        self.code = code

    def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
        # Any failure of the call fails the item; _fail_one then records the code it was asked for.
        raise RuntimeError(self.code)


def _fail_one(core, ctx, code="timeout"):
    """Drive one candidate evaluation to a failed work item through the worker."""
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=_Failing(code))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET last_error_code=? WHERE state='failed'", (code,))
        conn.commit()
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert any(row["state"] == "failed" for row in work), work
    return work


def _states(core):
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        work = dict(conn.execute("SELECT state,count(*) FROM work_items GROUP BY 1").fetchall())
        evaluations = dict(conn.execute("SELECT state,count(*) FROM candidate_evaluations GROUP BY 1").fetchall())
    return work, evaluations


def test_a_fault_is_cleared_and_its_three_tables_move_together(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    assert _states(core)[0].get("failed")

    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("timeout")
    work, evaluations = _states(core)
    assert not work.get("failed")
    with sqlite3.connect(core.storage.path) as conn:
        mismatched = conn.execute(
            """SELECT count(*) FROM candidate_evaluations e JOIN work_items w ON w.work_id=e.work_id
               WHERE w.state='pending' AND e.state<>'queued'"""
        ).fetchone()[0]
    assert mismatched == 0, "a re-queued work item whose evaluation stayed failed dies immediately"


def test_a_second_pass_does_nothing(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] >= 1
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0


def test_the_preview_writes_nothing(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    before = _states(core)
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert report["retried"] >= 1 and report["applied"] is False
    assert _states(core) == before


def test_a_terminal_failure_is_left_alone_without_the_flag(app):
    core, ctx = app
    _fail_one(core, ctx, "derivation_invalid")
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0
    assert _states(core)[0].get("failed")
    assert core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)["retried"] >= 1
    assert not _states(core)[0].get("failed")


def test_a_refusal_by_the_providers_policy_is_asked_once_and_re_run_only_on_request(worker_app):
    """Asking again would send the same request to the same policy: the item fails on its first refusal, the status
    counts it as by design, and only a run that includes the terminal failures re-opens it."""
    core, ctx, clock = worker_app
    source = long_source(core, ctx, "TEST 内容策略拒绝了这一条。")
    asked = []

    class Refusing:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
            asked.append(sources)
            raise AuxiliaryModelError("model_refused")

    for minute in range(1, 5):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{minute:02d}:30Z")
        core.drain_worker(ctx, consolidation=Refusing(), max_items=2, remaining_seconds=5)
    assert len(asked) == 1 and row(core, source) == ("failed", 0, 1)
    status = core.status(ctx)
    assert (status.failed_work, status.terminal_failed_work) == (1, 1)
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=True)["retried"] == 0
    every = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=True)
    assert (every["retried"], every["by_kind"]) == (1, {"model_refused": 1})


def test_a_candidate_pass_stops_at_the_first_interrupted_answer_and_a_plain_run_re_opens_the_one_it_failed(app):
    """The provider that interrupted one answer interrupts the next, so the pass asks once and the other evaluation
    waits for the next pass untouched.  A candidate evaluation is asked once: the one asked is left failed, and a
    plain run moves its three tables back together."""
    asked = []

    class Interrupted:
        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            asked.append(candidate)
            raise AuxiliaryModelError("model_output_interrupted")

    core, ctx = app
    _candidate(core, ctx, key="TEST-cand/1")
    _candidate(core, ctx, key="TEST-cand/2", value="红色")
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Interrupted())
    assert len(asked) == 1
    assert list(_codes(core).values()) == ["model_output_interrupted"]
    work, evaluations = _states(core)
    assert (work.get("failed"), evaluations.get("failed")) == (1, 1)
    preview = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["retried"], preview["by_kind"]) == (1, {"model_output_interrupted": 1})
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 1
    work, evaluations = _states(core)
    assert not work.get("failed") and not evaluations.get("failed")


def _codes(core):
    with sqlite3.connect(core.storage.path) as conn:
        return dict(conn.execute("SELECT work_id,last_error_code FROM work_items WHERE last_error_code IS NOT NULL"))


def test_a_row_re_opened_under_the_schema_stamp_is_re_opened_by_this_release(app):
    """#233: its re-look spent before the fix, a row stayed failed through every release that kept the schema."""
    core, ctx = app
    _fail_one(core, ctx, "retried:1110|derivation_retry:1|derivation_invalid")
    report = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)
    assert (report["retried"], report["already_retried"], report["already_retried_work_ids"]) == (1, 0, [])
    assert not _states(core)[0].get("failed")
    (code,) = _codes(core).values()
    assert code == f"retried:{RETRY_GENERATION}|derivation_retry:1|derivation_invalid"


def test_a_row_this_release_re_opened_is_counted_until_the_next_one(app, monkeypatch):
    """Failed again after this release re-opened it, a row is not re-opened by it twice, but the run says so and names
    it (for ``worker_entry --retry-failed``); a request that does not cover it does not count it; the next release
    re-opens it."""
    from scope_recall.core import work_storage

    core, ctx = app
    _fail_one(core, ctx, "derivation_retry:1|derivation_invalid")
    assert core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)["retried"] == 1
    (work_id,) = _codes(core)
    with sqlite3.connect(core.storage.path) as conn:
        # Its next attempt fails as the last did, in all three tables.
        conn.execute(
            "UPDATE work_items SET state='failed',last_error_code=last_error_code||'|derivation_invalid' "
            "WHERE work_id=?",
            (work_id,),
        )
        conn.execute("UPDATE candidate_evaluations SET state='failed' WHERE work_id=?", (work_id,))
        conn.commit()
    covered = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=True)
    assert (covered["retried"], covered["already_retried"], covered["already_retried_work_ids"]) == (0, 1, [work_id])
    uncovered = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (uncovered["retried"], uncovered["already_retried"], uncovered["already_retried_work_ids"]) == (0, 0, [])
    monkeypatch.setattr(work_storage, "MAX_OPERATOR_PAGE", 0)
    capped = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=True)
    assert (capped["already_retried"], capped["already_retried_work_ids"]) == (1, []), "ids beyond one retry's page"
    monkeypatch.setattr(work_storage, "RETRY_GENERATION", "99.0.0")
    later = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)
    assert (later["retried"], later["already_retried"]) == (1, 0)
    assert _codes(core)[work_id] == "retried:99.0.0|derivation_retry:1|derivation_invalid|derivation_invalid"


def _claim_and(core, ctx, *, error_code, recoverable):
    """Lease the one pending row and fail it, as a worker would."""
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        (item,) = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
        return tx.work.fail(
            item.work_id,
            item.lease_token,
            item.lease_owner,
            error_code=error_code,
            now=core.clock.utc_now(),
            recoverable=recoverable,
        )


def test_a_row_this_release_re_opened_keeps_its_stamp_through_its_next_failure(app):
    """A re-opened ``http_400`` that failed again had its code replaced, stamp and all, and each later run of the same
    release re-opened it again, as often as anyone ran it (audit of 3.10.1)."""
    core, ctx = app
    _fail_one(core, ctx, "http_400")
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 1
    assert _claim_and(core, ctx, error_code="http_400", recoverable=False).state == "failed"
    (code,) = _codes(core).values()
    assert code == f"retried:{RETRY_GENERATION}|http_400|http_400"
    again = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert (again["retried"], again["already_retried"]) == (0, 1)


def _pending_with(core, code):
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE work_items SET state='pending',attempt=0,last_error_code=? WHERE last_error_code IS NOT NULL",
            (code,),
        )
        conn.commit()


@pytest.mark.parametrize(
    "prior,rebuilt",
    [
        ("retried:{release}|timeout", "retried:{release}|capacity:1|http_429"),
        ("capacity:1|http_429", "capacity:2|http_429"),
        ("derivation_retry:1|capacity:2|http_429", "derivation_retry:1|capacity:3|http_429"),
        (
            "operator_retry:TEST-op|prior:retried:{release}|interrupted_retry:1|auto_retry:3|http_400",
            "operator_retry:TEST-op|retried:{release}|interrupted_retry:1|auto_retry:3|capacity:1|http_429",
        ),
    ],
)
def test_a_provider_refusing_everyone_keeps_every_budget_mark(app, prior, rebuilt):
    """A capacity refusal rebuilt the code from its count with the derivation mark alone: the release's stamp, an
    explicit retry's token, the interrupted attempt's mark and the automatic count went, and each budget began again
    (audits of 3.10.1).  The count of refusals in a row still replaces the last."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    _pending_with(core, prior.format(release=RETRY_GENERATION))
    assert _claim_and(core, ctx, error_code="http_429", recoverable=True).state == "pending"
    (code,) = _codes(core).values()
    assert code == rebuilt.format(release=RETRY_GENERATION)


def test_a_failure_and_a_deferral_past_the_bound_keep_the_marks_and_the_new_kind(app):
    """Added to a long history, a failure or a deferral was cut off the end of the bounded code, so the code kept a
    stale or broken kind (audits of 3.10.1)."""
    from scope_recall.core.work_storage import CODE_BOUND

    core, ctx = app
    _fail_one(core, ctx, "timeout")
    stamp = f"retried:{RETRY_GENERATION}|"
    long = f"{stamp}auto_retry:2|" + "timeout|" * 130 + "timeout"
    _pending_with(core, long)
    assert _claim_and(core, ctx, error_code="http_400", recoverable=False).state == "failed"
    (code,) = _codes(core).values()
    assert code == f"{stamp}auto_retry:2|http_400"
    _pending_with(core, long)
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        (item,) = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
        tx.work.defer_without_attempt(
            item.work_id, item.lease_token, item.lease_owner, now=core.clock.utc_now(), error_code="http_402"
        )
    (code,) = _codes(core).values()
    assert code == f"{stamp}auto_retry:2|http_402" and len(code) <= CODE_BOUND


def test_a_lost_lease_keeps_every_budget_mark_within_the_bound(app):
    """A lease that ran out with the attempts spent replaced a code that led with the release's stamp, taking the
    stamp and the automatic count with it, and one it kept could pass the bound (audits of 3.10.1)."""
    from scope_recall.core.work_storage import CODE_BOUND

    core, ctx = app
    _fail_one(core, ctx, "timeout")
    stamp = f"retried:{RETRY_GENERATION}|"
    for prior, after in (
        (f"{stamp}auto_retry:3|timeout", f"{stamp}auto_retry:3|timeout|lease_exhausted"),
        (f"{stamp}auto_retry:3|" + "timeout|" * 125 + "timeout", f"{stamp}auto_retry:3|lease_exhausted"),
        ("timeout", "lease_exhausted"),
    ):
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute(
                "UPDATE work_items SET state='leased',attempt=3,lease_owner='TEST-gone',"
                "lease_until='2000-01-01T00:00:00Z',last_error_code=? WHERE last_error_code IS NOT NULL",
                (prior,),
            )
            conn.commit()
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            assert tx.work.release_stale(core.clock.utc_now()) == 1
        (code,) = _codes(core).values()
        assert code == after and len(code) <= CODE_BOUND


def test_a_long_message_refused_on_a_later_page_resumes_there_once_re_opened(worker_app):
    """#233's items: a consolidation refused on its second page, its re-look spent under the schema stamp, resumes on
    that page when this release re-opens it, keeps the page it had accepted, and finishes."""
    core, ctx, _clock = worker_app
    source = long_source(core, ctx, FIRST + "\n" + "这是一段归档资料；" * 700 + "\n" + LAST)

    def drain(model, until):
        for _ in range(40):
            if row(core, source)[0] == until:
                return
            with sqlite3.connect(core.storage.path) as conn:
                conn.execute("UPDATE work_items SET available_at=? WHERE state='pending'", (core.clock.utc_now(),))
            core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
        raise AssertionError(row(core, source))

    def quoting_the_first(sources, **_):
        # Right on the first page; on the second, evidence from another page, refused.
        return consolidation_payload(sources[0], claims=[proposal(sources[0], FIRST)])

    drain(FakeConsolidation(quoting_the_first), "failed")
    _state, offset, _attempt = row(core, source)
    assert 0 < offset < len(source.event["content"])
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET last_error_code='retried:1110|'||last_error_code WHERE state='failed'")
        conn.commit()
    assert retry_class(next(iter(_codes(core).values()))) == "terminal"
    report = core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)
    assert (report["retried"], report["by_kind"]) == (1, {"DERIVATION_INVALID": 1})

    def fixed(sources, **_):
        page = sources[0]
        found = [proposal(page, FIRST)] if FIRST in page.event["content"] else []
        if LAST in page.event["content"]:
            found.append(proposal(page, LAST, value="深色", predicate="主题"))
        return consolidation_payload(page, claims=found)

    drain(FakeConsolidation(fixed), "done")
    assert row(core, source)[1] == len(source.event["content"])
    assert {payload["value_text"] for payload, _state in claims(core)} == {"蓝色", "深色"}


@pytest.mark.parametrize("limit", [0, -1, 257, 1.0, True])
def test_the_page_is_bounded(app, limit):
    core, ctx = app
    with pytest.raises(ContractError):
        core.operations.retry_failed_work(ctx, limit=limit, dry_run=True)


# --------------------------------------------------------------------------
# Every failure the status counts is one a run can reach
# --------------------------------------------------------------------------

#: Codes of every kind a writer records on a failed row: the worker's transient set; by-design outcomes in the case and
#: with the history their writers leave; refusals of a route, a size or an account; an interrupted attempt; an
#: embedding route's unusable answers and requests; a consolidation route's refusal and empty answer; the HTTP
#: helper's own code; codes no list names; an empty and a missing code.
_WRITTEN_CODES = (
    *sorted(AUTO_RECOVERABLE_ERRORS),
    "derivation_invalid",
    "DERIVATION_INVALID",
    "derivation_retry:1|derivation_invalid",
    "budget_checked:1110|input_invalid",
    "chunk_checked:1106|INPUT_INVALID",
    "sensitive_request",
    "http_400",
    "http_404",
    "http_402",
    "request_too_large",
    "candidate_attempt_interrupted",
    "vector_dimension_mismatch",
    "retried:0.0.1|auto_retry:2|vector_zero",
    "unsupported_response_shape",
    "unsupported_request_shape",
    "model_refused",
    "empty_output",
    "transport_worker_protocol",
    "a_code_no_release_has_written",
    "",
    None,
)


def _insert_failed(core, ctx, codes):
    """Failed embedding rows of this context, one per code in this order, their attempts spent."""
    capture(core, ctx, "TEST 这一条只是为了让存储有它的范围。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.executemany(
            """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,state,
                   attempt,available_at,last_error_code) VALUES ('embed',?,1,'TEST-scope',?,?,'failed',3,?,?)""",
            [
                (f"TEST-event-{index:05d}", ctx.project_id, ctx.branch_id, "2026-09-06T11:00:00Z", code)
                for index, code in enumerate(codes)
            ],
        )


def test_every_failure_the_status_counts_is_one_a_run_re_opens(app):
    """The worker and the doctor read the store's status: a failed row it does not count as terminal holds the
    instance at ``degraded`` until someone clears it, so a plain run must reach it, and a run with
    ``--include-terminal`` every failed row, whatever wrote its code."""
    core, ctx = app
    _insert_failed(core, ctx, _WRITTEN_CODES)
    status = core.status(ctx)
    assert status.failed_work == len(_WRITTEN_CODES) and 0 < status.terminal_failed_work < status.failed_work
    plain = core.operations.retry_failed_work(ctx, limit=256, dry_run=True)
    assert plain["retried"] == status.failed_work - status.terminal_failed_work
    every = core.operations.retry_failed_work(ctx, limit=256, include_terminal=True, dry_run=True)
    assert every["retried"] == status.failed_work


@pytest.mark.parametrize("ahead", ["derivation_invalid", f"retried:{RETRY_GENERATION}|timeout"])
def test_failures_a_run_passes_over_never_hide_the_ones_behind_them(app, ahead):
    """A run read eight failed rows for each one it could re-open and looked no further: more older rows than that it
    passes over, by design or already re-opened by this release, hid every fault behind them."""
    core, ctx = app
    _insert_failed(core, ctx, [ahead] * 600 + ["timeout"])
    report = core.operations.retry_failed_work(ctx, limit=1, dry_run=False)
    assert (report["retried"], report["by_kind"], report["examined"]) == (1, {"timeout": 1}, 601)
    with sqlite3.connect(core.storage.path) as conn:
        states = dict(conn.execute("SELECT subject_ref,state FROM work_items WHERE subject_ref LIKE 'TEST-event-%'"))
    assert states.pop("TEST-event-00600") == "pending" and set(states.values()) == {"failed"}


class _Embedding:
    """An embedding port that answers each request with the next of ``answers`` and every later one with the last: a
    code, raised as the route's adapter raises it, or None for a vector."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.written = []

    def prepare_source(self, source, *, remaining_seconds=1.0):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if answer is not None:
            raise AuxiliaryModelError(answer)
        return (0.25, 0.5, 0.75)

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        assert lease_guard()
        self.written.append(source.ref)


def _embed_row(core, source):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            """SELECT state,attempt,last_error_code FROM work_items WHERE work_type='embed' AND subject_ref=?
               AND subject_revision=?""",
            (source.ref, source.revision),
        ).fetchone()


def _embed_passes(core, ctx, port, *, passes=1):
    """Worker passes with the embedding route alone, five minutes apart: past each retry's backoff, inside the hour
    the automatic recovery waits."""
    for _ in range(passes):
        later = datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(minutes=5)
        core.clock.now = later.strftime("%Y-%m-%dT%H:%M:%SZ")
        core.drain_worker(ctx, max_items=8, remaining_seconds=10, embed=port)


@pytest.mark.parametrize(
    "code", ["vector_dimension_mismatch", "vector_nonfinite", "vector_zero", "unsupported_response_shape"]
)
def test_an_embedding_a_route_answered_unusably_is_re_opened_and_embedded(app, code):
    """A captured message's embedding failed three times on answers its route could not use (another model behind
    the endpoint answers with another width).  Once the route answers well, a run re-opens it, and the worker embeds
    it."""
    core, ctx = app
    source = capture(core, ctx, "TEST 我的显示器是 27 寸的。")
    _embed_passes(core, ctx, _Embedding(code), passes=3)
    assert _embed_row(core, source) == ("failed", 3, code)
    preview = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["retried"], preview["by_kind"]) == (1, {code: 1})
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["by_kind"] == {code: 1}
    port = _Embedding(None)
    _embed_passes(core, ctx, port)
    assert _embed_row(core, source)[0] == "done" and port.written == [source.ref]


def test_a_re_opened_row_is_not_failed_again_by_one_transient_fault(app):
    """Re-opened with the attempts that had failed it, a row had none left: the first timeout of its re-look failed it
    again, for the rest of the release."""
    core, ctx = app
    source = capture(core, ctx, "TEST 我的键盘是 87 键的。")
    _embed_passes(core, ctx, _Embedding("timeout"), passes=3)
    assert _embed_row(core, source)[:2] == ("failed", 3)
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["by_kind"] == {"timeout": 1}
    _embed_passes(core, ctx, _Embedding("network_error"))
    assert _embed_row(core, source)[:2] == ("pending", 1)
    port = _Embedding(None)
    _embed_passes(core, ctx, port)
    assert _embed_row(core, source)[0] == "done" and port.written == [source.ref]


# --------------------------------------------------------------------------
# What the status counts as a terminal failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "derivation_invalid",
        "DERIVATION_INVALID",
        "auto_retry:1|derivation_invalid",
        "budget_checked:1108|input_invalid",
        "input_invalid",
        "auto_retry:1|sensitive_request",
    ],
)
def test_every_by_design_terminal_failure_counts_as_terminal(app, code):
    """Whatever its decoration or case, a failed row of a terminal kind is counted terminal, as
    ``retry-failures`` reads it."""
    core, ctx = app
    _fail_one(core, ctx, code)
    status = core.status(ctx)
    assert status.failed_work >= 1 and status.terminal_failed_work == status.failed_work


def test_status_counts_each_failure_kind_once(app):
    """Retry history and case used to split one kind across several rows, so
    an operator saw ``derivation_invalid`` three times with different counts."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    codes = ("auto_retry:1|derivation_invalid", "DERIVATION_INVALID", "derivation_retry:1|derivation_invalid")
    with sqlite3.connect(core.storage.path) as conn:
        ids = [row[0] for row in conn.execute("SELECT work_id FROM work_items ORDER BY work_id LIMIT 3")]
        for work_id, code in zip(ids, codes):
            conn.execute("UPDATE work_items SET state='failed',last_error_code=? WHERE work_id=?", (code, work_id))
        conn.commit()
    counts = core.status(ctx).work_error_counts
    kinds = [kind for kind, _count in counts]
    assert len(kinds) == len(set(kinds))
    assert dict(counts)["derivation_invalid"] == len(ids) >= 2
    assert all(kind == kind.lower() and "|" not in kind for kind in kinds)


def test_a_fault_still_counts_as_actionable(app):
    """Narrowing must not go so far that a real fault stops being reported."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    assert core.status(ctx).terminal_failed_work == 0


def test_a_queued_row_s_last_error_is_kept_apart_from_the_failures(app):
    """A row queued for another attempt keeps the error of its last one.  Only failed rows make the failure counts;
    a queued row's terminal error counted among them hid as many failed rows that need a look."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        queued = [row[0] for row in conn.execute("SELECT work_id FROM work_items WHERE state!='failed' LIMIT 2")]
        for work_id in queued:
            conn.execute(
                "UPDATE work_items SET state='pending',last_error_code='auto_retry:1|derivation_invalid' "
                "WHERE work_id=?",
                (work_id,),
            )
        conn.commit()
    status = core.status(ctx)
    assert len(queued) == 2 and status.failed_work >= 1
    assert status.terminal_failed_work == 0
    assert dict(status.work_error_counts) == {"timeout": status.failed_work}
    assert dict(status.pending_error_counts) == {"derivation_invalid": 2}


# --------------------------------------------------------------------------
# What the worker retries by itself, an operator can clear too
# --------------------------------------------------------------------------


def test_every_transient_failure_the_worker_knows_is_operator_actionable():
    """A code the worker retries by itself is a fault, never a by-design outcome: rows one outage left behind once
    pinned a live instance at degraded with nobody able to clear them."""
    assert all(retry_class(code) == "actionable" for code in AUTO_RECOVERABLE_ERRORS), (
        "a code the worker retries automatically must also be clearable by hand"
    )


@pytest.mark.parametrize(
    "code",
    [
        "model_unavailable",
        "model_timeout",
        "model_output_interrupted",
        "response_status_failed",
        "network_error",
        "http_protocol",
        "http_429",
        "http_503",
        "http_529",
        "rate_limited",
    ],
)
def test_a_transient_model_failure_is_cleared_by_a_run_and_by_the_automatic_recovery(code):
    assert retry_class(code) == "actionable"
    assert selects(code, include_terminal=False, generation=RETRY_GENERATION) is True
    assert code in AUTO_RECOVERABLE_ERRORS


@pytest.mark.parametrize(
    "code",
    [
        "http_404",
        "http_409",
        "http_413",
        "http_422",
        "http_501",
        "http_520",
        "http_524",
        "endpoint_invalid",
        "http_redirect",
        "request_limit",
        "response_limit",
    ],
)
def test_a_refused_request_can_be_cleared_once_its_cause_is_fixed(code):
    """An HTTP status not named elsewhere: the request, its route or its model was refused, or the provider failed in
    a way the worker does not recover by itself, so nothing retries it by itself, but an operator who fixed the cause
    may re-open it, as ``http_400``.  The HTTP worker's own refusals of a route or a size are its own codes, not a
    status (a 5xx, and ``http_redirect``, left out at first: reviews of 3.7.3)."""
    assert retry_class(code) == "actionable"
    assert selects(code, include_terminal=False, generation=RETRY_GENERATION) is True
    assert code not in AUTO_RECOVERABLE_ERRORS


@pytest.mark.parametrize("code", ["http_401", "http_402", "http_403"])
def test_an_account_refusal_left_behind_can_be_cleared(code):
    """Before the worker parked account refusals it failed their items, and no
    operator command could reopen them once the account was fixed."""
    assert retry_class(code) == "actionable"
    assert selects(code, include_terminal=False, generation=RETRY_GENERATION) is True


def test_an_account_refusal_row_reopens_through_real_storage(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET last_error_code='http_402' WHERE state='failed'")
        conn.execute("UPDATE candidate_evaluations SET failure_code='http_402' WHERE state='failed'")
        conn.commit()
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["by_kind"].get("http_402") == 1
    work, evaluations = _states(core)
    assert not work.get("failed") and not evaluations.get("failed")


def test_a_transient_failure_clears_through_real_storage(app):
    """The live case: the outage left model_unavailable rows behind."""
    core, ctx = app
    _fail_one(core, ctx, "model_unavailable")
    assert _states(core)[0].get("failed")
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("model_unavailable")
    assert not _states(core)[0].get("failed")


def test_a_refused_evaluation_clears_through_real_storage(app):
    """``http_422`` failed a candidate evaluation on the shared store, and no command could re-open it."""
    core, ctx = app
    _fail_one(core, ctx, "http_422")
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("http_422")
    assert not _states(core)[0].get("failed")


def test_a_transport_failure_mid_reply_clears_through_real_storage(app):
    """#201: a model served over plain HTTP failed candidate evaluations with ``http_protocol``, and no operator
    command could take them up again."""
    core, ctx = app
    _fail_one(core, ctx, "http_protocol")
    assert _states(core)[0].get("failed")
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("http_protocol")
    assert not _states(core)[0].get("failed")


# --------------------------------------------------------------------------
# A provider refusing everyone must not spend an item's own budget
# --------------------------------------------------------------------------


def test_a_capacity_refusal_does_not_consume_an_attempt(app):
    """Four hours of "monthly usage limit reached" pushed 195 live work items
    into failed at attempt=3 apiece, each needing an operator to grant it back.
    A 429 says nothing about this payload."""
    from scope_recall.core.work_storage import CAPACITY_REFUSALS, MAX_RECOVERABLE_ATTEMPTS

    core, ctx = app
    _fail_one(core, ctx, "timeout")  # build one leasable work item
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    seen = []
    for _ in range(MAX_RECOVERABLE_ATTEMPTS + 3):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
            if not leased:
                break
            item = leased[0]
            tx.work.fail(
                item.work_id,
                item.lease_token,
                item.lease_owner,
                error_code="http_429",
                now=core.clock.utc_now(),
                recoverable=True,
            )
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state,attempt FROM work_items WHERE state<>'done'").fetchone()
            seen.append((row["state"], row["attempt"]) if row else None)
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()

    assert all(state == "pending" for state, _attempt in seen if state), seen
    assert not any(state == "failed" for state, _ in seen if state), (
        f"a provider outage exhausted the item's budget: {seen}"
    )
    assert "http_429" in CAPACITY_REFUSALS


def test_an_ordinary_fault_still_exhausts_its_budget(app):
    """The refund must not become a licence for every failure to retry forever."""
    from scope_recall.core.work_storage import MAX_RECOVERABLE_ATTEMPTS

    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    ended = None
    for _ in range(MAX_RECOVERABLE_ATTEMPTS + 2):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
            if not leased:
                break
            item = leased[0]
            tx.work.fail(
                item.work_id,
                item.lease_token,
                item.lease_owner,
                error_code="timeout",
                now=core.clock.utc_now(),
                recoverable=True,
            )
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state FROM work_items WHERE state<>'done'").fetchone()
            ended = row["state"] if row else None
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()
    assert ended == "failed", f"an ordinary fault retried forever: {ended}"


def test_a_terminal_failure_is_unaffected_by_the_refund(app):
    core, ctx = app
    _fail_one(core, ctx, "derivation_invalid")
    assert _states(core)[0].get("failed")


# --------------------------------------------------------------------------
# Never given up on, but never asked continuously either
# --------------------------------------------------------------------------


def test_the_wait_grows_with_each_refusal_in_a_row():
    """Refunding the attempt alone left the ordinary 60s ceiling in place, and
    197 items retried without pause for an entire provider outage."""
    from scope_recall.core.work_storage import CAPACITY_BACKOFF_FLOOR_SECONDS, _capacity_backoff_seconds

    waits = [_capacity_backoff_seconds(n) for n in range(1, 8)]
    assert waits == sorted(waits), waits
    assert waits[0] == CAPACITY_BACKOFF_FLOOR_SECONDS
    assert waits[-1] > waits[0]


def test_the_first_wait_is_not_shorter_than_the_cycle_it_replaces():
    """Sized against what the queue actually did: over eight hours of a real
    outage the median gap between one item's retries was 115 minutes, and 74%
    fell between one and two hours.  A backoff that started below that would
    make the instance ask *more* often than leaving it alone -- which a first
    draft of this, starting at 60 seconds, would have done."""
    from scope_recall.core.work_storage import (
        CAPACITY_BACKOFF_CEILING_SECONDS,
        CAPACITY_BACKOFF_FLOOR_SECONDS,
        _capacity_backoff_seconds,
    )

    observed_median_seconds = 115 * 60
    assert _capacity_backoff_seconds(1) >= observed_median_seconds / 4
    assert CAPACITY_BACKOFF_CEILING_SECONDS > observed_median_seconds
    assert CAPACITY_BACKOFF_FLOOR_SECONDS < CAPACITY_BACKOFF_CEILING_SECONDS


def test_the_wait_has_a_ceiling_but_the_retries_do_not():
    """A cap on *how often* is a loop guard; a cap on *how many* would give up
    on the item, which is the thing this must never do."""
    from scope_recall.core.work_storage import (
        CAPACITY_BACKOFF_CEILING_SECONDS,
        _capacity_backoff_seconds,
    )

    assert _capacity_backoff_seconds(50) == CAPACITY_BACKOFF_CEILING_SECONDS
    assert _capacity_backoff_seconds(10_000) == CAPACITY_BACKOFF_CEILING_SECONDS


def test_the_refusal_count_is_read_back_from_the_error_code():
    from scope_recall.core.work_storage import _capacity_count

    assert _capacity_count("capacity:5|http_429") == 5
    assert _capacity_count("http_429") == 0
    assert _capacity_count(None) == 0


def test_consecutive_refusals_space_the_retries_out(app):
    """Each refusal pushes the next attempt further out. Refunding the attempt
    without this left the ordinary 60s ceiling in place, and 197 live items
    retried without pause for an entire provider outage."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        # One item, so each reading below is the same item's next due time
        # rather than whichever row the round-robin claim happened to hand out.
        keep = conn.execute("SELECT min(work_id) FROM work_items").fetchone()[0]
        conn.execute("DELETE FROM candidate_evaluations WHERE work_id<>?", (keep,))
        conn.execute("DELETE FROM work_items WHERE work_id<>?", (keep,))
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    waits, codes = [], []
    for _ in range(4):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
            assert leased, "the item stopped being claimable"
            item = leased[0]
            tx.work.fail(
                item.work_id,
                item.lease_token,
                item.lease_owner,
                error_code="http_429",
                now=core.clock.utc_now(),
                recoverable=True,
            )
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state,available_at,last_error_code FROM work_items").fetchone()
            assert row["state"] == "pending", f"the outage consumed the item: {row['state']}"
            waits.append(row["available_at"])
            codes.append(row["last_error_code"])
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()

    assert waits == sorted(waits), f"the retries did not move further out: {waits}"
    assert len(set(waits)) == len(waits), f"every retry landed at the same moment: {waits}"
    assert codes[-1].startswith("capacity:"), codes


def test_the_two_rate_limit_sets_cannot_drift_apart():
    """Two hand-kept sets said the same thing about 429 and 503 while disagreeing about 502 and 504, which is how a
    pair always ends up.  The pass stands a work type down for every capacity refusal, taken from that set and not
    copied from it, and for an interrupted answer, which was billed and is no refusal."""
    from scope_recall.core.work_storage import CAPACITY_REFUSALS
    from scope_recall.core.worker import _RATE_LIMITED_ERRORS

    assert CAPACITY_REFUSALS <= _RATE_LIMITED_ERRORS
    assert _RATE_LIMITED_ERRORS - CAPACITY_REFUSALS == {"model_output_interrupted"}


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("repair_success", [False, True])
def test_invalid_candidate_gets_one_extra_attempt_repair_or_visible_review(app, legacy, repair_success):
    """Real candidate worker; legacy failures and fresh output share one cap."""
    from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT

    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    import json

    from scope_recall.core.worker import build_consolidation_model
    from scope_recall.runtime.instance import _BoundedCandidate

    calls = []

    class InvalidPort:
        def propose(self, messages, *, remaining_seconds):
            calls.append(json.dumps(messages))
            expected = {
                "code": "DERIVATION_INVALID" if legacy else "INPUT_INVALID",
                "field": "payload" if legacy or not repair_success else "source_refs",
            }
            hint = "validation_error=" + json.dumps(expected, sort_keys=True, separators=(",", ":"))
            if repair_success:
                if any(hint in message["content"] for message in messages if message["role"] == "system"):
                    body = next(
                        json.loads(message["content"])
                        for message in messages
                        if message["role"] == "user" and message["content"].startswith("{")
                    )
                    return json.dumps(body["empty_result"])
                return '{"protocol_version":"1.1"}'
            return "{not-json FAILED_BODY_SENTINEL"

    model = _BoundedCandidate(build_consolidation_model(InvalidPort()), 3)
    if legacy:
        with sqlite3.connect(core.storage.path) as db:
            db.execute(
                "UPDATE work_items SET state='failed',attempt=3,last_error_code='derivation_invalid' WHERE work_type='evaluate_candidate'"
            )
            db.execute("UPDATE candidate_evaluations SET state='failed',model_attempted_at=?", (core.clock.utc_now(),))
            db.execute("UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason='evaluation_failed'")
    for _ in range(4):
        with sqlite3.connect(core.storage.path) as db:
            db.execute("UPDATE work_items SET available_at=? WHERE state='pending'", (core.clock.utc_now(),))
        core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=model)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert len(calls) == (1 if legacy else 2), (work, evaluations)
    hint = {
        "code": "DERIVATION_INVALID" if legacy else "INPUT_INVALID",
        "field": "payload" if legacy or not repair_success else "source_refs",
    }
    assert "validation_error=" + json.dumps(hint, sort_keys=True, separators=(",", ":")) in " ".join(
        message["content"] for message in json.loads(calls[-1])
    )
    assert "FAILED_BODY_SENTINEL" not in calls[-1]
    if not legacy:
        assert "validation_error=" not in calls[0] and calls[0] != calls[1]
    if repair_success:
        assert work[0]["state"] == "done"
        assert evaluations[0]["state"] == "waiting_evidence"
        assert lifecycle[0]["reason"] == "insufficient_evidence"
        with sqlite3.connect(core.storage.path) as db:
            assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 0
        return
    assert work[0]["state"] == evaluations[0]["state"] == "failed"
    assert "derivation_retry:1|" in work[0]["last_error_code"]
    assert lifecycle[0]["reason"] == "evaluation_failed"
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM work_items WHERE state='failed'").fetchone()[0] == 1
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0
    # Explicit operator action does not grant another automatic retry budget.
    assert core.operations.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)["retried"] == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=model)
    assert len(calls) == (2 if legacy else 3)
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
