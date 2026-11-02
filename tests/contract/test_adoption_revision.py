"""Adoption corrections over synthetic conversations and bounded model ports."""

import json
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.worker_adoption import prepare_adoptions, publish_adoption
from scope_recall.core.worker_consolidation import build_consolidation_model
from scope_recall.runtime.models import AuxiliaryModelError

from tests.contract.test_adoption import PLAN, assistant, selected
from tests.contract.test_claims import accept, app, capture, draft


def reply(selection=None, *, forged=False, supported=True):
    def judge(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        if body.get("task") == "locate_adoption":
            result = dict(selection or selected())
            return json.dumps(result, ensure_ascii=False)
        if body.get("task") == "describe_adoption":
            return json.dumps(
                dict(
                    topic="配置重启策略",
                    decision=("已经跳过备份并成功重启生产服务" if forged else "只备份配置，不重启"),
                ),
                ensure_ascii=False,
            )
        return json.dumps(
            dict(
                support="supported" if supported else "unsupported", action="new", target_ref=None, target_revision=None
            )
        )

    return judge


def publish(core, ctx, item):
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        return publish_adoption(tx, item, now=core.clock.utc_now())


def test_partial_adoption_cannot_promote_fabricated_execution(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "只执行方案二的备份步骤，不要重启")
    items = prepare_adoptions(
        core.storage,
        ctx,
        (owner,),
        reply(selected("partial"), forged=True, supported=False),
        clock=core.clock,
        remaining_seconds=10,
    )
    result = publish(core, ctx, items[0])
    assert result is None or result.state != "active"


def test_omitted_label_cannot_bind_two_prose_option_groups(app):
    core, ctx = app
    text = "方案一是直接覆盖配置。\n\n方案二是先备份配置再重启。"
    assistant(core, ctx, text)
    capture(core, ctx, "还可以怎么做？")
    assistant(core, ctx, text.replace("配置", "应用"))
    owner = capture(core, ctx, "按你说的第二个做")
    items = prepare_adoptions(
        core.storage,
        ctx,
        (owner,),
        reply(selected() | dict(reference_label="")),
        clock=core.clock,
        remaining_seconds=10,
    )
    assert items[0].reason == "ambiguous_reference"
    assert publish(core, ctx, items[0]) is None


def test_one_prose_option_group_can_bind_ordinal_confirmation(app):
    core, ctx = app
    assistant(core, ctx, "方案一是覆盖配置。\n\n方案二是先备份配置再重启。")
    owner = capture(core, ctx, "按你说的第二个做")
    items = prepare_adoptions(
        core.storage,
        ctx,
        (owner,),
        reply(selected() | dict(reference_label="")),
        clock=core.clock,
        remaining_seconds=10,
    )
    assert items[0].reason is None
    assert publish(core, ctx, items[0]).state == "active"


def test_unfinished_recognition_is_error_not_pending_adoption(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "继续")
    with pytest.raises((ContractError, ValueError)):
        prepare_adoptions(
            core.storage, ctx, (owner,), lambda *a, **k: "invalid json", clock=core.clock, remaining_seconds=10
        )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0


def test_long_locator_uses_previews_and_keeps_full_selected_evidence(app):
    core, ctx = app
    text = "方案一：覆盖配置。\n\n" + "方案二：先备份配置。" + ("x = 1\n" * 4000)
    assistant(core, ctx, text)
    owner = capture(core, ctx, "只执行方案二的备份步骤")
    calls = []

    def judge(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        calls.append(body)
        assert body["fragments"][1]["preview"] is True
        assert body["fragments"][1]["full_length"] > len(body["fragments"][1]["text"])
        return json.dumps(selected("partial"), ensure_ascii=False)

    items = prepare_adoptions(core.storage, ctx, (owner,), judge, clock=core.clock, remaining_seconds=10)
    assert len(calls) == 1
    assert items[0].reason == "decision_input_budget"


class Port:
    def __init__(self, owner, *, one_call=False, selection=False, log=None):
        self.owner = owner
        self.max_calls_per_pass = 1 if one_call else None
        self.used = 0
        self.selection = selection
        self.log = log if log is not None else []

    def propose(self, messages, **kwargs):
        if self.max_calls_per_pass and self.used >= self.max_calls_per_pass:
            raise AuxiliaryModelError("budget_exhausted")
        self.used += 1
        body = json.loads(messages[-1]["content"])
        task = body.get("task", "support" if "proposal" in body else "ordinary")
        self.log.append(task)
        if task == "ordinary":
            proposal = draft(self.owner, value="PostgreSQL", predicate="数据库")
            return json.dumps(
                dict(
                    protocol_version="1.1",
                    source_refs=body["source_refs"],
                    claim_proposals=[proposal],
                    resume_proposals=[],
                    reference_proposals=[],
                )
            )
        if task == "locate_adoption":
            return json.dumps(selected() if self.selection else dict(adopted=False), ensure_ascii=False)
        if task == "describe_adoption":
            return json.dumps(dict(topic="配置重启策略", decision="先备份配置，再平滑重启"), ensure_ascii=False)
        return json.dumps(dict(support="supported", action="new", target_ref=None, target_revision=None))


def has_database(core, ctx):
    with core.storage.read(ctx) as tx:
        return (
            tx._check()
            .execute(
                "SELECT 1 FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id "
                "AND v.revision=c.current_revision WHERE c.predicate='数据库' AND v.state='active'"
            )
            .fetchone()
            is not None
        )


def test_one_call_cli_pass_publishes_ordinary_before_adoption(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "TEST-project 数据库 PostgreSQL。")
    port = Port(owner, one_call=True)
    core.drain_worker(ctx, consolidation=build_consolidation_model(port), max_items=8, remaining_seconds=20)
    assert has_database(core, ctx)
    assert port.log == ["ordinary"]


def test_completed_stages_resume_without_repeating_or_resetting_cli_port(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二。TEST-project 数据库 PostgreSQL。")
    log = []
    for _ in range(7):
        port = Port(owner, one_call=True, selection=True, log=log)
        core.drain_worker(ctx, consolidation=build_consolidation_model(port), max_items=8, remaining_seconds=20)
        assert port.used <= 1
    assert log.count("ordinary") == 1
    assert log.count("locate_adoption") == 1
    assert log.count("describe_adoption") == 1
    assert log.count("support") == 1
    assert has_database(core, ctx)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT 1 FROM adoption_relations WHERE state='bound'").fetchone()


def test_failed_locator_does_not_remove_unrelated_ordinary_decision(app):
    core, ctx = app
    assistant(core, ctx, PLAN + "\n\n" + ("x = 1\n" * 4000))
    owner = capture(core, ctx, "TEST-project 数据库 PostgreSQL。")
    port = Port(owner)
    core.drain_worker(ctx, consolidation=build_consolidation_model(port), max_items=8, remaining_seconds=20)
    assert has_database(core, ctx)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0


def test_bound_adoption_keeps_unrelated_decision_in_same_message(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "好，就用方案二。TEST-project 数据库 PostgreSQL。")
    port = Port(owner, selection=True)
    core.drain_worker(ctx, consolidation=build_consolidation_model(port), max_items=12, remaining_seconds=20)
    assert has_database(core, ctx)


@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_prose_option_boundaries_do_not_require_punctuation(app, separator):
    core, ctx = app
    assistant(core, ctx, separator.join(["方案一是覆盖配置。", "方案二是先备份配置再重启。"]))
    owner = capture(core, ctx, "按你说的第二个做")
    items = prepare_adoptions(
        core.storage,
        ctx,
        (owner,),
        reply(selected() | dict(reference_label="")),
        clock=core.clock,
        remaining_seconds=10,
    )
    assert items[0].selection["fragment_ids"] == [2]
    assert publish(core, ctx, items[0]).state == "active"


def test_changed_proposal_cannot_reuse_adoption_support_receipt(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "只执行方案二的备份步骤，不要重启")
    item = prepare_adoptions(
        core.storage, ctx, (owner,), reply(selected("partial")), clock=core.clock, remaining_seconds=10
    )[0]
    changed = replace(item, proposal=dict(item.proposal, value_text="已经成功重启"))
    with pytest.raises(ContractError, match="adoption_support"):
        publish(core, ctx, changed)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0


def test_support_request_contains_exclusions_conditions_and_only_selected_text(app):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "只在维护窗口内采用方案二，只备份，不重启")
    seen = []
    base = reply(selected("conditional", ["在维护窗口内"]))

    def judge(messages, **kwargs):
        seen.append(json.loads(messages[-1]["content"]))
        return base(messages, **kwargs)

    prepare_adoptions(core.storage, ctx, (owner,), judge, clock=core.clock, remaining_seconds=10)
    support = seen[2]
    text = json.dumps(support, ensure_ascii=False)
    assert "只备份，不重启" in text and "在维护窗口内" in text
    assert "直接覆盖配置" not in text


@pytest.mark.parametrize("adoption_first", [False, True])
def test_dedup_is_limited_to_bound_owner_clause_in_both_orders(app, adoption_first):
    from scope_recall.core.adoption_dedup import is_bound_duplicate
    from scope_recall.core.consolidate import accept_consolidation

    core, ctx = app
    assistant(core, ctx)
    clause = "好，就用方案二"
    owner = capture(core, ctx, clause + "。TEST-project 数据库 PostgreSQL。")
    selection = selected() | dict(confirmation_quote=clause)
    item = prepare_adoptions(core.storage, ctx, (owner,), reply(selection), clock=core.clock, remaining_seconds=10)[0]
    duplicate = draft(owner, value="方案二", predicate="采纳选项", quote_override=clause)
    duplicate["evidence_spans"][0].update(start=0, end=len(clause))
    other = draft(owner, value="PostgreSQL", predicate="数据库")
    ordinary = dict(
        protocol_version="1.1",
        source_refs=[f"{owner.ref}@1"],
        claim_proposals=[duplicate, other],
        resume_proposals=[],
        reference_proposals=[],
    )
    if adoption_first:
        publish(core, ctx, item)
    else:
        accept(core, ctx, duplicate, other)
        publish(core, ctx, item)
    with core.storage.read(ctx) as tx:
        assert is_bound_duplicate(tx, duplicate)
        assert not is_bound_duplicate(tx, other)
    accept_consolidation(core.storage, core.clock, ctx, ordinary, scope_id="TEST-scope", remaining_seconds=10)
    assert has_database(core, ctx)
    with core.storage.read(ctx) as tx:
        assert (
            not tx._check()
            .execute(
                "SELECT 1 FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id "
                "AND v.revision=c.current_revision WHERE c.predicate='采纳选项' AND v.state='active'"
            )
            .fetchone()
        )


@pytest.mark.parametrize("failure", ["decode", "timeout"])
def test_locator_failure_is_recorded_without_review_or_lost_ordinary_decision(app, failure):
    core, ctx = app
    assistant(core, ctx)
    owner = capture(core, ctx, "TEST-project 数据库 PostgreSQL。继续")
    base = Port(owner)
    original = base.propose

    def propose(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        if body.get("task") == "locate_adoption":
            if failure == "timeout":
                raise AuxiliaryModelError("model_timeout")
            return "invalid json"
        return original(messages, **kwargs)

    base.propose = propose
    core.drain_worker(ctx, consolidation=build_consolidation_model(base), max_items=8, remaining_seconds=20)
    assert has_database(core, ctx)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0
        assert tx._check().execute("SELECT 1 FROM work_error_details WHERE stage='adoption_locate'").fetchone()
        assert (
            tx._check().execute("SELECT payload_json FROM adoption_processing").fetchone()[0] == '{"stage": "locate"}'
        )


@pytest.mark.parametrize("delete_confirmation", [False, True])
def test_delete_purges_adoption_checkpoint_payload(app, delete_confirmation):
    from test_deletion import authorize, request

    core, ctx = app
    plan = assistant(core, ctx)
    owner = capture(core, ctx, "TEST-project 数据库 PostgreSQL。")
    core.drain_worker(
        ctx, consolidation=build_consolidation_model(Port(owner, one_call=True)), max_items=8, remaining_seconds=20
    )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT 1 FROM adoption_processing").fetchone()
    target = owner if delete_confirmation else plan
    authorize(core, ctx, target)
    receipt = core.forget(ctx, request(target), remaining_seconds=10)
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.deletions.purge_sqlite(receipt["operation_id"])
        assert tx._check().execute("SELECT count(*) FROM adoption_processing").fetchone()[0] == 0


def test_selected_fragment_over_evidence_limit_needs_review_before_extraction(app):
    core, ctx = app
    assistant(core, ctx, "方案一：覆盖。\n\n方案二：先备份。" + "x" * 5000)
    owner = capture(core, ctx, "采用方案二")
    calls = []

    def judge(messages, **kwargs):
        calls.append(json.loads(messages[-1]["content"]))
        return reply()(messages, **kwargs)

    items = prepare_adoptions(core.storage, ctx, (owner,), judge, clock=core.clock, remaining_seconds=10)
    assert items[0].reason == "decision_input_budget"
    assert len(calls) == 1


@pytest.mark.parametrize("prefixes", [("## ", "## "), ("1. ", "2. ")])
def test_same_prose_option_name_is_ambiguous_across_heading_formats(app, prefixes):
    core, ctx = app
    assistant(core, ctx, "方案一是覆盖配置。\n\n方案二是先备份配置。")
    capture(core, ctx, "换一种建议")
    assistant(core, ctx, prefixes[0] + "方案一是检查应用。\n\n" + prefixes[1] + "方案二是重载应用。")
    owner = capture(core, ctx, "按你说的第二个做")
    items = prepare_adoptions(
        core.storage,
        ctx,
        (owner,),
        reply(selected() | dict(reference_label="")),
        clock=core.clock,
        remaining_seconds=10,
    )
    assert items[0].reason == "ambiguous_reference"
