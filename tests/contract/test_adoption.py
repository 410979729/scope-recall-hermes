"""Explicit adoption is a source-bound decision, never assistant testimony."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.adoption import adoption_messages, decode_selection, fragments
from scope_recall.core.adoption_storage import adoption_context, binding_reason
from scope_recall.core.consolidate import consolidation_messages
from scope_recall.core.worker_adoption import prepare_adoptions, publish_adoption
from v11_support import recall_request, source_event

from tests.contract.test_claims import accept, app, capture, draft

PLAN = "方案一：直接覆盖配置。\n\n方案二：先备份配置，再平滑重启，失败时恢复备份。"


def assistant(core, ctx, text=PLAN, **kwargs):
    key = kwargs.pop("key", f"TEST-plan/{next(core.test_sequence)}")
    revision = kwargs.pop("revision", 1)
    event = source_event(
        source_event_key=key,
        source_revision=revision,
        origin="assistant_visible",
        role="assistant",
        content=text,
        **kwargs,
    )
    saved = core.record_event(
        replace(ctx, actor_origin="assistant_visible"), event, scope_id="TEST-scope", remaining_seconds=10
    )
    return core.source(ctx, saved.event_refs[0].ref, revision)


def selected(scope="whole", conditions=None):
    return dict(adopted=True, fragment_ids=[2], scope=scope, conditions=conditions or [], reference_label="方案二")


def model(selection=None):
    calls = []

    def judge(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        calls.append(body)
        if "proposal" in body:
            return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))
        if body["task"] == "locate_adoption":
            return json.dumps(selection if selection is not None else selected(), ensure_ascii=False)
        assert "直接覆盖" not in json.dumps(body, ensure_ascii=False)
        decision = (
            "只先备份配置，不进行重启。" if body["scope"] == "partial" else "先备份配置，再平滑重启，失败时恢复备份。"
        )
        topic = "版面布局" if "排版" in body["adopted_fragments"][0]["text"] else "配置重启策略"
        if topic == "版面布局":
            decision = "采用双栏排版。"
        return json.dumps(dict(decision=decision, topic=topic), ensure_ascii=False)

    return judge, calls


def prepared(core, ctx, confirmation, selection=None):
    judge, calls = model(selection)
    return prepare_adoptions(core.storage, ctx, (confirmation,), judge, clock=core.clock, remaining_seconds=10), calls


def publish(core, ctx, adoption):
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        return publish_adoption(tx, adoption, now=core.clock.utc_now())


def test_paused_entry_rejects_direct_adoption_publication(app):
    from scope_recall.contracts import ContractError
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    assistant(core, ctx)
    confirmation = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, confirmation)
    MemoryManagement(core, InstallationMaintenance(ctx)).set_entry_state("local", "paused", actor="TEST-owner")
    with pytest.raises(ContractError, match="entry_paused"):
        publish(core, ctx, rows[0])


def test_fragments_are_exact_disjoint_source_slices():
    text = "## Options\n\n1. First\n   continuation\n2. Second\n\nTail"
    parts = fragments(text)
    assert [p["id"] for p in parts] == list(range(1, len(parts) + 1))
    assert all(text[p["start"] : p["end"]] == p["text"] for p in parts)
    assert all(a["end"] <= b["start"] for a, b in zip(parts, parts[1:], strict=False))


def test_s4_stores_owner_root_and_selected_assistant_fragment(app):
    core, ctx = app
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    rows, calls = prepared(core, ctx, owner)
    assert len(rows) == 1 and rows[0].reason is None
    result = publish(core, ctx, rows[0])
    stored = core.current_claim(ctx, result.ref)
    assert stored.state == "active" and stored.payload["kind"] == "decision"
    assert "Owner adopted assistant proposal" in stored.payload["value_text"]
    assert stored.reason == "owner_adopted_assistant_proposal"
    assert {s["source_ref"] for s in stored.payload["evidence_spans"]} == {owner.ref, plan.ref}
    assert len(calls) == 3
    assert calls[1]["adopted_fragments"][0]["text"] == fragments(PLAN)[1]["text"]


@pytest.mark.parametrize(
    "scope,text,conditions",
    [
        ("partial", "只采纳方案二的备份步骤，重启不做", []),
        ("conditional", "只在维护窗口内才用方案二", ["在维护窗口内"]),
    ],
)
def test_partial_and_conditional_adoption_preserve_boundaries(app, scope, text, conditions):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, text)
    rows, _ = prepared(core, ctx, owner, selected(scope, conditions))
    item = publish(core, ctx, rows[0])
    saved = core.current_claim(ctx, item.ref)
    assert saved.payload["conditions"] == conditions
    assert scope in saved.payload["value_text"]


@pytest.mark.parametrize("text", ["好的", "继续", "方案二不行", "知道了", "先别做"])
def test_no_adoption_keeps_existing_consolidation_input(app, text):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, text)
    before = consolidation_messages((owner,))
    rows, calls = prepared(core, ctx, owner, dict(adopted=False))
    assert rows == () and len(calls) == 1
    assert consolidation_messages((owner,)) == before
    assert PLAN not in json.dumps(before, ensure_ascii=False)


def test_two_same_named_option_groups_wait_for_confirmation(app):
    core, ctx = app
    assistant(core, ctx)
    capture(core, ctx, "还有别的备选吗？")
    assistant(core, ctx, "方案一：热更新。\n\n方案二：蓝绿切换。")
    owner = capture(core, ctx, "好，就用方案二")
    rows, calls = prepared(core, ctx, owner)
    assert rows[0].reason == "ambiguous_reference" and len(calls) == 1
    assert publish(core, ctx, rows[0]) is None


def test_source_edited_after_confirmation_cannot_bind(app):
    core, ctx = app
    plan = assistant(core, ctx, key="TEST-plan")
    owner = capture(core, ctx, "好，就用方案二")
    assistant(core, ctx, "方案一：覆盖。\n\n方案二：不备份直接重启。", key="TEST-plan", revision=2)
    with core.storage.read(ctx) as tx:
        snapshot = adoption_context(tx, owner)
        assert snapshot.assistant.ref == plan.ref
        assert (
            binding_reason(tx, snapshot, decode_selection(json.dumps(selected()), snapshot.fragments))
            == "source_changed"
        )


def test_deleted_source_is_not_sent_to_the_model(app):
    core, ctx = app
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (plan.ref,))
    from scope_recall.contracts import ContractError

    with pytest.raises(ContractError, match="SOURCE_MISSING"):
        prepared(core, ctx, owner)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0


def test_cross_session_and_intervening_user_messages_are_not_adoption_context(app):
    core, ctx = app
    assistant(core, ctx)
    other = capture(core, replace(ctx, session_id="TEST-other"), "好，就用方案二")
    rows, calls = prepared(core, ctx, other)
    assert rows == () and not calls
    capture(core, ctx, "先查一下日志")
    owner = capture(core, ctx, "好，就用方案二")
    rows, calls = prepared(core, ctx, owner)
    assert rows == () and not calls


@pytest.mark.parametrize(
    "change", [dict(fragment_ids=[99]), dict(conditions=["不存在的条件"]), dict(reference_label="不存在的方案")]
)
def test_model_cannot_invent_positions_conditions_or_labels(app, change):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    if "fragment_ids" in change or "conditions" in change:
        from scope_recall.contracts import ContractError

        with pytest.raises(ContractError):
            prepared(core, ctx, owner, selected() | change)
        return
    rows, _ = prepared(core, ctx, owner, (selected() | change))
    assert rows[0].reason is not None
    assert publish(core, ctx, rows[0]) is None


def test_assistant_alone_and_forged_decisions_gain_no_authority(app):
    core, ctx = app
    plan = assistant(core, ctx)
    result = accept(core, ctx, draft(plan, value="先备份配置"))
    assert result.items[0].state == "proposed"


def test_source_changes_during_model_call_are_rechecked_at_publication(app):
    core, ctx = app
    assistant(core, ctx, key="TEST-plan")
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    assistant(core, ctx, "方案二：跳过备份。", key="TEST-plan", revision=2)
    assert publish(core, ctx, rows[0]) is None


def test_worker_publishes_adoption_and_cross_session_packet_carries_its_content(app):
    core, ctx = app
    assistant(core, ctx)
    capture(core, ctx, "好，就用方案二")
    judge, calls = model()

    class Model:
        def judge(self, messages, **kwargs):
            return judge(messages, **kwargs)

        def propose(self, sources, **kwargs):
            assert all(s.event["origin"] == "human_direct" for s in sources)
            return json.dumps(
                dict(
                    protocol_version="1.1",
                    source_refs=[f"{s.ref}@{s.revision}" for s in sources],
                    claim_proposals=[],
                    resume_proposals=[],
                    reference_proposals=[],
                )
            )

    result = core.drain_worker(ctx, consolidation=Model(), max_items=8, remaining_seconds=20)
    assert result.failed == 0 and result.completed > 0
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT claim_ref FROM adoption_relations WHERE state='bound'").fetchone()
        assert row is not None
    from scope_recall.core.retrieval import CandidateRef

    class SyntheticVectors:
        def search(self, context, **kwargs):
            return (
                CandidateRef(
                    "claim",
                    row["claim_ref"],
                    1,
                    "vector",
                    vector_score=0.99,
                    vector_id="TEST-adoption-vector",
                    embedding_space=core.recall_pipeline.policy.embedding_space_id,
                ),
            )

    # A deterministic semantic hit exercises admission, live hydration and
    # packet rendering without calling an embedding model or touching vectors.
    core.recall_pipeline.vector_port = SyntheticVectors()
    packet = core.recall_packet(
        replace(ctx, session_id="TEST-next"), recall_request(query="按上次定的方案来"), deadline_seconds=5
    )
    assert any(item["ref"] == row["claim_ref"] and "先备份配置" in item["content"] for item in packet["items"]), packet
    assert "直接覆盖" not in json.dumps(packet["items"], ensure_ascii=False)


def test_pending_adoption_uses_existing_owner_review_page(app):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    assistant(core, ctx)
    capture(core, ctx, "好，就用方案二")
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    publish(core, ctx, rows[0])
    authority = InstallationMaintenance(ctx)
    page = MemoryManagement(core, authority).quarantine()
    assert any(
        item["ref"] == owner.ref and item["category"] == "adoption" and item["rules"] == ["ambiguous_reference"]
        for item in page["items"]
    )


@pytest.mark.parametrize("delete_confirmation", [False, True])
def test_deleting_either_source_removes_decision_and_relation(app, delete_confirmation):
    from test_deletion import authorize, request

    core, ctx = app
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    item = publish(core, ctx, rows[0])
    target = owner if delete_confirmation else plan
    authorize(core, ctx, target)
    receipt = core.forget(ctx, request(target), remaining_seconds=10)
    assert core.current_claim(ctx, item.ref) is None
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.deletions.purge_sqlite(receipt["operation_id"])
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0


def test_editing_bound_assistant_exposes_pending_review_and_hides_decision(app):
    core, ctx = app
    assistant(core, ctx, key="TEST-plan")
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    publish(core, ctx, rows[0])
    assistant(core, ctx, "方案二：跳过备份。", key="TEST-plan", revision=2)
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT state,reason FROM adoption_relations").fetchone()
        assert tuple(row) == ("pending_review", "source_changed")
    packet = core.recall_packet(
        replace(ctx, session_id="TEST-next"), recall_request(query="配置备份方案"), deadline_seconds=5
    )
    assert not any("Owner adopted assistant proposal" in item["content"] for item in packet["items"])


def test_missing_model_label_cannot_bypass_duplicate_option_binding(app):
    core, ctx = app
    assistant(core, ctx)
    capture(core, ctx, "再给一组方案")
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner, selected() | dict(reference_label=""))
    assert rows[0].reason == "ambiguous_reference"
    assert publish(core, ctx, rows[0]) is None


def test_partial_adoption_can_preserve_literal_owner_restrictions(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "只执行方案二的备份那一步")
    rows, _ = prepared(core, ctx, owner, selected("partial", ["备份那一步"]))
    saved = publish(core, ctx, rows[0])
    assert core.current_claim(ctx, saved.ref).payload["conditions"] == ["备份那一步"]


def test_unrelated_adopted_plans_do_not_replace_each_other(app):
    core, ctx = app
    assistant(core, ctx)
    first = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, first)
    old = publish(core, ctx, rows[0])
    other = replace(ctx, session_id="TEST-document-session")
    assistant(core, other, "选项A：单栏。\n\n选项B：双栏排版。")
    owner = capture(core, other, "采用选项B来排版")
    judge, _ = model(selected() | dict(reference_label="选项B"))
    rows = prepare_adoptions(core.storage, other, (owner,), judge, clock=core.clock, remaining_seconds=10)
    new = publish(core, other, rows[0])
    assert old.ref != new.ref
    assert core.current_claim(ctx, old.ref).state == "active"


def test_owner_can_correct_adopted_decision_through_existing_capture_path(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    old = publish(core, ctx, rows[0])
    capture(core, ctx, "更正：TEST-project 配置重启策略 改成先验证备份再重启。", when="2026-09-06T12:00:00Z")
    current = core.current_claim(ctx, old.ref)
    assert current.revision > old.revision
    assert current.payload["value_text"] == "先验证备份再重启"
    assert len(core.claim_history(ctx, old.ref)) == 2


def test_adoption_does_not_discard_other_owner_facts_in_the_same_message(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "TEST-project 配色 蓝色。好，就用方案二")
    judge, _ = model()

    class Model:
        def judge(self, messages, **kwargs):
            return judge(messages, **kwargs)

        def propose(self, sources, **kwargs):
            return json.dumps(
                dict(
                    protocol_version="1.1",
                    source_refs=[f"{s.ref}@{s.revision}" for s in sources],
                    claim_proposals=[draft(owner, value="蓝色", kind="preference", statement_kind="assertion")],
                    resume_proposals=[],
                    reference_proposals=[],
                )
            )

    result = core.drain_worker(ctx, consolidation=Model(), max_items=8, remaining_seconds=20)
    assert result.failed == 0
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT count(*) FROM claims WHERE kind='preference'").fetchone()
        assert row[0] == 1


def test_recommending_an_option_again_is_not_a_second_option_group(app):
    core, ctx = app
    assistant(core, ctx, PLAN + "\n\n建议优先考虑方案二。")
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    assert rows[0].reason is None
    assert publish(core, ctx, rows[0]).state == "active"


def test_option_titles_on_consecutive_lines_have_separate_fragments(app):
    core, ctx = app
    assistant(core, ctx, PLAN.replace("\n\n", "\n"))
    owner = capture(core, ctx, "好，就用方案二")
    rows, _ = prepared(core, ctx, owner)
    assert rows[0].reason is None
    assert publish(core, ctx, rows[0]).state == "active"


def test_adoption_checkpoint_cannot_publish_after_pause_and_resume(app):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance
    from scope_recall.core.worker import build_consolidation_model

    from tests.contract.test_adoption_revision import Port

    core, ctx = app
    assistant(core, ctx)
    confirmation = capture(core, ctx, "好，就用方案二。TEST-project 数据库 PostgreSQL。")
    for _ in range(10):
        core.drain_worker(
            ctx,
            consolidation=build_consolidation_model(Port(confirmation, one_call=True, selection=True)),
            max_items=1,
            remaining_seconds=20,
        )
        with core.storage.read(ctx) as tx:
            row = (
                tx._check()
                .execute("SELECT payload_json FROM adoption_processing WHERE confirmation_ref=?", (confirmation.ref,))
                .fetchone()
            )
            if row and json.loads(row[0])["stage"] == "publish":
                before = tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0]
                break
    else:
        pytest.fail("publish checkpoint not reached")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.set_entry_state("local", "paused", actor="TEST-owner")
    owner.set_entry_state("local", "normal", actor="TEST-owner")
    core.drain_worker(
        ctx,
        consolidation=build_consolidation_model(Port(confirmation, selection=True)),
        max_items=1,
        remaining_seconds=20,
    )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == before

    for _ in range(8):
        core.drain_worker(
            ctx,
            consolidation=build_consolidation_model(Port(confirmation, one_call=True, selection=True)),
            max_items=1,
            remaining_seconds=20,
        )
        with core.storage.read(ctx) as tx:
            if tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] > before:
                break
    else:
        pytest.fail("adoption was not prepared again after the entry resumed")
