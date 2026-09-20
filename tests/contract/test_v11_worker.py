"""P10 worker lease, consolidation, and bounded drain contracts."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.worker import _decode_consolidation_result
from test_v11_claims import accept, app, capture, draft
from test_v11_deletion import authorize, request


class Clock:
    _now = "2026-09-06T12:00:00Z"
    _mono = 1000.0

    def utc_now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, *, seconds=0.0, iso=None):
        if iso is not None:
            self._now = iso
        self._mono += seconds


def work_rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            "SELECT work_type,subject_ref,subject_revision,state,attempt,lease_token,last_error_code FROM work_items ORDER BY work_id"
        ).fetchall()


def _mark_embed_done(core) -> None:
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()


def procedure_proposal(source, *, value="导出步骤", verification="user_accepted", roots=None):
    refs = roots or [f"{source.ref}@{source.revision}"]
    return dict(
        kind="procedure",
        subject="TEST-project",
        predicate="导出方法",
        value_text=value,
        conditions=["同类型TEST导出"],
        statement_kind="assertion",
        valid_from=source.event["occurred_at"],
        valid_to=None,
        evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"])],
        procedure=dict(
            conditions=["同类型TEST导出"],
            non_applicable=["非TEST环境"],
            method=["先检查透明背景", "再导出"],
            verification_basis=verification,
            counterexample_refs=[],
        ),
    )


def consolidation_payload(*sources, claims=(), resume_proposals=()):
    refs = [f"{s.ref}@{s.revision}" for s in sources]
    return dict(
        protocol_version="1.1",
        source_refs=refs,
        claim_proposals=list(claims),
        resume_proposals=list(resume_proposals),
        reference_proposals=[],
    )


class FakeConsolidation:
    def __init__(self, builder):
        self.builder = builder
        self.calls = 0
        self.feedbacks = []

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
        self.calls += 1
        self.feedbacks.append(validation_feedback)
        # Model-port feedback is optional; deterministic builders stay unchanged.
        payload = self.builder(sources, episode_ref=episode_ref)
        return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def worker_app(app):
    core, ctx = app
    clock = Clock()
    core.clock = clock
    return core, ctx, clock


def test_work_queue_dedupes_subject_version_and_types(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 队列去重。")
    rows = work_rows(core)
    assert len(rows) == 2
    assert {(r[0], r[2]) for r in rows} == {("consolidate", 1), ("embed", 1)}
    with core.storage.write(ctx) as tx:
        first = tx.work.enqueue("consolidate", source.ref, 1, available_at=clock.utc_now())
        second = tx.work.enqueue("embed", source.ref, 1, available_at=clock.utc_now())
    assert first is False and second is False
    assert len(work_rows(core)) == 2


def test_bounded_drain_is_idle_without_daemon(worker_app):
    core, ctx, clock = worker_app
    receipt = core.drain_worker(ctx, max_items=4, remaining_seconds=5)
    assert receipt.idle and receipt.processed == 0


def test_C14_duplicate_capture_does_not_expand_work(worker_app):
    core, ctx, clock = worker_app
    first = capture(core, ctx, "好", key="TEST-C14/1")
    second = capture(core, ctx, "好", key="TEST-C14/2")
    replay = capture(core, ctx, "好", key="TEST-C14/1", recorded_at="2026-09-06T07:00:00Z")
    assert replay.ref == first.ref and replay.revision == first.revision
    assert len(work_rows(core)) == 0  # acknowledgements remain source-only
    assert core.status(ctx).sources == 2


def consolidate_rows(core):
    return [row for row in work_rows(core) if row[0] == "consolidate"]


def test_model_unavailable_waits_without_spending_attempts(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 需要巩固。")
    # The source capture schedules one consolidate and one embed item.  Keep
    # the retry test focused on the consolidate lease rather than letting the
    # independently ordered embed item consume a retry pass.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5)
    assert receipt.processed == 0 and receipt.unavailable_work_types == ("consolidate",)
    row = consolidate_rows(core)[0]
    assert row[3] == "pending" and row[4] == 0 and row[6] is None
    for attempt in (2, 3):
        clock.advance(seconds=60, iso=f"2026-09-06T12:0{attempt}:00Z")
        core.drain_worker(ctx, owner_id=f"retry-worker-{attempt}", max_items=1, remaining_seconds=5)
    row = consolidate_rows(core)[0]
    assert row[3] == "pending" and row[4] == 0 and row[6] is None
    model = FakeConsolidation(lambda sources, **_: consolidation_payload(*sources))
    assert core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5).completed == 1
    assert model.calls == 1


def test_budget_exhausted_is_not_relabeled_model_unavailable(worker_app):
    core, ctx, _clock = worker_app
    capture(core, ctx, "TEST 预算耗尽不得伪装成无模型。")
    _mark_embed_done(core)

    class BudgetError(RuntimeError):
        def __init__(self):
            self.error_type = "budget_exhausted"
            super().__init__("budget_exhausted")

    class ExhaustedModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            raise BudgetError()

    receipt = core.drain_worker(ctx, consolidation=ExhaustedModel(), max_items=1, remaining_seconds=5)
    assert receipt.deferred == 1 and receipt.retried == 0 and receipt.failed == 0
    row = consolidate_rows(core)[0]
    assert row[3] == "pending" and row[4] == 0 and row[6] == "budget_exhausted"
    assert core.drain_worker(ctx, consolidation=ExhaustedModel(), max_items=1, remaining_seconds=5).processed == 0


def test_transient_http_status_still_retries_with_honest_code(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 短暂 HTTP 失败仍可重试。")
    _mark_embed_done(core)

    class HttpError(RuntimeError):
        def __init__(self):
            self.error_type = "http_status"
            super().__init__("http_status")

    class FlakyHttp:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            raise HttpError()

    receipt = core.drain_worker(ctx, consolidation=FlakyHttp(), max_items=1, remaining_seconds=5)
    assert receipt.retried == 1 and receipt.failed == 0
    row = consolidate_rows(core)[0]
    assert row[3] == "pending" and row[6] == "http_status"
    clock.advance(seconds=60, iso="2026-09-06T12:01:00Z")
    core.drain_worker(ctx, consolidation=FlakyHttp(), max_items=1, remaining_seconds=5)
    row = consolidate_rows(core)[0]
    assert row[3] == "pending" and row[6] == "http_status"


def test_bad_json_records_derivation_invalid_without_hiding_source(worker_app, monkeypatch):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 精确代码 XAS-A_19.2-beta。")
    _mark_embed_done(core)

    from scope_recall.core.worker import build_consolidation_model
    from scope_recall.runtime.instance import _BoundedConsolidation
    payloads = []

    class BadPort:
        def propose(self, messages, *, remaining_seconds):
            payloads.append(json.dumps(messages))
            return "{not-json FAILED_BODY_SENTINEL"

    model = _BoundedConsolidation(build_consolidation_model(BadPort()), 3)
    # One extra attempt only; keep failure and source visible, never mark done.
    for attempt in range(1, 5):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{attempt:02d}:30Z")
        core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=5)
    assert len(payloads) == 2 and payloads[0] != payloads[1]
    assert "validation_error=" not in payloads[0]
    assert 'validation_error={"code":"INPUT_INVALID","field":"payload"}' in json.loads(payloads[1])[0]["content"]
    assert "FAILED_BODY_SENTINEL" not in payloads[1]
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    assert row[3] == "failed" and row[4] == 2
    assert row[6].startswith("derivation_retry:1|")
    with sqlite3.connect(core.storage.path) as db:
        from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM consolidation_outcomes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM work_error_details").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    assert core.source(ctx, source.ref, 1).event["content"] == "TEST 精确代码 XAS-A_19.2-beta。"
    from scope_recall.maintenance import doctor
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *args: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    report = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert report.needs_review_work == report.failed_work == 1
    assert report.to_dict()["needs_review_work"] == 1
    assert "work_needs_review" in report.capability_gaps and report.status != "ok"


def test_flaky_model_derivation_recovers_on_bounded_retry(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 精确代码 XAS-A_19.2-beta。")
    _mark_embed_done(core)
    from scope_recall.core.worker import build_consolidation_model
    from scope_recall.runtime.instance import _BoundedConsolidation
    payloads = []
    # The field names the property the model left out, not the schema keyword
    # that caught it: "required" told the model nothing it could act on.
    hint = 'validation_error={"code":"INPUT_INVALID","field":"source_refs"}'

    class RepairPort:
        def propose(self, messages, *, remaining_seconds):
            payloads.append(json.dumps(messages))
            # This succeeds only when the actual formatter carries the validator
            # feedback, not merely because this happens to be the second call.
            if hint not in messages[0]["content"]:
                return '{"protocol_version":"1.1"}'
            return json.dumps(json.loads(messages[-1]["content"])["empty_result"])

    model = _BoundedConsolidation(build_consolidation_model(RepairPort()), 3)
    first = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
    assert first.retried >= 1 and first.failed == 0
    for attempt in range(1, 4):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{attempt:02d}:30Z")
        core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=5)
    assert len(payloads) == 2 and payloads[0] != payloads[1]
    assert "validation_error=" not in payloads[0]
    assert hint in json.loads(payloads[1])[0]["content"]
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    assert row[3] == "done"


def test_reply_cut_off_at_output_limit_is_named_in_the_guided_retry(worker_app, tmp_path, monkeypatch):
    from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
    from scope_recall.core.worker import build_consolidation_model
    from scope_recall.runtime.auxiliary import build_auxiliary_runtime
    from scope_recall.runtime.instance import _BoundedConsolidation
    from test_runtime_auxiliary import FakeTransport, _chat_reply, _runtime_config

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 输出被上限截断。")
    _mark_embed_done(core)
    config, _, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    prompts = []

    def cut_off(**kwargs):
        prompts.append(json.loads(kwargs["body"])["messages"][0]["content"])
        return 200, _chat_reply({"role": "assistant", "content": '{"protocol_version":"1.1","source_refs":["'}, "length")

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(cut_off))
    model = _BoundedConsolidation(build_consolidation_model(runtime.consolidation), 3)
    for attempt in range(1, 5):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{attempt:02d}:30Z")
        core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=5)
    assert len(prompts) == 2
    assert "validation_error=" not in prompts[0]
    assert 'validation_error={"code":"DERIVATION_INVALID","field":"model_output_truncated"}' in prompts[1]
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT error_code,error_field FROM work_error_details").fetchall() == [
            ("DERIVATION_INVALID", "model_output_truncated")] * 2
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    assert row[3] == "failed" and row[6].startswith("derivation_retry:1|")


def test_consolidation_accepts_only_transport_fence_and_null_optional_location(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 的公开代号是 TEST-ARCHIVE-V12-FLARE。")
    _mark_embed_done(core)

    class FencedModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            claim = dict(
                kind="fact", subject="TEST-project", predicate="公开代号",
                value_text="TEST-ARCHIVE-V12-FLARE", conditions=[],
                statement_kind="assertion", valid_from=sources[0].event["occurred_at"],
                valid_to=None,
                evidence_spans=[dict(source_ref=sources[0].ref, source_revision=sources[0].revision,
                                     quote=sources[0].event["content"], location=None)],
            )
            payload = consolidation_payload(sources[0], claims=[claim])
            return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    receipt = core.drain_worker(ctx, consolidation=FencedModel(), max_items=1, remaining_seconds=5)
    assert receipt.completed == 1
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(predicate="公开代号")
    assert refs and core.current_claim(ctx, refs[0]).payload["value_text"] == "TEST-ARCHIVE-V12-FLARE"


@pytest.mark.parametrize(
    "raw",
    [
        '{"protocol_version":"1.1","source_refs":[],"claim_proposals":[],"resume_proposals":[],"reference_proposals":[]} trailing',
        'prefix\n{"protocol_version":"1.1","source_refs":[],"claim_proposals":[],"resume_proposals":[],"reference_proposals":[]}',
        '{"protocol_version":"1.1","source_refs":[],"claim_proposals":[],"resume_proposals":[],"reference_proposals":[],"extra":true}',
        '[]',
    ],
)
def test_consolidation_decoder_rejects_unsafe_envelopes(raw):
    with pytest.raises((ContractError, ValueError, TypeError, json.JSONDecodeError)):
        _decode_consolidation_result(raw)


def _timed_claim_result(valid_from, valid_to=None):
    claim = dict(kind="fact", subject="TEST-project", predicate="配色", value_text="蓝色", conditions=[],
                 statement_kind="assertion", valid_from=valid_from, valid_to=valid_to,
                 evidence_spans=[dict(source_ref="TEST-event", source_revision=1, quote="TEST-project 配色 蓝色")])
    return json.dumps(dict(protocol_version="1.1", source_refs=["TEST-event@1"], claim_proposals=[claim],
                           resume_proposals=[], reference_proposals=[]))


def test_consolidation_decoder_writes_numeric_offsets_as_the_same_utc_instant():
    value = _decode_consolidation_result(_timed_claim_result("2026-09-16T10:00:00+08:00", "2026-09-16T01:30:00.25-05:30"))
    claim = value["claim_proposals"][0]
    assert (claim["valid_from"], claim["valid_to"]) == ("2026-09-16T02:00:00Z", "2026-09-16T07:00:00.25Z")
    unchanged = _decode_consolidation_result(_timed_claim_result("2026-09-16T02:00:00+00:00", "2026-09-17T00:00:00Z"))
    assert (unchanged["claim_proposals"][0]["valid_from"], unchanged["claim_proposals"][0]["valid_to"]) == (
        "2026-09-16T02:00:00+00:00", "2026-09-17T00:00:00Z")


@pytest.mark.parametrize(
    "valid_from",
    [
        "2026-02-30T10:00:00+08:00",
        "2026-09-16T10:00:00+24:00",
        "2026-09-16T10:00:00+08:60",
        "2026-09-16T10:00:00+0800",
        "2026-09-16T10:00:00-00:00",
        "2026-09-16T10:00:00",
        "2026-09-16",
    ],
)
def test_consolidation_decoder_still_rejects_invalid_timestamps(valid_from):
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        _decode_consolidation_result(_timed_claim_result(valid_from))


def test_offset_timestamp_does_not_discard_the_batch_and_is_stored_in_utc(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色 蓝色。TEST-project 主题 深色。", when="2026-09-16T02:00:00Z")
    _mark_embed_done(core)

    def builder(sources, episode_ref=None):
        page = sources[0]
        return consolidation_payload(page, claims=[
            draft(page, "蓝色", quote_override="TEST-project 配色 蓝色。", valid_from="2026-09-16T02:00:00Z"),
            draft(page, "深色", predicate="主题", quote_override="TEST-project 主题 深色。",
                  valid_from="2026-09-16T10:00:00+08:00"),
        ])

    receipt = core.drain_worker(ctx, consolidation=FakeConsolidation(builder), max_items=1, remaining_seconds=5)
    assert receipt.completed == 1
    with sqlite3.connect(core.storage.path) as db:
        stored = {json.loads(payload)["value_text"]: (json.loads(payload)["valid_from"], column)
                  for payload, column in db.execute("SELECT payload_json,valid_from FROM claim_versions")}
    assert set(stored) == {"蓝色", "深色"}
    assert stored["深色"] == stored["蓝色"] == ("2026-09-16T02:00:00Z", "2026-09-16T02:00:00.000000+00:00")
    assert next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")[3] == "done"


def test_model_cannot_submit_evidence_outside_its_authorized_batch(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 本批来源。", key="TEST-worker/batch")
    unrelated = capture(core, ctx, "TEST 另一条来源。", key="TEST-worker/other", origin="assistant_visible")
    _mark_embed_done(core)

    class HallucinatingModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
            return json.dumps(consolidation_payload(unrelated), ensure_ascii=False)

    for attempt in range(1, 5):
        clock.advance(seconds=65.0, iso=f"2026-09-06T12:{attempt:02d}:30Z")
        core.drain_worker(ctx, consolidation=HallucinatingModel(), max_items=4, remaining_seconds=5)
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    # D4 retains invalid derivation as failed/needs-review after one extra
    # automatic attempt; unauthorized evidence must never become done/source_only.
    assert row[3] == "failed" and row[6].lower().endswith("derivation_invalid")
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT count(*) FROM consolidation_outcomes").fetchone()[0] == 0


def test_M41_precise_anchor_survives_failed_derivation(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST定稿代码为：XAS-A_19.2-beta。")

    class BadSchema:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            return json.dumps({"protocol_version": "1.1", "source_refs": [], "claim_proposals": [], "resume_proposals": [], "reference_proposals": []})

    core.drain_worker(ctx, consolidation=BadSchema(), max_items=2, remaining_seconds=5)
    assert core.search_sources(ctx, "XAS-A_19.2-beta", history=True)
    assert core.source(ctx, source.ref, 1) is not None


def test_fake_model_procedure_passes_source_validation(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "做TEST导出时，先检查透明背景；这是我认可的此类任务步骤。")

    def builder(sources, episode_ref=None):
        claim = procedure_proposal(sources[0])
        return consolidation_payload(sources[0], claims=[claim])

    model = FakeConsolidation(builder)
    receipt = core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    assert receipt.completed >= 1 and model.calls == 1
    with core.storage.read(ctx) as tx:
        claim_refs = tx.claims.list_refs(predicate="导出方法")
    assert claim_refs
    item = core.claim_history(ctx, claim_refs[0])[-1]
    assert item.state == "proposed"
    assert item.payload["procedure"]["method"] == ["先检查透明背景", "再导出"]


def test_M31_procedure_reuse_requires_accepted_method(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "做TEST导出时，先检查透明背景；这是我认可的此类任务步骤。")
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[procedure_proposal(sources[0])]))
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        claim_ref = tx.claims.list_refs(predicate="导出方法")[0]
    item = core.claim_history(ctx, claim_ref)[-1]
    assert item.payload["procedure"]["verification_basis"] == "user_accepted"


def test_M32_single_success_does_not_generalize_without_acceptance(worker_app):
    core, ctx, clock = worker_app
    observed = capture(core, ctx, "TEST 导出成功，exit.code=0。", origin="tool_observation")
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[procedure_proposal(sources[0], verification="observed_once")]))
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(predicate="导出方法")
    assert refs
    assert core.claim_history(ctx, refs[0])[-1].state == "proposed"


def test_M33_counterexample_kept_in_procedure(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 导出前先检查透明背景；未安装设备时不适用。这是我认可的方法。")
    counter = capture(core, ctx, "TEST 未安装设备时导出失败。", origin="tool_observation", when="2026-09-02T13:00:00Z")

    def builder(sources, episode_ref=None):
        claim = procedure_proposal(source)
        claim["procedure"]["counterexample_refs"] = [f"{counter.ref}@{counter.revision}"]
        claim["procedure"]["non_applicable"] = ["未安装设备"]
        return consolidation_payload(source, counter, claims=[claim])

    core.drain_worker(ctx, consolidation=FakeConsolidation(builder), max_items=4, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        ref = tx.claims.list_refs(predicate="导出方法")[0]
    payload = core.claim_history(ctx, ref)[-1].payload
    assert payload["procedure"]["non_applicable"] == ["未安装设备"]
    assert f"{counter.ref}@{counter.revision}" in payload["procedure"]["counterexample_refs"]


def test_M35_assistant_echo_does_not_promote_procedure(worker_app):
    core, ctx, clock = worker_app
    root = capture(core, ctx, "做TEST导出时，先检查透明背景；这是我认可的此类任务步骤。")
    echo = capture(core, ctx, root.event["content"], origin="assistant_visible", evidence_refs=[f"{root.ref}@1"])

    def builder(sources, episode_ref=None):
        return consolidation_payload(echo, claims=[procedure_proposal(echo, roots=[f"{root.ref}@1"])])

    core.drain_worker(ctx, consolidation=FakeConsolidation(builder), max_items=4, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(predicate="导出方法")
    assert not refs


def test_C13_worker_keeps_single_root_for_echo(worker_app):
    core, ctx, clock = worker_app
    root = capture(core, ctx, "TEST-project 配色 蓝色。")
    echo = capture(core, ctx, "TEST-project 配色 银色。", origin="memory_reinjection", evidence_refs=[f"{root.ref}@1"])
    with core.storage.read(ctx) as tx:
        roots = tx.claims.roots((f"{root.ref}@1", f"{echo.ref}@1"))
    assert [(r.ref, r.revision) for r in roots] == [(root.ref, 1)]


def test_no_repeat_reflection_without_new_sources(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "请帮我完成 TEST 巩固一次即可。")

    def builder(sources, episode_ref=None):
        refs = [f"{sources[0].ref}@{sources[0].revision}"]
        from scope_recall.core.episodes import source_watermark

        resume = dict(
            episode_ref=episode_ref,
            goal=dict(text=sources[0].event["content"], evidence_refs=refs),
            decisions=[],
            verified_progress=[],
            open_items=[],
            blockers=[],
            next_step=None,
            next_step_basis="unknown",
            artifact_refs=[],
            source_watermark=source_watermark(refs),
            evidence_refs=refs,
        )
        return consolidation_payload(sources[0], resume_proposals=[resume])

    model = FakeConsolidation(builder)
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    assert model.calls == 1
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE work_items SET state='pending',attempt=0,lease_token=0,lease_owner=NULL,lease_until=NULL WHERE work_type='consolidate' AND subject_ref=?",
            (source.ref,),
        )
        conn.commit()
    receipt = core.drain_worker(ctx, consolidation=model, max_items=4, remaining_seconds=10, owner_id="second-pass")
    assert model.calls == 1
    assert receipt.completed >= 1


def test_C09_deleted_source_obsoletes_late_worker_result(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 待巩固。", key="TEST-worker/delete")
    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=60, limit=1)[0]
    assert leased.subject_ref == source.ref
    authorize(core, ctx, source)
    core.forget(ctx, request(source), remaining_seconds=10)
    from scope_recall.core.worker import _process_consolidate

    class ValidModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            return json.dumps(consolidation_payload(sources[0]), ensure_ascii=False)

    disposition, code, state = _process_consolidate(
        core.storage, clock, ctx, leased, model=ValidModel(), started=clock.monotonic(), budget=10,
    )
    # forget fences and obsoletes outstanding work before this late worker
    # observes the deleted source; the stale disposition is the lease guard,
    # while the durable row must already be obsolete.
    assert disposition == "stale" and code == "authority_revoked" and state == "obsolete"
    assert work_rows(core)[0][3] == "obsolete"


def test_C35_stale_lease_cannot_overwrite_newer_completion(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST lease 竞争。")
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(sources[0]))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()

    with core.storage.write(ctx) as tx:
        first = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=0.001, limit=1)[0]
    clock.advance(seconds=1)
    with core.storage.write(ctx) as tx:
        tx.work.release_stale(clock.utc_now())
        second = tx.work.claim_next("worker-b", clock.utc_now(), lease_seconds=60, limit=1)[0]
    assert second.lease_token > first.lease_token
    from scope_recall.core.worker import _process_consolidate
    disposition, code, state = _process_consolidate(
        core.storage, clock, ctx, second, model=model,
        started=clock.monotonic(), budget=5,
    )
    assert disposition == "completed" and code is None and state == "done"
    with core.storage.write(ctx) as tx:
        stale = tx.work.complete(first.work_id, first.lease_token, "worker-a", now=clock.utc_now())
    assert stale.disposition == "stale"
    row = sqlite3.connect(core.storage.path).execute(
        "SELECT state,lease_token FROM work_items WHERE work_id=?", (first.work_id,)
    ).fetchone()
    assert row[0] == "done" and row[1] == second.lease_token


def test_fair_claim_orders_by_available_at(worker_app):
    core, ctx, clock = worker_app
    one = capture(core, ctx, "TEST 第一条。", key="TEST-fair/1")
    two = capture(core, ctx, "TEST 第二条。", key="TEST-fair/2")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET available_at=? WHERE subject_ref=?", ("2026-09-06T11:00:00Z", one.ref))
        conn.execute("UPDATE work_items SET available_at=? WHERE subject_ref=?", ("2026-09-06T12:00:00Z", two.ref))
        conn.commit()
    with core.storage.write(ctx) as tx:
        claimed = tx.work.claim_next("fair-worker", clock.utc_now(), lease_seconds=60, limit=1)[0]
    assert claimed.subject_ref == one.ref


def _iso(value: str, seconds: float) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def test_fresh_conversation_is_claimed_in_the_first_pass_beside_the_backlog(worker_app):
    from dataclasses import replace

    core, ctx, clock = worker_app
    clock.advance(iso="2026-09-05T12:00:00Z")
    backlog = [capture(core, replace(ctx, session_id=f"TEST-old/{index}"), f"TEST 昨天积压的事实 {index}。")
               for index in range(40)]
    clock.advance(iso="2026-09-06T12:00:00Z")
    first = capture(core, ctx, "TEST 刚刚说的第一句。")
    second = capture(core, ctx, "TEST 刚刚说的第二句。")
    other = capture(core, replace(ctx, session_id="TEST-other-chat"), "TEST 另一段对话刚刚说的话。")
    _mark_embed_done(core)
    batches = []

    def record(sources, **_):
        batches.append([source.ref for source in sources])
        return consolidation_payload(*sources)

    receipt = core.drain_worker(ctx, consolidation=FakeConsolidation(record), max_items=6, remaining_seconds=10)
    # FIFO and the lane alternate, FIFO first.  The lane takes the oldest fresh
    # item, whose episode batch still carries the rest of that conversation;
    # once no fresh work is left its turn falls back to the backlog.
    assert receipt.completed == 6
    assert batches == [[backlog[0].ref], [first.ref, second.ref], [backlog[1].ref], [other.ref],
                       [backlog[2].ref], [backlog[3].ref]]
    states = {row[1]: row[3] for row in consolidate_rows(core)}
    assert states[first.ref] == states[second.ref] == states[other.ref] == "done"
    assert [states[source.ref] for source in backlog].count("pending") == 36


def test_fresh_lane_keeps_purge_first_window_origins_and_fifo(worker_app):
    from scope_recall.core.work_storage import FRESH_LANE_SECONDS

    core, ctx, clock = worker_app
    now = clock.utc_now()
    clock.advance(iso=_iso(now, -FRESH_LANE_SECONDS - 1))
    outside = capture(core, ctx, "TEST 刚过两小时的消息。")
    clock.advance(iso=_iso(now, -FRESH_LANE_SECONDS))
    edge = capture(core, ctx, "TEST 恰好两小时前的消息。")
    clock.advance(iso=_iso(now, -60))
    reply = capture(core, ctx, "TEST 助手刚刚的回答。", origin="assistant_visible")
    clock.advance(iso=_iso(now, -30))
    document = capture(core, ctx, "TEST 刚刚导入的文档。", origin="external_document")
    clock.advance(iso=now)
    with core.storage.write(ctx) as tx:
        assert tx.work.enqueue("purge", "delete-test:TEST-scope", 1, available_at=now)
        order = [(item.subject_ref, item.work_type)
                 for item in tx.work.claim_next("TEST-lane", now, lease_seconds=60, limit=3, fresh_lane=True)]
        while claimed := tx.work.claim_next("TEST-lane", now, lease_seconds=60, fresh_lane=True):
            order.append((claimed[0].subject_ref, claimed[0].work_type))
    # Purge first; then fresh conversation oldest first -- an assistant reply is
    # fresh embedding work but not a consolidation root, and a document records
    # ingestion, not conversation; then plain FIFO once the lane is empty.
    assert order == [
        ("delete-test:TEST-scope", "purge"),
        (edge.ref, "consolidate"), (edge.ref, "embed"), (reply.ref, "embed"),
        (outside.ref, "consolidate"), (outside.ref, "embed"), (reply.ref, "consolidate"),
        (document.ref, "consolidate"), (document.ref, "embed"),
    ]


def test_fresh_lane_examines_a_bounded_page_of_recent_work(worker_app, monkeypatch):
    from scope_recall.core import work_storage

    core, ctx, clock = worker_app
    clock.advance(iso="2026-09-05T12:00:00Z")
    old = capture(core, ctx, "TEST 昨天积压的事实。")
    clock.advance(iso="2026-09-06T11:00:00Z")
    fresh = capture(core, ctx, "TEST 一小时前的新消息。")
    clock.advance(iso="2026-09-06T12:00:00Z")
    _mark_embed_done(core)
    with sqlite3.connect(core.storage.path) as conn:
        # Work made available after the fresh item, e.g. a refill of old sources.
        conn.executemany(
            """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
               VALUES ('consolidate',?,1,'TEST-scope',?,?,?)""",
            [(f"TEST-refilled/{index}", ctx.project_id, ctx.branch_id, clock.utc_now()) for index in range(2)])
    monkeypatch.setattr(work_storage, "FRESH_LANE_SCAN_ROWS", 2)
    with core.storage.write(ctx) as tx:
        blind = tx.work.claim_next("TEST-bounded", clock.utc_now(), lease_seconds=60, fresh_lane=True)
    monkeypatch.setattr(work_storage, "FRESH_LANE_SCAN_ROWS", 3)
    with core.storage.write(ctx) as tx:
        seen = tx.work.claim_next("TEST-bounded", clock.utc_now(), lease_seconds=60, fresh_lane=True)
    assert [blind[0].subject_ref, seen[0].subject_ref] == [old.ref, fresh.ref]


def test_concurrent_claim_gives_single_leased_item(worker_app, tmp_path):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 并发领取。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    barrier = Barrier(2)
    results = []

    def claim(owner):
        barrier.wait()
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            results.append(tx.work.claim_next(owner, clock.utc_now(), lease_seconds=60, limit=1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(claim, "worker-a")
        pool.submit(claim, "worker-b")
    leased = [item for batch in results for item in batch]
    assert len(leased) == 1


def test_rebuild_projection_is_the_existing_queue_type(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST projection rebuild anchor。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM lexical_postings WHERE source_id=(SELECT source_id FROM source_events WHERE event_id=? AND source_revision=?)", (source.ref, source.revision))
        conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('consolidate','embed') AND subject_ref=?", (source.ref,))
        conn.commit()
    with core.storage.write(ctx) as tx:
        assert tx.work.enqueue("rebuild_projection", source.ref, source.revision, available_at=clock.utc_now())
    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="projection-worker")
    assert receipt.completed == 1
    with core.storage.read(ctx) as tx:
        lexical, _ = tx.source_projection_status(source.ref, source.revision)
    assert lexical == "ready"


def test_rebuild_projection_claim_revision_uses_same_queue(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    claim = accept(core, ctx, draft(source)).items[0]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('consolidate','embed')")
        conn.commit()
    row = next(row for row in work_rows(core) if row[0] == "rebuild_projection")
    assert row[1] == claim.ref and row[2] == claim.revision
    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="claim-projection-worker")
    assert receipt.completed == 1
    assert next(row for row in work_rows(core) if row[0] == "rebuild_projection")[3] == "done"


def test_C34_interrupted_capture_persists_source_and_pending_work(worker_app):
    core, ctx, clock = worker_app
    saved = capture(core, ctx, "TEST 部分输出", capture_state="partial")
    assert saved.event["capture_state"] == "partial"
    assert core.source(ctx, saved.ref, 1).event["capture_state"] == "partial"
    assert any(r[3] == "pending" for r in work_rows(core))

    class MustNotConsolidate:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            raise AssertionError("partial capture must not reach consolidation model")

    core.drain_worker(ctx, consolidation=MustNotConsolidate(), max_items=1, remaining_seconds=5)


def test_embed_none_retries_without_marking_done(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 向量延后。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-only", embed=None)
    row = [r for r in work_rows(core) if r[0] == "embed"][0]
    assert row[3] == "pending" and row[4] == 0 and row[6] is None
    assert receipt.processed == 0 and receipt.unavailable_work_types == ("embed",)
    with core.storage.read(ctx) as tx:
        lexical, semantic = tx.source_projection_status(row[1], row[2])
    assert lexical == "ready" and semantic == "pending"


def test_embed_port_completes_semantic_projection(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 向量完成。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()

    class OkEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            return {"ref": source.ref, "revision": source.revision}

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            assert prepared == {"ref": source.ref, "revision": source.revision}
            assert lease_guard()

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-only", embed=OkEmbed())
    row = [r for r in work_rows(core) if r[0] == "embed"][0]
    assert row[3] == "done" and receipt.completed == 1 and receipt.items[0].state == "done"
    with core.storage.read(ctx) as tx:
        lexical, semantic = tx.source_projection_status(source.ref, source.revision)
    assert lexical == "ready" and semantic == "ready"


def test_embed_prepare_new_revision_blocks_publish(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST embed 原版本。", key="TEST-embed/revision", revision=1)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    published = {"called": False}

    class RevisionEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            capture(core, ctx, "TEST embed 新版本。", key="TEST-embed/revision", revision=2)
            return {"ref": source.ref, "revision": source.revision}

        def publish_source(self, *args, **kwargs):
            published["called"] = True

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-revision-fence", embed=RevisionEmbed())
    row = [r for r in work_rows(core) if r[0] == "embed" and r[1] == source.ref and r[2] == 1][0]
    assert published["called"] is False
    assert row[3] != "done"
    assert receipt.obsolete == 1 or receipt.stale == 1


def test_embed_prepare_suppression_blocks_publish(worker_app):
    """Suppressing the subject while it is being embedded blocks publication.

    This was ``test_embed_prepare_epoch_flip_blocks_publish`` and bumped
    ``memory_epoch`` by hand.  Every capture bumps it, so a bare epoch move no
    longer blocks publication (test_unrelated_capture_during_embedding_still_publishes);
    the authority change the bump stood in for is performed instead, with the
    same outcome.
    """
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST embed epoch fence。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    published = {"called": False}

    class SuppressingEmbed:
        def prepare_source(self, subject, *, remaining_seconds=1.0):
            authorize(core, ctx, source, mode="suppress")
            core.forget(ctx, request(source, mode="suppress"), remaining_seconds=10)
            return {"ref": subject.ref, "revision": subject.revision}

        def publish_source(self, *args, **kwargs):
            published["called"] = True

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-epoch-fence", embed=SuppressingEmbed())
    row = [r for r in work_rows(core) if r[0] == "embed" and r[1] == source.ref][0]
    assert published["called"] is False
    assert row[3] == "pending" and row[6] == "memory_epoch_changed"
    assert receipt.retried == 1


def test_embed_prepare_delete_blocks_publish(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST embed delete fence。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    published = {"called": False}

    class DeletingEmbed:
        def prepare_source(self, subject, *, remaining_seconds=1.0):
            authorize(core, ctx, source)
            core.forget(ctx, request(source), remaining_seconds=10)
            return {"ref": subject.ref, "revision": subject.revision}

        def publish_source(self, *args, **kwargs):
            published["called"] = True

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-delete-fence", embed=DeletingEmbed())
    row = [r for r in work_rows(core) if r[0] == "embed" and r[1] == source.ref][0]
    assert published["called"] is False
    assert row[3] == "obsolete"
    assert receipt.completed == 0 and receipt.retried == 0 and receipt.stale + receipt.obsolete == 1


def test_embed_prepare_deadline_does_not_open_guard_or_publish(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST embed deadline fence。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    published = {"called": False}

    class SlowPrepare:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            clock.advance(seconds=10)
            return {"ref": source.ref, "revision": source.revision}

        def publish_source(self, *args, **kwargs):
            published["called"] = True

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-deadline-fence", embed=SlowPrepare())
    row = [r for r in work_rows(core) if r[0] == "embed"][0]
    assert published["called"] is False
    assert receipt.skipped == 1
    assert row[3] != "done"


def test_M44_repeat_drain_without_new_capture_skips_model(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "请帮我完成 TEST 无新增不反思。")

    def builder(sources, episode_ref=None):
        refs = [f"{sources[0].ref}@{sources[0].revision}"]
        from scope_recall.core.episodes import source_watermark

        resume = dict(
            episode_ref=episode_ref,
            goal=dict(text=sources[0].event["content"], evidence_refs=refs),
            decisions=[],
            verified_progress=[],
            open_items=[],
            blockers=[],
            next_step=None,
            next_step_basis="unknown",
            artifact_refs=[],
            source_watermark=source_watermark(refs),
            evidence_refs=refs,
        )
        return consolidation_payload(sources[0], resume_proposals=[resume])

    model = FakeConsolidation(builder)
    first = core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    assert model.calls == 1 and first.completed >= 1
    calls = model.calls
    second = core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10, owner_id="pass-2")
    assert model.calls == calls
    assert second.idle or second.skipped >= 1


def intention_proposal(source, *, state="pending", value="提醒检查散热"):
    return dict(
        kind="intention",
        subject="TEST-project",
        predicate="散热检查",
        value_text=value,
        conditions=["TEST-project 验收"],
        statement_kind="request",
        valid_from=source.event["occurred_at"],
        valid_to=None,
        evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"])],
        intention=dict(
            cue="验收前",
            target="检查散热",
            conditions=["TEST-project 验收"],
            state=state,
            state_evidence_refs=[f"{source.ref}@{source.revision}"],
        ),
    )


def claim_count(core, ctx):
    with core.storage.read(ctx) as tx:
        return len(tx.claims.list_refs())


def test_consolidation_barrier_old_worker_cannot_mutate_after_lease_stolen(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST barrier consolidate。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    epoch_before = core.status(ctx).memory_epoch
    claims_before = claim_count(core, ctx)
    barrier = Barrier(2)

    class BlockingModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            barrier.wait()
            barrier.wait()
            return json.dumps(
                consolidation_payload(sources[0], claims=[procedure_proposal(sources[0])]),
                ensure_ascii=False,
            )

    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=0.001, limit=1)[0]
    assert leased.subject_ref == source.ref

    from scope_recall.core.worker import _process_consolidate

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _process_consolidate,
            core.storage,
            clock,
            ctx,
            leased,
            model=BlockingModel(),
            started=clock.monotonic(),
            budget=30,
        )
        barrier.wait()
        clock.advance(seconds=1)
        with core.storage.write(ctx) as tx:
            tx.work.release_stale(clock.utc_now())
            tx.work.claim_next("worker-b", clock.utc_now(), lease_seconds=60, limit=1)
        barrier.wait()
        disposition, code, state = future.result()

    assert disposition in {"stale", "obsolete"}
    assert claim_count(core, ctx) == claims_before
    assert core.status(ctx).memory_epoch == epoch_before
    with core.storage.read(ctx) as tx:
        assert not tx.claims.list_refs(predicate="导出方法")


def _restore(core, ctx, backup):
    """The operator restore sequence: checkpoint, close admission, copy back, replay."""
    from scope_recall.core.restore import (
        InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger,
    )
    from test_v11_deletion import sqlite_backup

    authority = InstallationMaintenance(ctx)
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup, core.storage.path)
    replay_deletion_ledger(core.storage, authority, ledger)


def test_C10_restore_epoch_fences_late_consolidation(worker_app, tmp_path):
    """A restore during the model call that replays a suppression of the batch source.

    This test used to bump ``memory_epoch`` by hand to stand in for a restore.
    Every capture bumps it too, so a bare epoch move no longer discards a paid
    result.  The restore is now performed for real -- backup, a suppression
    recorded in the deletion ledger, restore and replay -- and the late result
    is still discarded as a retryable ``memory_epoch_changed``.
    """
    from test_v11_deletion import sqlite_backup

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    epoch_before = core.status(ctx).memory_epoch
    barrier = Barrier(2)
    backup = tmp_path / "TEST-restore-during-model.sqlite3"

    class EpochFenceModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            barrier.wait()
            sqlite_backup(core.storage.path, backup)
            authorize(core, ctx, source, mode="suppress")
            core.forget(ctx, request(source, mode="suppress"), remaining_seconds=10)
            _restore(core, ctx, backup)
            return json.dumps(consolidation_payload(sources[0], claims=[draft(source)]), ensure_ascii=False)

    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=60, limit=1)[0]

    from scope_recall.core.worker import _process_consolidate

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _process_consolidate,
            core.storage,
            clock,
            ctx,
            leased,
            model=EpochFenceModel(),
            started=clock.monotonic(),
            budget=30,
        )
        barrier.wait()
        disposition, code, state = future.result()

    assert disposition == "retry" and code == "memory_epoch_changed"
    assert core.status(ctx).memory_epoch > epoch_before
    assert core.source(ctx, source.ref, source.revision).suppressed
    assert claim_count(core, ctx) == 0
    row = next(row for row in work_rows(core) if row[0] == "consolidate" and row[1] == source.ref)
    assert row[3] == "pending" and row[6] == "memory_epoch_changed"


def test_restore_of_an_older_backup_during_extraction_fences_late_consolidation(worker_app, tmp_path):
    """A restore to a backup older than the pre-call read is detected by itself.

    Nothing the result cites changed: the source missing from the backup is in
    another project.  The rows the fence marked no longer exist, so the result
    is still discarded as it was when any epoch move discarded it.
    """
    from dataclasses import replace

    from test_v11_deletion import sqlite_backup

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    _mark_embed_done(core)
    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=60, limit=1)[0]
    assert leased.subject_ref == source.ref
    backup = tmp_path / "TEST-older-backup.sqlite3"
    sqlite_backup(core.storage.path, backup)
    capture(core, replace(ctx, project_id="TEST-elsewhere"), "TEST 备份之后的另一个项目消息。")

    class RestoringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            _restore(core, ctx, backup)
            return json.dumps(consolidation_payload(*sources, claims=[draft(source)]), ensure_ascii=False)

    from scope_recall.core.worker import _process_consolidate

    result = _process_consolidate(core.storage, clock, ctx, leased, model=RestoringModel(),
                                  started=clock.monotonic(), budget=30)
    assert result == ("retry", "memory_epoch_changed", "pending")
    assert claim_count(core, ctx) == 0


def test_restore_that_reuses_claim_version_rowids_fences_late_consolidation(worker_app, tmp_path):
    """A slot written after a restore can reuse the rowid the fence marked.

    Claim versions written during the call are found by rowid order.  Here the
    restored backup lacks the last version the fence saw, and the slot's new
    head is written into that same rowid; only the changed identity of the
    marked row shows the ordering can no longer be trusted.  Accepting would
    have filed the stale proposal as late history under the newer head.
    """
    from test_v11_deletion import sqlite_backup

    core, ctx, clock = worker_app
    subject = capture(core, ctx, "TEST-project 配色 蓝色。")
    unrelated = capture(core, ctx, "TEST-other 字体 宋体。")
    newer = capture(core, ctx, "TEST-project 配色 绿色。", when="2026-09-03T12:00:00Z")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE NOT (work_type='consolidate' AND subject_ref=?)", (subject.ref,))
        conn.commit()
    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=60, limit=1)[0]
    assert leased.subject_ref == subject.ref
    backup = tmp_path / "TEST-rowid-backup.sqlite3"
    sqlite_backup(core.storage.path, backup)
    accept(core, ctx, draft(unrelated, "宋体", subject="TEST-other", predicate="字体"))
    slot = {}

    class RestoringModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            _restore(core, ctx, backup)
            slot["ref"] = accept(core, ctx, draft(newer, "绿色")).items[0].ref
            return json.dumps(consolidation_payload(*sources, claims=[draft(subject)]), ensure_ascii=False)

    from scope_recall.core.worker import _process_consolidate

    result = _process_consolidate(core.storage, clock, ctx, leased, model=RestoringModel(),
                                  started=clock.monotonic(), budget=30)
    assert result == ("retry", "memory_epoch_changed", "pending")
    assert [version.payload["value_text"] for version in core.claim_history(ctx, slot["ref"])] == ["绿色"]


def test_release_stale_and_claim_respect_trusted_context(worker_app):
    from dataclasses import replace

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST context fence。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET project_id=? WHERE subject_ref=?", ("OTHER-project", source.ref))
        conn.commit()
    with core.storage.write(ctx) as tx:
        assert tx.work.claim_next("worker", clock.utc_now(), lease_seconds=0.001, limit=4) == ()
    clock.advance(seconds=1)
    with core.storage.write(ctx) as tx:
        assert tx.work.release_stale(clock.utc_now()) == 0
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    assert row[3] == "pending"
    other_ctx = replace(ctx, project_id="OTHER-project")
    with core.storage.write(other_ctx) as tx:
        claimed = tx.work.claim_next("other-worker", clock.utc_now(), lease_seconds=60, limit=1)
    assert len(claimed) == 1


def test_expired_lease_cannot_mutate_cross_scope_work(worker_app):
    from dataclasses import replace

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST lease expiry fence。")
    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=0.001, limit=1)[0]
    clock.advance(seconds=1)
    other_ctx = replace(ctx, project_id="OTHER-project")
    with core.storage.write(other_ctx) as tx:
        mutation = tx.work.complete(leased.work_id, leased.lease_token, leased.lease_owner, now=clock.utc_now())
    assert mutation.disposition == "stale"
    row = next(row for row in work_rows(core) if row[1] == source.ref and row[0] == "consolidate")
    assert row[3] == "leased"


def test_lease_only_crash_fails_after_three_attempts(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST crash lease。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    for index in range(3):
        with core.storage.write(ctx) as tx:
            tx.work.claim_next(f"crash-{index}", clock.utc_now(), lease_seconds=0.001, limit=1)
        clock.advance(seconds=1)
        with core.storage.write(ctx) as tx:
            tx.work.release_stale(clock.utc_now())
    row = consolidate_rows(core)[0]
    assert row[3] == "failed" and row[4] == 3 and row[6] == "lease_exhausted"


def test_embed_barrier_cannot_publish_after_lease_stolen(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST embed barrier。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    barrier = Barrier(2)
    published = {"called": False}

    class BlockingEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            return {"ref": source.ref, "revision": source.revision}

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            barrier.wait()
            barrier.wait()
            if lease_guard():
                published["called"] = True

    with core.storage.write(ctx) as tx:
        leased = tx.work.claim_next("worker-a", clock.utc_now(), lease_seconds=0.001, limit=1)[0]

    from scope_recall.core.worker import _process_embed

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _process_embed,
            core.storage,
            clock,
            ctx,
            leased,
            embed=BlockingEmbed(),
            started=clock.monotonic(),
            budget=30,
        )
        barrier.wait()
        clock.advance(seconds=1)
        with core.storage.write(ctx) as tx:
            tx.work.release_stale(clock.utc_now())
            tx.work.claim_next("worker-b", clock.utc_now(), lease_seconds=60, limit=1)
        barrier.wait()
        disposition, code, state = future.result()

    assert published["called"] is False
    assert disposition == "stale"
    row = next(row for row in work_rows(core) if row[0] == "embed" and row[1] == source.ref)
    assert row[3] != "done"
    with core.storage.read(ctx) as tx:
        lexical, semantic = tx.source_projection_status(source.ref, source.revision)
    assert semantic != "ready"


def test_build_consolidation_model_passes_bounded_messages(worker_app):
    from scope_recall.core.worker import build_consolidation_model

    core, ctx, clock = worker_app
    captured = {}

    class SpyPort:
        def propose(self, messages, *, remaining_seconds=1.0):
            captured["messages"] = messages
            body = json.loads(messages[1]["content"])
            return json.dumps(
                consolidation_payload(core.source(ctx, body["sources"][0]["source_ref"], body["sources"][0]["source_revision"])),
                ensure_ascii=False,
            )

    source = capture(core, ctx, "TEST canonical request.")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    model = build_consolidation_model(SpyPort())
    receipt = core.drain_worker(ctx, consolidation=model, max_items=1, remaining_seconds=10)
    assert receipt.completed == 1
    body = json.loads(captured["messages"][1]["content"])
    record = body["sources"][0]
    assert body["source_refs"] == [f"{source.ref}@{source.revision}"]
    assert record["source_ref"] == source.ref and record["source_revision"] == source.revision
    assert record["origin"] == "human_direct" and record["content"] == source.event["content"]
    assert "source_watermark" in body
    assert len(captured["messages"][0]["content"].encode("utf-8")) + len(captured["messages"][1]["content"].encode("utf-8")) <= 16000


def test_worker_receipt_reports_persisted_state(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST receipt state。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    class Offline:
        def propose(self, *args, **kwargs):
            raise ConnectionError("TEST offline")
    receipt = core.drain_worker(ctx, consolidation=Offline(), max_items=1, remaining_seconds=5)
    assert receipt.retried == 1 and receipt.items[0].disposition == "retry" and receipt.items[0].state == "pending"


def test_deadline_expiry_returns_partial_receipt(worker_app):
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST deadline one。", key="TEST-deadline/1")
    capture(core, ctx, "TEST deadline two。", key="TEST-deadline/2")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='embed'")
        conn.commit()
    calls = {"count": 0}

    class SlowModel:
        def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
            calls["count"] += 1
            if calls["count"] == 1:
                clock.advance(seconds=10)
            return json.dumps(consolidation_payload(sources[0]), ensure_ascii=False)

    receipt = core.drain_worker(ctx, consolidation=SlowModel(), max_items=4, remaining_seconds=5)
    assert 1 <= receipt.processed < 4
    assert receipt.processed == len(receipt.items)
    assert receipt.completed + receipt.failed + receipt.retried + receipt.skipped + receipt.obsolete + receipt.stale == receipt.processed


def test_model_work_is_claimed_only_while_the_pass_covers_one_bounded_request(worker_app):
    from scope_recall.core.worker import FINALIZE_MARGIN_SECONDS, WorkerConfig, drain_worker

    core, ctx, clock = worker_app
    doomed = capture(core, ctx, "TEST 预算尾部删除。", key="TEST-reserve/doomed")
    authorize(core, ctx, doomed)
    core.forget(ctx, request(doomed), remaining_seconds=10)
    kept = capture(core, ctx, "TEST 预算尾部保留。", key="TEST-reserve/kept")

    class Purge:
        def purge_active(self, operation_id, *, receipt, remaining_seconds):
            return True

    class Embed:
        calls = 0

        def prepare_source(self, source, *, remaining_seconds=1.0):
            self.calls += 1
            return source.ref

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            assert lease_guard()

    def one_second_call(sources, **_):
        clock.advance(seconds=1)
        return consolidation_payload(*sources)

    model, embed = FakeConsolidation(one_second_call), Embed()
    reserve = 45.0 + FINALIZE_MARGIN_SECONDS

    def drain(remaining_seconds):
        receipt = drain_worker(core.storage, clock, ctx, config=WorkerConfig("TEST-reserve", request_seconds=45.0),
                               consolidation=model, embed=embed, purge=Purge(), remaining_seconds=remaining_seconds)
        return [(item.work_type, item.disposition) for item in receipt.items]

    def kept_work():
        return {row[0]: row[3:] for row in work_rows(core) if row[1] == kept.ref}

    # Below the reserve the purge still runs; model work is not even claimed.
    assert drain(reserve - 0.5) == [("purge", "completed")]
    assert model.calls == embed.calls == 0
    assert kept_work() == {"consolidate": ("pending", 0, 0, None), "embed": ("pending", 0, 0, None)}
    # A covered pass claims model work.  Once a call has spent the reserve the
    # embed is left pending untouched, while local work still uses the tail.
    with core.storage.write(ctx) as tx:
        assert tx.work.enqueue("rebuild_projection", kept.ref, kept.revision, available_at=clock.utc_now())
    assert drain(reserve) == [("consolidate", "completed"), ("rebuild_projection", "completed")]
    assert model.calls == 1 and embed.calls == 0
    assert kept_work()["embed"] == ("pending", 0, 0, None)
    # The next covered pass claims it as before.
    assert drain(reserve) == [("embed", "completed")]
    assert embed.calls == 1 and kept_work()["embed"][0] == "done"


def test_runtime_drain_reserves_its_own_request_bound(worker_app):
    from scope_recall.core.worker import FINALIZE_MARGIN_SECONDS
    from scope_recall.runtime.instance import RuntimeInstance, RuntimeInstanceConfig

    core, ctx, _clock = worker_app
    capture(core, ctx, "TEST 运行时请求上限。")
    _mark_embed_done(core)
    model = FakeConsolidation(lambda sources, **_: consolidation_payload(*sources))
    config = RuntimeInstanceConfig(binding=ctx.binding, session_id=ctx.session_id,
                                   allowed_scope_ids=ctx.allowed_scope_ids,
                                   project_id=ctx.project_id, branch_id=ctx.branch_id, request_seconds=20.0)
    runtime = RuntimeInstance(config=config, core=core, auxiliary=None)
    short = runtime.drain(consolidation=model, remaining_seconds=20.0 + FINALIZE_MARGIN_SECONDS - 1)
    assert short.idle and model.calls == 0
    assert consolidate_rows(core)[0][3:] == ("pending", 0, 0, None)
    covered = runtime.drain(consolidation=model, remaining_seconds=60.0)
    assert covered.completed == 1 and model.calls == 1


def test_M36_worker_intention_with_unproved_cue_stays_proposed(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    claim = intention_proposal(source)
    claim["intention"]["cue"] = "未出现的提示语"
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(sources[0], claims=[claim]))
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(predicate="散热检查")
    assert refs
    assert core.claim_history(ctx, refs[0])[-1].state == "proposed"


def test_M37_worker_negative_cancellation_request_stays_proposed(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    model = FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[intention_proposal(sources[0])]))
    core.drain_worker(ctx, consolidation=model, max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        ref = tx.claims.list_refs(predicate="散热检查")[0]
    negative = capture(core, ctx, "TEST-project 不要取消验收前检查散热的约定。")
    cancel = intention_proposal(negative, state="cancelled")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        negative, claims=[cancel])), max_items=2, remaining_seconds=10, owner_id="cancel-pass")
    history = core.claim_history(ctx, ref)
    assert core.current_claim(ctx, ref) is None
    assert history[0].state == history[-1].state == "proposed"
    assert history[0].payload["intention"]["state"] == "pending"


def test_M38_worker_intention_cancellation_request_stays_proposed(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[intention_proposal(sources[0])])), max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        ref = tx.claims.list_refs(predicate="散热检查")[0]
    cancel = capture(core, ctx, "TEST-project 散热检查约定取消了。", when="2026-09-03T12:00:00Z")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        cancel, claims=[intention_proposal(cancel, state="cancelled")])), max_items=2, remaining_seconds=10, owner_id="cancel")
    history = core.claim_history(ctx, ref)
    assert core.current_claim(ctx, ref) is None
    assert history[0].state == history[-1].state == "proposed"
    assert history[0].payload["intention"]["state"] == "pending"


@pytest.mark.parametrize("origin,text", [("assistant_visible", "TEST-project 提醒你检查散热。"), ("human_direct", "TEST-project 已提醒，但尚未完成散热检查。")])
def test_M40_worker_reminder_does_not_promote_request_intention(worker_app, origin, text):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[intention_proposal(sources[0])])), max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        ref = tx.claims.list_refs(predicate="散热检查")[0]
    reminder = capture(core, ctx, text, origin=origin, when="2026-09-03T12:00:00Z")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        reminder, claims=[intention_proposal(reminder, state="completed")])), max_items=2, remaining_seconds=10, owner_id="reminder")
    history = core.claim_history(ctx, ref)
    assert core.current_claim(ctx, ref) is None
    assert history[0].state == history[-1].state == "proposed"
    assert history[0].payload["intention"]["state"] == "pending"


def test_M39_worker_expired_intention_without_valid_to_stays_proposed(worker_app):
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    claim = intention_proposal(source, state="expired")
    core.drain_worker(ctx, consolidation=FakeConsolidation(lambda sources, episode_ref=None: consolidation_payload(
        sources[0], claims=[claim])), max_items=2, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(predicate="散热检查")
    assert refs
    assert core.claim_history(ctx, refs[0])[-1].state == "proposed"


def test_a_port_refusal_names_its_reason(worker_app):
    """A companion without a fenced write refused every embed with
    STORAGE_UNAVAILABLE and the field ``fenced_upsert_unsupported``; only the
    code reached the operator (#85).  The field is recorded with it."""
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 向量拒绝。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()

    class RefusingEmbed:
        def prepare_source(self, source, *, remaining_seconds=1.0):
            return {"ref": source.ref}

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            raise ContractError("STORAGE_UNAVAILABLE", "fenced_upsert_unsupported")

    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=5, owner_id="embed-refused", embed=RefusingEmbed())
    assert receipt.retried == 1 and receipt.items[0].error_code == "STORAGE_UNAVAILABLE"
    with sqlite3.connect(core.storage.path) as conn:
        detail = conn.execute(
            "SELECT stage,error_code,error_field FROM work_error_details ORDER BY detail_id DESC LIMIT 1"
        ).fetchone()
    assert detail == ("process", "STORAGE_UNAVAILABLE", "fenced_upsert_unsupported")
