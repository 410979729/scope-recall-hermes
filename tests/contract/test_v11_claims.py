"""P05 state contracts over captured synthetic text, not end-to-end model scores."""
from copy import deepcopy
from dataclasses import replace
import itertools
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scope_recall.contracts import ContractError, ImportProvenance, import_source_fingerprint
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import Qualification, qualify
from v11_support import context, source_event


class Clock:
    now = "2026-09-06T12:00:00Z"
    def utc_now(self): return self.now
    def monotonic(self): return time.monotonic()


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-claims"),project_id="TEST-project",branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding),clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core,ctx


def capture(core,ctx,raw,*,origin="human_direct",key=None,revision=1,when="2026-09-01T12:00:00Z",attested=False,**changes):
    event = source_event(source_event_key=key or f"TEST-claims/{next(core.test_sequence)}",source_revision=revision,
                         origin=origin,role="tool" if origin == "tool_observation" else "document" if origin in {"external_document","imported"} else "user",
                         content=raw,occurred_at=when,time_precision="unknown" if when is None else "instant",**changes)
    importer = ImportProvenance("human_direct","a"*64,frozenset({import_source_fingerprint(event)})) if attested else None
    actor = replace(ctx,actor_origin=origin,import_provenance=importer)
    saved = core.record_event(actor,event,scope_id="TEST-scope",remaining_seconds=10)
    assert saved.durability == "persisted"
    return core.source(ctx,saved.event_refs[0].ref,revision)


def draft(source, value="蓝色", *, quote_override=None, **changes):
    quote = source.event["content"] if quote_override is None else quote_override
    result = dict(kind="decision",subject="TEST-project",predicate="配色",value_text=value,conditions=[],
                  statement_kind="decision",valid_from=source.event["occurred_at"],valid_to=None,
                  evidence_spans=[dict(source_ref=source.ref,source_revision=source.revision,quote=quote)])
    result.update(changes)
    return result


def accept(core,ctx,*claims):
    refs = list(dict.fromkeys(f"{s['source_ref']}@{s['source_revision']}" for c in claims for s in c["evidence_spans"]))
    for c in claims:
        refs.extend(r for r in c.get("intention",{}).get("state_evidence_refs",[]) if r not in refs)
        refs.extend(r for r in c.get("procedure",{}).get("counterexample_refs",[]) if r not in refs)
    return core.accept_claim_proposals(ctx,dict(protocol_version="1.1",source_refs=refs,claim_proposals=list(claims),
                                              resume_proposals=[],reference_proposals=[]),scope_id="TEST-scope",remaining_seconds=10)


def initial(core,ctx,*,value="蓝色",when="2026-09-01T12:00:00Z",**changes):
    source = capture(core,ctx,f"TEST-project 配色 {value}。",when=when)
    item = accept(core,ctx,draft(source,value,**changes)).items[0]
    assert item.state == "active"
    return item,source


def revise_request(item,source,value="银色",**changes):
    request = dict(protocol_version="1.1",target_ref=item.ref,expected_revision=item.revision,new_value=value,
                   conditions=[],source_evidence_refs=[f"{source.ref}@{source.revision}"],valid_from=source.event["occurred_at"])
    request.update(changes)
    return request


def counts(core):
    with sqlite3.connect(core.storage.path) as conn:
        return {t:conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("claims","claim_versions","evidence_links","work_items")}


def test_C04_M26_capture_explicit_attribute_correction_is_immediate_without_vectors(app):
    core,ctx = app
    item,_ = initial(core,ctx,value="H100",kind="fact",predicate="配色")
    class NoModels:
        def __getattr__(self,key): raise AssertionError("mutation cannot call models")
    core.vectors = core.consolidation = NoModels()
    correction = capture(core,ctx,"刚才写错了，TEST-project 用H200。",when="2026-09-03T12:00:00Z")
    current = core.current_claim(ctx,item.ref)
    assert current.payload["value_text"] == "H200" and current.revision == 2
    history = core.claim_history(ctx,item.ref)
    assert history[0].payload["value_text"] == "H100" and history[1].replaces_revision == 1
    assert history[0].recorded_to == history[1].recorded_from
    assert core.source(ctx,correction.ref,1).event["content"] == "刚才写错了，TEST-project 用H200。"
    with sqlite3.connect(core.storage.path) as conn:
        work = conn.execute("SELECT subject_revision,work_type,state FROM work_items WHERE subject_ref=? ORDER BY subject_revision,work_type",(item.ref,)).fetchall()
    # Every claim head is queued for the vector index too, so search can reach
    # the derived layer instead of depending on relation expansion out of some
    # event that happened to be retrieved first. Naming the work types keeps the
    # property explicit rather than implied by a row count: the superseded
    # revision's work is obsoleted and the new revision's is queued — and none
    # of it called a model, which NoModels above would have caught.
    assert work == [(1,"embed","obsolete"),(1,"rebuild_projection","obsolete"),
                    (2,"embed","pending"),(2,"rebuild_projection","pending")]


