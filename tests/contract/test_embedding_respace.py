"""A re-embed run after the embedding space changed (``respace-embeddings``).

``work_items`` is unique on its type and subject, so an embedding done in one space stayed done when the model
changed, and the new space never received it (#200, found and reproduced by @Vivamisu; the model switch below follows
that reproduction).  An operator starts a run; each drain of a worker in that space reopens a page of the store's
done embeddings, newest first and only while the embed queue has room.
"""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import work_storage
from scope_recall.core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING
from scope_recall.core.storage import SQLiteStorage
from scope_recall.runtime.vector_upkeep import respace_if_due

from tests.contract.test_trace import app, edge  # noqa: F401  (fixture)

SPACE_A = "a" * 64
SPACE_B = "b" * 64


def _embeds(core) -> list[tuple]:
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("""SELECT work_id,subject_ref,state,last_error_code,attempt,lease_token FROM work_items
                               WHERE work_type='embed' ORDER BY work_id""").fetchall()


def _finish_embeds(core) -> None:
    """As a worker that embedded everything queued leaves it."""
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""UPDATE work_items SET state='done',attempt=1,lease_token=lease_token+1,last_error_code=NULL
                        WHERE work_type='embed' AND state<>'done'""")


def _start(core, ctx, space=SPACE_B, action="start") -> dict:
    return core.respace_embeddings(ctx, space_id=space, action=action, dry_run=False)


def _page(core, ctx, space=SPACE_B, room=64) -> dict:
    with core.storage.write(ctx) as tx:
        return tx.work.respace_page(space, now="2026-10-06T12:00:00Z", room=room)


def _queue_embeds(core, count: int, *, prefix: str = "event-TEST-waiting") -> None:
    with sqlite3.connect(core.storage.path) as conn:
        for index in range(count):
            conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at)
                            VALUES ('embed',?,1,'TEST-scope','2026-09-28T00:00:00Z')""", (f"{prefix}-{index}",))


def _space_instance(core, ctx, model, *, backend="sqlite-bruteforce", storage_dir=None):
    """A runtime instance embedding with ``model``: its own space, its own vector directory (``storage_dir`` keeps a
    native store's path short on Windows)."""
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.instance import (RuntimeInstance, RuntimeInstanceConfig, VectorRuntimeConfig,
                                               default_vector_factory)

    class Embedding:
        def embed_query(self, text, *, remaining_seconds):
            return (1.0,) + (0.0,) * 7

        def embed_source(self, source, *, remaining_seconds):
            return (1.0,) + (0.0,) * 7

        def embed_text(self, text, *, remaining_seconds):
            return (1.0,) + (0.0,) * 7

    auxiliary = AuxiliaryRuntimeConfig.from_mapping({
        "external_embedding": False, "external_consolidation": False,
        "embedding": {"credential_env": "TEST_EMBED_KEY", "model": model,
                      "endpoint": "https://test.invalid/embeddings", "dimensions": 8, "dialect": "openai"},
    })
    config = RuntimeInstanceConfig(binding=ctx.binding, session_id=ctx.session_id, allowed_scope_ids=ctx.allowed_scope_ids,
                                   project_id=ctx.project_id, branch_id=ctx.branch_id, auxiliary=auxiliary)
    config = replace(config, vector=VectorRuntimeConfig(
        backend=backend, storage_dir=storage_dir or ctx.binding.data_directory / "vectors" / config.embedding_space_id(),
        table_name="TEST-respace", dimensions=8, test_injection_override=storage_dir is not None))
    return RuntimeInstance(config, core, SimpleNamespace(query_embedding=Embedding(), source_embedding=Embedding()),
                           _vector_factory=default_vector_factory)