def test_C05_late_import_cannot_replace_current_and_recorded_time_does_not_become_valid_time(app):
    core,ctx = app
    current,_ = initial(core,ctx,value="银色",when="2026-09-03T12:00:00Z")
    old = capture(core,ctx,"TEST-project 配色 蓝色。",origin="imported",source_original_origin="human_direct",attested=True)
    inserted = accept(core,ctx,draft(old)).items[0]
    assert inserted.disposition == "historical" and inserted.revision == 2
    assert core.current_claim(ctx,current.ref).payload["value_text"] == "银色"
    assert core.current_claim(ctx,current.ref,as_of="2026-09-02T12:00:00Z").payload["value_text"] == "蓝色"
    history = core.claim_history(ctx,current.ref)
    assert history[1].valid_from != history[1].recorded_from and history[1].replaces_revision is None
    assert all(v.current_revision == 1 for v in history)


def test_unknown_time_is_not_filled_from_recorded_and_cannot_answer_past(app):
    core,ctx = app
    item,_ = initial(core,ctx,when=None)
    version = core.current_claim(ctx,item.ref)
    assert version.valid_from is None and version.valid_to is None
    assert core.current_claim(ctx,item.ref,as_of="2026-09-01T12:00:00Z") is None


def test_C06_C07_scope_project_branch_are_read_and_evidence_boundaries(app):
    core,ctx = app
    item,source = initial(core,ctx)
    for other in (replace(ctx,project_id="TEST-other"),replace(ctx,branch_id="TEST-exp")):
        assert core.current_claim(other,item.ref) is None
        assert core.claim_history(other,item.ref) == ()
        with pytest.raises(ContractError,match="SOURCE_MISSING"):
            accept(core,other,draft(source))
    with core.storage.write(replace(ctx,project_id="TEST-other")) as tx:
        with pytest.raises(ContractError,match="ACCESS_DENIED"):
            tx.claims.require_target(core.claim_history(ctx,item.ref)[0])


def test_conditional_variants_and_C23_C24_constraints_coexist(app):
    core,ctx = app
    constrained = capture(core,ctx,"TEST-project 未授权不改网站，只限写文案任务。")
    c = accept(core,ctx,draft(constrained,"不改网站",kind="constraint",predicate="网站修改",conditions=["未授权","写文案任务"])).items[0]
    allowed = capture(core,ctx,"TEST-project 沙箱已授权修改网页，只限TEST沙箱。",when="2026-09-03T12:00:00Z")
    a = accept(core,ctx,draft(allowed,"修改网页",kind="constraint",predicate="网站修改",conditions=["TEST沙箱","已授权"])).items[0]
    assert c.state == a.state == "active" and c.ref != a.ref
    assert core.current_claim(ctx,c.ref).payload["conditions"] == ["未授权","写文案任务"]
    assert core.current_claim(ctx,a.ref).payload["conditions"] == ["TEST沙箱","已授权"]


def test_M27_finite_exception_expires_and_does_not_permanently_change_regular_rule(app):
    core,ctx = app
    item,_ = initial(core,ctx,value="白底")
    source = capture(core,ctx,"TEST-project 配色 今天破例用黑底，只限今天。",when="2026-09-06T00:00:00Z")
    exception = accept(core,ctx,draft(source,"黑底",valid_to="2026-09-07T00:00:00Z")).items[0]
    assert exception.state == "active" and exception.ref == item.ref
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "黑底"
    core.clock.now = "2026-09-07T00:00:00Z"
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "白底"