def test_a_model_switch_re_embeds_what_was_embedded_once_an_operator_starts_a_run(app):
    """Switching alone leaves the new space empty, on purpose: re-embedding is paid.  A run fills it."""
    from scope_recall.core.composition import SystemClock

    core, ctx = app
    core.clock = SystemClock()
    claim, source = edge(core, ctx, "TEST-A", "TEST-B")
    old = _space_instance(core, ctx, "TEST-model-A")
    new = _space_instance(core, ctx, "TEST-model-B")
    space = new.config.embedding_space_id()
    try:
        old.drain()
        assert old._vector_store.count_rows() == 2
        before = _embeds(core)
        assert [row[2] for row in before] == ["done", "done"]
        new.drain()
        assert new.embed_respace is None and new._vector_store.count_rows() == 0
        assert _embeds(core) == before, "no run, nothing reopened"
        assert _start(core, ctx, space=space)["to_reopen"] == 2
        new.drain(purge_only=True)
        assert _embeds(core) == before, "a pass that only purges goes on with no run"
        new.drain()
        assert (new.embed_respace["outcome"], new.embed_respace["reopened"]) == ("complete", 2)
        assert set(new._vector_store.list_ids()) == {f"p10:{ref}@1:{space}" for ref in (source.ref, claim.ref)}
        assert [row[2] for row in _embeds(core)] == ["done", "done"]
        new.drain()
        assert new.embed_respace is None, "a finished run does nothing more"
        assert old._vector_store.count_rows() == 2, "the old space is left alone"
    finally:
        old.close()
        new.close()


def test_a_reopened_row_waits_like_new_work_with_its_lease_fenced_and_attempts_afresh(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    before = {row[0]: row for row in _embeds(core)}
    _start(core, ctx)
    assert (_page(core, ctx)["outcome"]) == "complete"
    with sqlite3.connect(core.storage.path) as conn:
        available = {row[0] for row in conn.execute("SELECT available_at FROM work_items WHERE work_type='embed'")}
    assert available == {"2026-10-06T12:00:00Z"}, "it joins the queue behind what already waits"
    for work_id, _ref, state, code, attempt, token in _embeds(core):
        assert (state, code, attempt, token) == ("pending", work_storage.RESPACE_MARKER, 0, before[work_id][5] + 1)


def test_a_preview_counts_what_a_run_would_reopen_and_changes_nothing(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    before = _embeds(core)
    report = core.respace_embeddings(ctx, space_id=SPACE_B, action="start", dry_run=True)
    assert (report["applied"], report["run"], report["to_reopen"], report["waiting"]) == (False, None, 2, 0)
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["run"] is None
    assert _embeds(core) == before


def test_the_preview_counts_what_still_waits_which_a_run_started_now_would_pay_for_twice(app):
    """An embedding waiting at the start lies below the run: it is embedded into the new space in its turn, and the
    run reopens it again when it gets there (review of 3.8.0)."""
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    _queue_embeds(core, 3)
    assert core.respace_embeddings(ctx, space_id=SPACE_B, action="start", dry_run=True)["waiting"] == 3
    _start(core, ctx)
    _finish_embeds(core)
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["to_reopen"] == 5, "the three are reopened too"


def test_one_run_at_a_time_and_one_per_space_unless_started_again_on_purpose(app):
    """A start after a finished run into the same space began a second paid run without a word (review of 3.8.0)."""
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    run = _start(core, ctx)["run"]
    assert (run["embedding_space"], run["completed"], run["reopened"]) == (SPACE_B, False, 0)

    def refused_start(space, field):
        for dry_run in (False, True):
            with pytest.raises(ContractError) as refused:
                core.respace_embeddings(ctx, space_id=space, action="start", dry_run=dry_run)
            assert (refused.value.code, refused.value.field) == ("VERSION_CONFLICT", field)

    refused_start(SPACE_B, "respace_running")
    refused_start(SPACE_A, "respace_running")
    preview = core.respace_embeddings(ctx, space_id=SPACE_B, action="restart", dry_run=True)
    assert preview["run"] == run, "a preview shows the run as it stands"
    assert _start(core, ctx, action="restart")["run"]["next_work_id"] == run["next_work_id"]
    assert _page(core, ctx)["outcome"] == "complete"
    refused_start(SPACE_B, "respace_finished")
    assert _start(core, ctx, action="restart")["run"]["completed"] is False
    assert _page(core, ctx)["outcome"] == "complete"
    again = _start(core, ctx, space=SPACE_A)["run"]
    assert (again["embedding_space"], again["completed"], again["reopened"]) == (SPACE_A, False, 0), \
        "a finished run into another space gives way: the model changed again"


def test_newest_first_within_the_room_and_never_what_came_after_the_start(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-C", "TEST-D")
    _finish_embeds(core)
    started = [row[0] for row in _embeds(core)]
    _start(core, ctx)
    edge(core, ctx, "TEST-E", "TEST-F")
    _finish_embeds(core)
    first = _page(core, ctx, room=1)
    assert (first["outcome"], first["reopened"]) == ("progress", 1)
    states = {row[0]: row[2] for row in _embeds(core)}
    assert [states[work_id] for work_id in started] == ["done", "done", "done", "pending"]
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["to_reopen"] == 3
    rest = _page(core, ctx)
    assert (rest["outcome"], rest["reopened"]) == ("complete", 3)
    states = {row[0]: row[2] for row in _embeds(core)}
    assert [states[work_id] for work_id in started] == ["pending"] * 4
    assert [state for work_id, state in states.items() if work_id not in started] == ["done", "done"], \
        "what was queued after the start is embedded in the new space already"
    run = core.respace_embeddings(ctx, space_id=SPACE_B)["run"]
    assert (run["completed"], run["reopened"]) == (True, 4)


def test_a_tool_output_whose_vector_expired_is_left_without_one(app):
    core, ctx = app
    claim, source = edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("INSERT INTO expired_vectors(source_ref,source_revision,expired_at,reason) VALUES (?,1,?,'window')",
                     (source.ref, "2026-10-01T00:00:00Z"))
    assert _start(core, ctx)["to_reopen"] == 1
    assert _page(core, ctx)["reopened"] == 1
    states = {row[1]: row[2] for row in _embeds(core)}
    assert (states[source.ref], states[claim.ref]) == ("done", "pending")


def test_a_worker_in_another_space_leaves_the_run_alone(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    before = _embeds(core)
    _start(core, ctx, space=SPACE_A)
    assert _page(core, ctx, space=SPACE_B) == {"outcome": "space_mismatch", "embedding_space": SPACE_A}
    assert respace_if_due(SQLiteStorage(ctx.binding), ctx, SPACE_B)["outcome"] == "space_mismatch"
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["space_matches"] is False
    assert _embeds(core) == before


def test_cancel_forgets_the_run_and_what_it_reopened_is_still_embedded(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-C", "TEST-D")
    _finish_embeds(core)
    _start(core, ctx)
    _page(core, ctx, room=1)
    preview = core.respace_embeddings(ctx, space_id=SPACE_B, action="cancel", dry_run=True)
    assert "cancelled" not in preview and preview["run"] is not None, "a preview names the run it would forget"
    report = core.respace_embeddings(ctx, space_id=SPACE_B, action="cancel", dry_run=False)
    assert report["cancelled"] and report["run"] is None
    assert sorted(row[2] for row in _embeds(core)) == ["done", "done", "done", "pending"]
    assert _page(core, ctx)["outcome"] == "none"
    assert respace_if_due(SQLiteStorage(ctx.binding), ctx, SPACE_B) is None


def test_the_drain_s_upkeep_keeps_the_queue_to_its_ceiling_and_yields_to_evaluations(app):
    """Topped up as the import backfill tops it up: a message captured now and an evaluation that waits still move."""
    core, ctx = app
    for index in range(3):
        edge(core, ctx, f"TEST-A{index}", f"TEST-B{index}")
    _finish_embeds(core)
    _start(core, ctx)
    storage = SQLiteStorage(ctx.binding)
    _queue_embeds(core, IMPORT_EMBED_QUEUE_CEILING - 1)
    receipt = respace_if_due(storage, ctx, SPACE_B)
    assert (receipt["outcome"], receipt["reopened"]) == ("progress", 1), "the one place left"
    held = respace_if_due(storage, ctx, SPACE_B)
    assert (held["outcome"], held["reopened"]) == ("held", 0)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE subject_ref LIKE 'event-TEST-waiting-%'")
        conn.execute("""INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at)
                        VALUES ('evaluate_candidate','candidate-TEST-ready',1,'TEST-scope','2026-09-28T00:00:00Z')""")
    evaluations = frozenset({"evaluate_candidate"})
    page = respace_if_due(storage, ctx, SPACE_B, yield_to=evaluations, yield_ceiling=2)
    assert (page["outcome"], page["reopened"]) == ("progress", 1), "two may wait while an evaluation is ready"
    assert respace_if_due(storage, ctx, SPACE_B, yield_to=evaluations, yield_ceiling=2)["outcome"] == "held"
    assert respace_if_due(storage, ctx, SPACE_B)["reopened"] == 4, "without one ready, up to the ceiling"


def test_what_waits_in_a_partition_this_worker_cannot_see_holds_the_run(app):
    """A run reopens rows of every partition, so it is held by the store's queue: counted as the worker's own,
    300 rows of another project went pending 64 a pass (review of 3.8.0)."""
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    _start(core, ctx)
    _queue_embeds(core, IMPORT_EMBED_QUEUE_CEILING, prefix="event-TEST-elsewhere")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET project_id='TEST-other-project' WHERE subject_ref LIKE 'event-TEST-elsewhere-%'")
    storage = SQLiteStorage(ctx.binding)
    with storage.read(ctx) as tx:
        assert (tx.work.pending_depth("embed"), tx.work.embed_queue()["pending"]) == (0, IMPORT_EMBED_QUEUE_CEILING)
    held = respace_if_due(storage, ctx, SPACE_B)
    assert (held["outcome"], held["reopened"]) == ("held", 0)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE subject_ref LIKE 'event-TEST-elsewhere-%'")
    assert respace_if_due(storage, ctx, SPACE_B)["reopened"] == 2


def test_a_drain_says_a_run_into_another_space_and_a_page_that_failed(app, monkeypatch):
    """Both reach the worker's status, where the doctor reads a pass (``background_gaps``)."""
    from scope_recall.core.composition import SystemClock

    core, ctx = app
    core.clock = SystemClock()
    edge(core, ctx, "TEST-A", "TEST-B")
    instance = _space_instance(core, ctx, "TEST-model-B")
    try:
        instance.drain()
        _start(core, ctx, space=SPACE_A)
        instance.drain()
        assert "embedding_respace_space_mismatch" in instance.background_gaps
        _start(core, ctx, space=instance.config.embedding_space_id(), action="restart")

        def refuse(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(work_storage.WorkItems, "respace_page", refuse)
        instance.drain()
        assert "embedding_respace_failed:OperationalError" in instance.background_gaps
    finally:
        instance.close()


def test_a_page_looks_through_a_bounded_window_of_work_ids(app, monkeypatch):
    """However sparse the done embeddings are among the work ids, a page holds the writer lease for a bounded scan."""
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    monkeypatch.setattr(work_storage, "RESPACE_SCAN", 1)
    run = _start(core, ctx)["run"]
    pages = []
    while not pages or pages[-1]["outcome"] == "progress":
        pages.append(_page(core, ctx))
    assert len(pages) == run["next_work_id"], "one work id a page"
    assert (pages[-1]["outcome"], sum(page["reopened"] for page in pages)) == ("complete", 2)


def test_a_failed_page_is_a_receipt_and_changes_nothing(app, monkeypatch):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    _start(core, ctx)
    before = _embeds(core)

    def refuse(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(work_storage.WorkItems, "respace_page", refuse)
    assert respace_if_due(SQLiteStorage(ctx.binding), ctx, SPACE_B) == {"outcome": "failed",
                                                                         "error": "OperationalError"}
    assert _embeds(core) == before
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["run"]["reopened"] == 0


def test_the_command_maps_its_flags_and_previews_unless_applied(app, monkeypatch, capsys):
    from scope_recall.maintenance import cli

    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _finish_embeds(core)
    config = SimpleNamespace(context=lambda: ctx, request_seconds=5.0, embedding_space_id=lambda: SPACE_B)
    monkeypatch.setattr(cli, "_run_core", lambda args, call, **_: cli._emit(call(core, config)) or 0)

    def run(*flags) -> dict:
        assert cli.main(["respace-embeddings", "--config", "TEST-config.json", *flags]) == 0
        return json.loads(capsys.readouterr().out)

    assert (run()["action"], run("--start")["applied"]) == ("status", False)
    assert core.respace_embeddings(ctx, space_id=SPACE_B)["run"] is None
    started = run("--start", "--apply")
    assert (started["action"], started["applied"], started["run"]["embedding_space"]) == ("start", True, SPACE_B)
    assert run("--restart", "--apply")["action"] == "restart"
    assert run("--cancel", "--apply")["cancelled"] is True
    with pytest.raises(SystemExit):
        cli.main(["respace-embeddings", "--config", "TEST-config.json", "--start", "--cancel"])


def test_doctor_names_a_run_no_worker_will_go_on_with():
    from scope_recall.maintenance.doctor import DoctorReport, _check_embedding_respace

    run = {"embedding_space": SPACE_A, "next_work_id": 7, "reopened": 3, "completed": False,
           "updated_at": "2026-10-06T12:00:00Z"}
    going = DoctorReport(host="hermes", status="degraded", embedding_respace=dict(run),
                         embedding_health={"pending": 70, "failed": 0, "oldest_pending_at": None})
    _check_embedding_respace(going, SimpleNamespace(embedding_space_id=lambda: SPACE_A))
    assert going.capability_gaps == [] and going.checks[-1]["result"] == "running"
    assert "70 embeddings wait in the store" in going.checks[-1]["detail"], "a held run says why it waits"
    stranded = DoctorReport(host="hermes", status="degraded", embedding_respace=dict(run))
    _check_embedding_respace(stranded, SimpleNamespace(embedding_space_id=lambda: SPACE_B))
    assert stranded.capability_gaps == ["embedding_respace_space_mismatch"]
    assert stranded.checks[-1]["result"] == "space_mismatch"
    done = DoctorReport(host="hermes", status="degraded", embedding_respace={**run, "completed": True})
    _check_embedding_respace(done, None)
    assert done.capability_gaps == [] and done.checks[-1]["result"] == "complete"


def test_doctor_names_an_embedding_backlog_that_aged_beside_a_refusing_provider(tmp_path):
    """Recall went on answering by words alone while embeddings waited for a week behind HTTP 429s, and nothing
    said so (reported with #200)."""
    from datetime import datetime, timedelta, timezone
    import time

    from scope_recall.maintenance.doctor import DoctorReport, _check_embedding_health
    from scope_recall.runtime.model_budget import REQUESTS_TABLE, embedding_calls

    path = tmp_path / "auxiliary-budget.sqlite3"
    now_ns = time.time_ns()
    with sqlite3.connect(path) as db:
        db.execute(REQUESTS_TABLE)
        db.executemany("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)", [
            ("TEST-embed", "http_200", now_ns - 3 * 86400 * 10 ** 9),  # older than a day: not counted
            ("TEST-embed", "http_200", now_ns - 7200 * 10 ** 9),
            ("TEST-chat", "http_200", now_ns),
            *(("TEST-embed", "http_429_usage_unknown_reserved_charge_retained", now_ns - step * 10 ** 9)
              for step in (2, 1, 0)),
        ])
    auxiliary = SimpleNamespace(ledger_path=path, external_embedding=True, external_consolidation=False,
                                embedding=SimpleNamespace(kind="openai", space=lambda: {"model": "TEST-embed"}))
    calls = embedding_calls(auxiliary)
    assert (calls["model"], calls["calls"], calls["answered"], calls["refused"]) == ("TEST-embed", 4, 1, {"http_429": 3})
    aged = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    report = DoctorReport(host="hermes", status="degraded",
                          embedding_health={"pending": 12, "failed": 0, "oldest_pending_at": aged})
    _check_embedding_health(report, SimpleNamespace(auxiliary=auxiliary, vector=object()))
    assert report.capability_gaps == ["embedding_backlog_aged"]
    assert report.embedding_health["held_model"] == "TEST-embed"
    assert report.embedding_health["last_day"]["refused"] == {"http_429": 3}
    detail = report.checks[-1]["detail"]
    assert "the oldest for 30 h" in detail and "held for TEST-embed" in detail
    assert "asked 4 times and answered 1, refusing http_429 x3" in detail
    fresh = DoctorReport(host="hermes", status="degraded", embedding_health={
        "pending": 12, "failed": 0, "oldest_pending_at": datetime.now(timezone.utc).isoformat()})
    _check_embedding_health(fresh, None)
    assert (fresh.capability_gaps, fresh.checks) == ([], [])
    assert embedding_calls(SimpleNamespace(ledger_path=path, external_embedding=False,
                                           embedding=auxiliary.embedding)) is None, "no external route, no calls"


def test_doctor_says_nothing_of_a_backlog_where_nothing_embeds_and_names_a_worker_where_nothing_refused():
    """Without a vector store or an external embedding route the queue only grows, by choice: an install without one
    went from "attention" to "degraded" for good (review of 3.8.0)."""
    from datetime import datetime, timedelta, timezone

    from scope_recall.maintenance.doctor import DoctorReport, _check_embedding_health

    aged = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    route = SimpleNamespace(ledger_path=None, external_embedding=True, external_consolidation=False,
                            embedding=SimpleNamespace(kind="openai", space=lambda: {"model": "TEST-embed"}))
    for config in (SimpleNamespace(auxiliary=route, vector=None),
                   SimpleNamespace(auxiliary=SimpleNamespace(**{**vars(route), "external_embedding": False}),
                                   vector=object()),
                   SimpleNamespace(auxiliary=SimpleNamespace(**{**vars(route), "embedding": None}), vector=object()),
                   SimpleNamespace(auxiliary=None, vector=object())):
        report = DoctorReport(host="hermes", status="degraded",
                              embedding_health={"pending": 12, "failed": 0, "oldest_pending_at": aged})
        _check_embedding_health(report, config)
        assert (report.capability_gaps, report.checks) == ([], []), config
    report = DoctorReport(host="hermes", status="degraded",
                          embedding_health={"pending": 12, "failed": 0, "oldest_pending_at": aged})
    _check_embedding_health(report, SimpleNamespace(auxiliary=route, vector=object()))
    assert report.capability_gaps == ["embedding_backlog_aged"]
    assert "provider" not in report.checks[-1]["detail"], "no ledger: nothing to say of the provider"


@pytest.mark.parametrize("statuses, said", [
    ((), "nothing asked the provider in the last day, so no worker has reached them"),
    (("network_error_usage_unknown_reserved_charge_retained",) * 3, "asked 3 times and answered 0"),
])
def test_doctor_blames_the_worker_only_when_nothing_asked_the_provider(tmp_path, statuses, said):
    """A proxy outage ends calls in network errors, which are no refusals: "refused nothing, so no worker has reached
    them" was wrong there (review of 3.8.0)."""
    from datetime import datetime, timedelta, timezone
    import time

    from scope_recall.maintenance.doctor import DoctorReport, _check_embedding_health
    from scope_recall.runtime.model_budget import REQUESTS_TABLE

    path = tmp_path / "auxiliary-budget.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(REQUESTS_TABLE)
        db.executemany("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)",
                       [("TEST-embed", status, time.time_ns() - (3600 + step) * 10 ** 9)
                        for step, status in enumerate(statuses)])
    route = SimpleNamespace(ledger_path=path, external_embedding=True, external_consolidation=False,
                            embedding=SimpleNamespace(kind="openai", space=lambda: {"model": "TEST-embed"}))
    aged = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    report = DoctorReport(host="hermes", status="degraded",
                          embedding_health={"pending": 12, "failed": 0, "oldest_pending_at": aged})
    _check_embedding_health(report, SimpleNamespace(auxiliary=route, vector=object()))
    detail = report.checks[-1]["detail"]
    assert said in detail and ("no worker" in detail) is (not statuses), detail


def test_the_embed_queue_is_the_store_s_and_read_by_state(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    _queue_embeds(core, 2, prefix="event-TEST-elsewhere")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET project_id='TEST-other-project',state='failed' "
                     "WHERE subject_ref='event-TEST-elsewhere-0'")
        conn.execute("UPDATE work_items SET project_id='TEST-other-project',available_at='2026-01-01T00:00:00Z' "
                     "WHERE subject_ref='event-TEST-elsewhere-1'")
        plan = " ".join(row[3] for row in conn.execute(
            """EXPLAIN QUERY PLAN SELECT count(*) FROM work_items
               WHERE state IN ('pending','failed') AND +work_type='embed'"""))
    with core.storage.read(ctx) as tx:
        queue = tx.work.embed_queue()
    assert queue == {"pending": 3, "failed": 1, "oldest_pending_at": "2026-01-01T00:00:00Z"}
    assert "work_ready" in plan, plan