def test_M28_future_rule_and_recorded_asof_use_distinct_axes(app):
    core,ctx = app
    core.clock.now = "2026-09-01T12:00:00Z"
    item,_ = initial(core,ctx,value="短版")
    core.clock.now = "2026-09-02T12:00:00Z"
    source = capture(core,ctx,"从2026年9月3日起，TEST-project 配色 详细版。",when="2026-09-02T12:00:00Z")
    next_version = accept(core,ctx,draft(source,"详细版",valid_from="2026-09-03T00:00:00Z")).items[0]
    assert next_version.state == "active"
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "短版"
    assert core.current_claim(ctx,item.ref,as_of="2026-09-04T00:00:00Z").payload["value_text"] == "详细版"
    assert core.current_claim(ctx,item.ref,as_of="2026-09-04T00:00:00Z",known_at="2026-09-01T15:00:00Z").payload["value_text"] == "短版"
    # Original valid intervals remain immutable, with replacement links deciding
    # effective versions at the requested time/knowledge boundary.
    assert core.claim_history(ctx,item.ref)[0].valid_to is None


def test_invented_dates_and_unknown_historical_time_stay_proposed(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色 蓝色。",when=None)
    assert accept(core,ctx,draft(source,valid_from="2026-08-01T00:00:00Z")).items[0].state == "proposed"
    past = capture(core,ctx,"TEST-project 以前配色 银色。",when=None)
    assert accept(core,ctx,draft(past,"银色")).items[0].state == "proposed"


def test_M29_observation_basis_does_not_claim_present_verification(app):
    core,ctx = app
    source = capture(core,ctx,"2026年8月1日TEST-project 报价 100单位，这是当日报价。",origin="tool_observation",when="2026-08-01T00:00:00Z")
    item = accept(core,ctx,draft(source,"100单位",kind="fact",predicate="报价")).items[0]
    version = core.current_claim(ctx,item.ref)
    assert version.basis == "observed" and version.reason == "observation_at_source_time"
    assert version.valid_from == "2026-08-01T00:00:00.000000+00:00"
    # Packet-level stale-observation labels and actual host answers are P06/P09.


def test_M30_external_conflicting_unknown_order_becomes_disputed_regardless_scores(app):
    core,ctx = app
    a = capture(core,ctx,"TEST-project 验收 完成。",origin="external_document",when=None)
    one = accept(core,ctx,draft(a,"完成",kind="fact",predicate="验收")).items[0]
    b = capture(core,ctx,"TEST-project 验收 失败。",origin="external_document",when=None)
    two = accept(core,ctx,draft(b,"失败",kind="fact",predicate="验收")).items[0]
    assert one.state == "active" and two.state == "disputed"
    version = core.current_claim(ctx,one.ref)
    assert version.state == "disputed" and version.conflict_revisions == (1,)
    assert len(core.claim_history(ctx,one.ref)) == 2


def test_same_assertion_candidate_collision_is_zero_mutation(app):
    core,ctx = app
    item,first = initial(core,ctx)
    another = capture(core,ctx,"TEST-project 配色 蓝色。")
    before = core.storage.path.read_bytes()
    after = accept(core,ctx,draft(another))
    assert after.items[0].disposition == "duplicate" and after.items[0].revision == 1
    assert core.storage.path.read_bytes() == before
    assert core.claim_history(ctx,item.ref)[0].payload["evidence_spans"][0]["source_ref"] == first.ref


def test_proposed_collision_cannot_demote_active_or_advance_pointer(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    synthetic = capture(core,ctx,"假设TEST-project 配色 银色。",origin="assistant_visible")
    proposal = accept(core,ctx,draft(synthetic,"银色")).items[0]
    assert proposal.state == "proposed" and proposal.disposition == "historical"
    assert core.current_claim(ctx,item.ref).revision == 1


@pytest.mark.parametrize("origin",["assistant_visible","host_generated","memory_reinjection","origin_unknown"])
def test_C13_derived_echoes_have_one_root_without_new_authority_or_literal_laundering(app,origin):
    core,ctx = app
    item,source = initial(core,ctx)
    echo = capture(core,ctx,"TEST-project 配色 银色。",origin=origin,evidence_refs=[f"{source.ref}@1"])
    with core.storage.read(ctx) as tx:
        roots = tx.claims.roots((f"{source.ref}@1",f"{echo.ref}@1"))
    assert [(r.ref,r.revision) for r in roots] == [(source.ref,1)]
    assert accept(core,ctx,draft(echo,"银色")).items[0].state == "proposed"
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "蓝色"


def test_C15_assistant_success_does_not_override_tool_failure(app):
    core,ctx = app
    failure = capture(core,ctx,"TEST-project 测试 失败。",origin="tool_observation")
    item = accept(core,ctx,draft(failure,"失败",kind="fact",predicate="测试")).items[0]
    success = capture(core,ctx,"TEST-project 测试 通过。",origin="assistant_visible")
    assert accept(core,ctx,draft(success,"通过",kind="fact",predicate="测试")).items[0].state == "proposed"
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "失败"


def test_imported_role_and_original_label_require_runtime_manifest_attestation(app):
    core,ctx = app
    forged = capture(core,ctx,"TEST-project 配色 蓝色。",origin="imported",source_original_origin="human_direct")
    assert accept(core,ctx,draft(forged)).items[0].state == "proposed"
    trusted = capture(core,ctx,"TEST-project 配色 银色。",origin="imported",source_original_origin="human_direct",attested=True)
    assert accept(core,ctx,draft(trusted,"银色")).items[0].state == "active"
    assert core.source(ctx,trusted.ref,1).import_provenance_sha256 == "a"*64


@pytest.mark.parametrize("state",["partial","gap"])
def test_incomplete_root_cannot_promote(app,state):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色 蓝色。",capture_state=state)
    assert accept(core,ctx,draft(source)).items[0].state == "proposed"


def test_quote_trimming_cannot_remove_hypothesis_or_negation(app):
    core,ctx = app
    source = capture(core,ctx,"假设TEST-project 配色 蓝色。")
    p = draft(source)
    p["evidence_spans"][0]["quote"] = "TEST-project 配色 蓝色"
    assert accept(core,ctx,p).items[0].state == "proposed"


def test_C36_ambiguous_correction_preserves_both_currents_and_raw_update(app):
    core,ctx = app
    one,_ = initial(core,ctx)
    two_source = capture(core,ctx,"TEST-project 字号 16。")
    two = accept(core,ctx,draft(two_source,"16",predicate="字号")).items[0]
    correction = capture(core,ctx,"那个不对，改一下。",when="2026-09-03T12:00:00Z")
    pending = core.unresolved_updates(ctx)
    assert len(pending) == 1 and set(pending[0]["candidate_refs"]) == {one.ref,two.ref}
    assert pending[0]["source_ref"] == correction.ref
    assert core.current_claim(ctx,one.ref).revision == core.current_claim(ctx,two.ref).revision == 1


def test_ambiguous_update_closes_once_the_user_settles_it_on_one_candidate(app):
    core,ctx = app
    one,one_source = initial(core,ctx)
    two_source = capture(core,ctx,"TEST-project 字号 16。")
    two = accept(core,ctx,draft(two_source,"16",predicate="字号")).items[0]
    capture(core,ctx,"那个不对，改一下。",when="2026-09-03T12:00:00Z")
    assert len(core.unresolved_updates(ctx)) == 1
    # The user names which one they meant.  ``resolved`` had no writer at all
    # before this, so the row stayed open forever and kept being handed to the
    # host on every read.
    settle = capture(core,ctx,"TEST-project 配色改为银色。",when="2026-09-04T12:00:00Z")
    assert core.current_claim(ctx,one.ref).payload["value_text"] == "银色"
    assert core.unresolved_updates(ctx) == ()
    with sqlite3.connect(core.storage.path) as conn:
        state,resolved_at = conn.execute("SELECT state,resolved_at FROM unresolved_updates").fetchone()
    assert state == "resolved" and resolved_at
    # The untouched candidate is left exactly as it was; settling one ambiguity
    # is not a licence to revise the other.
    assert core.current_claim(ctx,two.ref).revision == 1


def test_ambiguity_the_user_never_settled_is_still_reported(app):
    core,ctx = app
    initial(core,ctx)
    capture(core,ctx,"TEST-project 字号 16。")
    capture(core,ctx,"那个不对，改一下。",when="2026-09-03T12:00:00Z")
    open_before = {row["ref"] for row in core.unresolved_updates(ctx)}
    assert open_before
    # An unrelated later capture settles nothing, so the question the host still
    # has to ask stays on the list.  Closing rows is tied to an authorized
    # revision of a named candidate, not to time passing.
    capture(core,ctx,"今天天气不错。",when="2026-09-04T12:00:00Z")
    assert open_before <= {row["ref"] for row in core.unresolved_updates(ctx)}


def test_explicit_revision_compare_and_swap_and_no_silent_retries(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色。",when="2026-09-03T12:00:00Z")
    request = revise_request(item,source)
    result = core.revise(ctx,request,remaining_seconds=10)
    assert result.items[0].state == "active" and result.items[0].revision == 2
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="VERSION_CONFLICT"):
        core.revise(ctx,request,remaining_seconds=10)
    assert core.storage.path.read_bytes() == before


def test_two_real_writers_with_same_expected_revision_have_exactly_one_winner(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色。",when="2026-09-03T12:00:00Z")
    request = revise_request(item,source)
    barrier = Barrier(2)
    def write():
        barrier.wait()
        try:
            return core.revise(ctx,request,remaining_seconds=5).items[0].revision
        except Exception as exc:
            return getattr(exc,"code",type(exc).__name__)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:write(),range(2)))
    assert results.count(2) == 1 and len(core.claim_history(ctx,item.ref)) == 2
    assert any(x in {"VERSION_CONFLICT","TruthWriterBusyError"} for x in results if x != 2)


def test_old_human_quote_and_agent_echo_cannot_authorize_new_revision(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色。",when="2026-09-03T12:00:00Z")
    capture(core,ctx,"现在讨论另一件事情。",when="2026-09-04T12:00:00Z")
    with pytest.raises(ContractError,match="current_human_evidence"):
        core.revise(ctx,revise_request(item,source))
    assert core.current_claim(ctx,item.ref).revision == 1


def test_retraction_blocks_current_but_keeps_history_and_past(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    capture(core,ctx,"撤回TEST-project 配色蓝色。",when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx,item.ref) is None
    assert core.claim_history(ctx,item.ref)[-1].state == "retracted"
    assert core.current_claim(ctx,item.ref,as_of="2026-09-02T12:00:00Z").payload["value_text"] == "蓝色"
    old = capture(core,ctx,"TEST-project 配色 蓝色。",origin="imported",source_original_origin="human_direct",attested=True)
    assert accept(core,ctx,draft(old)).items[0].disposition == "duplicate"
    assert core.current_claim(ctx,item.ref) is None


def test_explicit_condition_variant_does_not_change_base(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色，只限TEST沙箱。",when="2026-09-03T12:00:00Z")
    result = core.revise(ctx,revise_request(item,source,conditions=["TEST沙箱"]),remaining_seconds=10)
    assert result.items[0].ref != item.ref
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "蓝色"


def test_stale_source_revision_is_fenced_before_any_derived_write(app):
    core,ctx = app
    old = capture(core,ctx,"TEST-project 配色 蓝色。",key="TEST-edited")
    capture(core,ctx,"TEST-project 配色 银色。",key="TEST-edited",revision=2)
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="VERSION_CONFLICT"):
        accept(core,ctx,draft(old))
    assert core.storage.path.read_bytes() == before


def test_batch_invalid_span_rolls_back_other_valid_proposals(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色 蓝色。")
    bad = draft(source,"银色")
    bad["evidence_spans"][0]["quote"] = "This was never said"
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="DERIVATION_INVALID"):
        accept(core,ctx,draft(source),bad)
    assert core.storage.path.read_bytes() == before and counts(core)["claims"] == 0


def test_sql_foreign_key_enforces_one_existing_head(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("UPDATE claims SET current_revision=999 WHERE claim_id=?",(item.ref,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.commit()
        conn.rollback()
    assert core.current_claim(ctx,item.ref).revision == 1


def intention(source,state="pending"):
    return draft(source,"提醒检查散热",kind="intention",predicate="散热检查",intention=dict(cue="验收前",target="检查散热",
                 conditions=["TEST-project 验收"],state=state,state_evidence_refs=[f"{source.ref}@{source.revision}"]))


def test_M38_intention_cancellation_uses_same_versions_and_does_not_erase_history(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 验收前提醒检查散热。")
    one = accept(core,ctx,intention(source)).items[0]
    cancel = capture(core,ctx,"TEST-project 散热检查约定取消了。",when="2026-09-03T12:00:00Z")
    two = accept(core,ctx,intention(cancel,"cancelled")).items[0]
    assert one.ref == two.ref and two.revision == 2 and two.state == "active"
    assert core.current_claim(ctx,one.ref).payload["intention"]["state"] == "cancelled"
    assert core.claim_history(ctx,one.ref)[0].payload["intention"]["state"] == "pending"


@pytest.mark.parametrize("origin,text",[("assistant_visible","TEST-project 提醒你检查散热。"),("human_direct","TEST-project 已提醒，但尚未完成散热检查。")])
def test_M40_reminder_does_not_complete_intention(app,origin,text):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 验收前提醒检查散热。")
    item = accept(core,ctx,intention(source)).items[0]
    reminder = capture(core,ctx,text,origin=origin,when="2026-09-03T12:00:00Z")
    attempt = accept(core,ctx,intention(reminder,"completed")).items[0]
    assert attempt.state == "proposed" and core.current_claim(ctx,item.ref).payload["intention"]["state"] == "pending"


def test_intention_actual_human_completion_and_procedure_evidence_are_qualified(app):
    core,ctx = app
    completed = capture(core,ctx,"TEST-project 散热检查已完成。")
    assert accept(core,ctx,intention(completed,"completed")).items[0].state == "active"
    source = capture(core,ctx,"TEST-project 验收前检查方法已认可，先检查温度，再验收。未安装设备时不适用。")
    procedure = draft(source,"检查方法",kind="procedure",predicate="验收方法",procedure=dict(conditions=["验收前"],non_applicable=["未安装设备"],
                      method=["先检查温度","再验收"],verification_basis="user_accepted",counterexample_refs=[]))
    item = accept(core,ctx,procedure).items[0]
    assert item.state == "active" and core.current_claim(ctx,item.ref).payload["procedure"]["non_applicable"] == ["未安装设备"]


def test_import_attestation_is_not_a_model_request_field(app):
    core,ctx = app
    item,source = initial(core,ctx)
    request = revise_request(item,source)
    request["import_provenance"] = {"original_origin":"human_direct","manifest_sha256":"a"*64}
    with pytest.raises(ContractError,match="INPUT_INVALID"):
        core.revise(ctx,request)


@pytest.mark.parametrize("raw",["不要撤回TEST-project 配色蓝色。","TEST-project 配色改为银色？","不要把TEST-project 配色改成银色。"])
def test_negative_and_questioned_operations_do_not_change_current(app,raw):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,raw,when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx,item.ref).revision == 1
    with pytest.raises(ContractError,match="revision_not_asserted"):
        core.revise(ctx,revise_request(item,source,None if "撤回" in raw else "银色"))


def test_question_marked_preference_source_cannot_become_active_claim(app):
    core,ctx = app
    source = capture(core,ctx,"我喜欢蓝色吗？")
    proposal = draft(source,"蓝色",kind="preference",predicate="颜色偏好",statement_kind="assertion")
    result = accept(core,ctx,proposal).items[0]
    assert result.state != "active"
    assert core.claim_history(ctx,result.ref)[0].state != "active"


def test_unpunctuated_question_revision_cannot_execute(app):
    core,ctx = app
    item,_ = initial(core,ctx,value="蓝色")
    source = capture(core,ctx,"把TEST-project 配色改成银色吗")
    assert core.current_claim(ctx,item.ref).revision==item.revision
    with pytest.raises(ContractError,match="revision_not_asserted"):
        core.revise(ctx,revise_request(item,source,"银色"))
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "蓝色"


def test_negative_cancellation_does_not_cancel_pending_intention(app):
    core,ctx=app
    source=capture(core,ctx,'TEST-project 验收前提醒检查散热。')
    pending=accept(core,ctx,intention(source)).items[0]
    negative=capture(core,ctx,'TEST-project 不要取消验收前检查散热的约定。')
    attempted=accept(core,ctx,intention(negative,'cancelled')).items[0]
    assert attempted.state=='proposed'
    assert core.current_claim(ctx,pending.ref).payload['intention']['state']=='pending'


def test_late_direct_event_cannot_use_capture_fastpath_to_replace_newer_current(app):
    core,ctx = app
    item,_ = initial(core,ctx,value="银色",when="2026-09-03T12:00:00Z")
    old = capture(core,ctx,"TEST-project 配色改为蓝色。",when="2026-09-01T12:00:00Z")
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "银色"
    assert core.unresolved_updates(ctx)[0]["source_ref"] == old.ref


def test_invented_method_steps_and_intention_targets_cannot_gain_active(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 方法已认可，先检查温度。")
    p = draft(source,"方法",kind="procedure",predicate="方法",procedure=dict(conditions=[],non_applicable=[],method=["部署到生产"],verification_basis="user_accepted",counterexample_refs=[]))
    assert accept(core,ctx,p).items[0].state == "proposed"
    p = intention(source)
    assert accept(core,ctx,p).items[0].state == "proposed"


def test_failure_after_head_evidence_epoch_and_work_updates_rolls_back_everything(app,monkeypatch):
    from scope_recall.core.claim_storage import Claims
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色。",when="2026-09-03T12:00:00Z")
    before = core.storage.path.read_bytes()
    append = Claims.append
    def fail_after(self,*args,**kwargs):
        append(self,*args,**kwargs)
        raise RuntimeError("TEST fault after mutation")
    monkeypatch.setattr(Claims,"append",fail_after)
    with pytest.raises(RuntimeError,match="TEST fault"):
        core.revise(ctx,revise_request(item,source))
    assert core.storage.path.read_bytes() == before
    assert core.current_claim(ctx,item.ref).revision == 1


def test_unrelated_hypothetical_sentence_cannot_demote_exact_asserted_span(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色 蓝色。别的项目银色只是一个假设方案。")
    p = draft(source)
    p["evidence_spans"][0]["quote"] = "TEST-project 配色 蓝色"
    assert accept(core,ctx,p).items[0].state == "active"


def test_import_attestation_cannot_be_reused_for_another_source_body_or_revision(app):
    core,ctx = app
    event = source_event(source_event_key="TEST-attested",origin="imported",source_original_origin="human_direct",content="TEST-project 配色 蓝色。")
    provenance = ImportProvenance("human_direct","a"*64,frozenset({import_source_fingerprint(event)}))
    actor = replace(ctx,actor_origin="imported",import_provenance=provenance)
    for changes in ({"content":"TEST-project 配色 银色。"},{"source_revision":2},{"source_event_key":"TEST-forged"}):
        with pytest.raises(ContractError,match="import_provenance"):
            core.record_event(actor,{**event,**changes},scope_id="TEST-scope",remaining_seconds=10)
    assert counts(core)["claims"] == 0 and core.status(ctx).sources == 0


def test_unknown_retraction_time_does_not_resurrect_known_old_fact_for_asof(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"TEST-project 配色蓝色需要撤回。",when=None)
    # Capture itself performs this unambiguous retraction without inventing time.
    assert core.current_claim(ctx,item.ref) is None
    assert core.current_claim(ctx,item.ref,as_of="2026-09-02T00:00:00Z") is None
    assert core.claim_history(ctx,item.ref)[-1].valid_from is None


def test_explicit_revision_cannot_borrow_unrelated_sentence_deadline(app):
    core,ctx = app
    item,_ = initial(core,ctx)
    source = capture(core,ctx,"Please correct TEST-project 配色: 银色。另一份红色方案到2026-09-30截止。",when="2026-09-03T12:00:00Z")
    with pytest.raises(ContractError,match="time_not_grounded"):
        core.revise(ctx,revise_request(item,source,{"value_text":"银色","valid_to":"2026-09-30T00:00:00Z"}))
    assert core.current_claim(ctx,item.ref).revision == 1


# The model is shown ``json.dumps(content, ensure_ascii=False)``, so a quote it
# copies faithfully can be the escaped form rather than the stored bytes.  On
# alpha, 109 of the 111 sources behind the evidence_span failures change under
# that encoding.


def test_encoded_origin_map_agrees_with_json_dumps_character_for_character():
    import json
    from scope_recall.core.evidence_quote import encoded_with_origin
    for raw in ('a"b', "line\nbreak", "path C:\\temp\\x", "tab\there",
                '中文 mixed "quote" and \\slash', "control" + chr(1) + "char"):
        encoded, origin = encoded_with_origin(raw)
        assert encoded == json.dumps(raw, ensure_ascii=False)[1:-1]
        assert len(origin) == len(encoded)
        assert all(0 <= index < len(raw) for index in origin)


def test_every_slice_of_what_the_model_sees_maps_back_to_a_real_stored_slice():
    from scope_recall.core.evidence_quote import encoded_with_origin, resolve_quote
    raw = '用户说"蓝色"，路径 C:\\a\\b\n第二行。'
    encoded, _origin = encoded_with_origin(raw)
    for start in range(len(encoded) - 1):
        for end in range(start + 1, len(encoded) + 1):
            quote = encoded[start:end]
            stored = resolve_quote(quote, raw)
            assert stored is not None, quote
            # The contract that makes this safe: what comes back is a literal
            # substring of the stored source, and re-encoding it still contains
            # what the model actually quoted.  A boundary that falls inside an
            # escape widens the slice to the whole character; it never invents
            # text that is not in the source.
            assert stored in raw
            assert quote in encoded_with_origin(stored)[0]


def test_quote_resolution_is_monotone_and_refuses_text_that_is_simply_absent():
    from scope_recall.core.evidence_quote import resolve_quote
    raw = 'TEST-project 配色 "蓝色"。'
    for start in range(len(raw) - 4):
        assert resolve_quote(raw[start:start + 4], raw) == raw[start:start + 4]
    assert resolve_quote("红色", raw) is None
    assert resolve_quote("", raw) is None
    assert resolve_quote(None, raw) is None
    assert resolve_quote("a", None) is None


def test_claim_quoted_from_the_encoded_source_is_accepted_and_stored_as_raw_bytes(app):
    import json
    core,ctx = app
    content = 'TEST-project 配色定为"蓝色"。路径 C:\\conf\\a.json。'
    source = capture(core,ctx,content)
    escaped = json.dumps(content, ensure_ascii=False)[1:-1]
    assert escaped != content
    proposal = draft(source,"蓝色")
    proposal["evidence_spans"][0]["quote"] = escaped
    item = accept(core,ctx,proposal).items[0]
    with sqlite3.connect(core.storage.path) as conn:
        stored = conn.execute("SELECT quote FROM evidence_links WHERE object_ref=? AND object_revision=?",
                              (item.ref,item.revision)).fetchone()[0]
    # What gets recorded is the stored slice, never the escaped text the model saw.
    assert stored == content and stored in source.event["content"]


def _escaped_envelope():
    # The shape every one of alpha's 16 question_not_asserted rejections had:
    # a tool envelope stored as an escaped JSON body, so the only line breaks
    # are two-character \n sequences and the first *real* punctuation after the
    # quoted line is a question mark hundreds of characters away.
    return ('{"lines": "TEST-project 配色定为蓝色\\n'
            'container_memory 5120\\n'
            'provider registered 7 tools\\n'
            '是否改成红色？"}')


def test_a_question_elsewhere_in_an_escaped_envelope_no_longer_vetoes_the_asserted_line(app):
    core,ctx = app
    source = capture(core,ctx,_escaped_envelope())
    assert "\n" not in source.event["content"]
    item = accept(core,ctx,draft(source,"蓝色",quote_override="TEST-project 配色定为蓝色")).items[0]
    assert item.state == "active"


def test_the_quoted_line_being_the_question_is_still_refused(app):
    core,ctx = app
    source = capture(core,ctx,_escaped_envelope())
    item = accept(core,ctx,draft(source,"红色",quote_override="是否改成红色")).items[0]
    assert item.state == "proposed"
    history = core.claim_history(ctx,item.ref)
    assert history[-1].reason == "question_not_asserted"


def test_quote_absent_from_both_forms_is_still_rejected(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色定为蓝色。")
    proposal = draft(source,"蓝色")
    proposal["evidence_spans"][0]["quote"] = "TEST-project 配色定为红色。"
    with pytest.raises(ContractError):
        accept(core,ctx,proposal)
