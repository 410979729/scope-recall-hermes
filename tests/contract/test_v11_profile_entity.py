"""Focused Core profile/entity read-view contracts over synthetic TEST claims."""
from __future__ import annotations

from dataclasses import replace
import itertools
import os
from pathlib import Path

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.read_views import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_ITEMS
import scope_recall.core.read_views as read_views
from tests.contract.test_v11_aliases import _alias, _identity
from tests.contract.test_v11_claims import Clock, accept, capture, draft, initial, intention
from tests.v11_support import context


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-profile-entity"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _assert_candidate_origin() -> None:
    imported = Path(read_views.__file__).resolve()
    assert imported.is_relative_to(ROOT)
    isolated = Path(os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"]).resolve()
    assert Path(os.environ["HERMES_HOME"]).resolve().is_relative_to(isolated)


def _accept(core, ctx, text, **changes):
    source = capture(core, ctx, text)
    return accept(core, ctx, draft(source, **changes)).items[0], source


def test_candidate_import_stays_on_this_checkout():
    _assert_candidate_origin()


def test_profile_groups_admitted_current_claims_and_entity_one_hop(app):
    _assert_candidate_origin()
    core, ctx = app
    fact, _ = _accept(core, ctx, "TEST-project 配色 蓝色。", value="蓝色", kind="fact", predicate="配色")
    pref, _ = _accept(core, ctx, "TEST-project 偏好 简洁。", value="简洁", kind="preference", predicate="偏好", statement_kind="assertion")
    constraint, _ = _accept(
        core, ctx, "TEST-project 未授权不改网站，只限写文案任务。",
        value="不改网站", kind="constraint", predicate="网站修改", conditions=["未授权", "写文案任务"],
    )
    decision, _ = _accept(core, ctx, "TEST-project 发布窗口 周二。", value="周二", kind="decision", predicate="发布窗口")
    intent_source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    pending = accept(core, ctx, intention(intent_source)).items[0]
    incoming, _ = _accept(
        core, ctx, "TEST-alice 负责 TEST-project。",
        value="TEST-project", kind="fact", predicate="负责", subject="TEST-alice",
    )
    conditional, _ = _accept(
        core, ctx, "未授权时 TEST-bob 依赖 TEST-project。",
        value="TEST-project", kind="fact", predicate="依赖", subject="TEST-bob",
        conditions=["未授权"],
    )
    cooccur, _ = _accept(
        core, ctx, "TEST-alice 合作伙伴是 Bob, Carol。",
        value="Bob, Carol", kind="fact", predicate="合作伙伴", subject="TEST-alice",
    )

    profile = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-profile", "subject": "TEST-project",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert profile["status"] == "ok"
    assert profile["alias_resolution"] == "literal"
    assert profile["resolved_subject"] == "TEST-project"
    assert [item["value_text"] for item in profile["sections"]["facts"]] == ["蓝色"]
    assert [item["value_text"] for item in profile["sections"]["preferences"]] == ["简洁"]
    assert [item["value_text"] for item in profile["sections"]["constraints"]] == ["不改网站"]
    assert [item["value_text"] for item in profile["sections"]["decisions"]] == ["周二"]
    assert [item["ref"] for item in profile["sections"]["pending_intentions"]] == [pending.ref]
    assert profile["disputed"] == []
    fact_item = profile["sections"]["facts"][0]
    assert fact_item["ref"] == fact.ref
    assert fact_item["revision"] == 1
    assert fact_item["temporal_status"] == "current"
    assert fact_item["evidence_refs"]
    assert fact_item["conditions"] == []
    assert constraint.ref in {item["ref"] for item in profile["sections"]["constraints"]}

    probe = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-probe", "subject": "TEST-project",
        "action": "probe", "direction": "outgoing", "max_items": 16, "budget_tokens": 4096,
    })
    assert [item["value_text"] for item in probe["statements"]] == ["蓝色"]
    assert all(item["direction"] == "outgoing" for item in probe["statements"])

    related = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-related", "subject": "TEST-project",
        "action": "related", "direction": "both", "max_items": 16, "budget_tokens": 4096,
    })
    outgoing_values = {item["value_text"] for item in related["statements"] if item["direction"] == "outgoing"}
    incoming_refs = {item["ref"] for item in related["statements"] if item["direction"] == "incoming"}
    assert "蓝色" in outgoing_values
    assert incoming.ref in incoming_refs
    assert conditional.ref in incoming_refs
    assert incoming_refs == {incoming.ref, conditional.ref}
    conditional_item = next(item for item in related["statements"] if item["ref"] == conditional.ref)
    assert conditional_item["conditions"] == ["未授权"]
    assert conditional_item["valid_from"]
    assert conditional_item["valid_to"] is None
    assert conditional_item["claim_state"] == "active"
    assert conditional_item["basis"] == "direct_report"
    bob = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-bob", "subject": "Bob",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert bob["status"] == "no_match"
    assert bob["statements"] == []
    assert bob["coverage"] != "complete_for_query"
    alice = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-alice", "subject": "TEST-alice",
        "action": "related", "direction": "outgoing", "predicate": "合作伙伴",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert alice["statements"] == [
        item for item in alice["statements"] if item["value_text"] == "Bob, Carol" and item["ref"] == cooccur.ref
    ]
    assert pref.ref not in {item["ref"] for item in probe["statements"]}


def test_correction_suppress_scope_and_empty_chat_are_honest(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100", kind="fact", predicate="配色")
    capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")
    profile = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-correct", "subject": "TEST-project",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert [item["value_text"] for item in profile["sections"]["facts"]] == ["H200"]
    assert profile["sections"]["facts"][0]["revision"] == 2

    current = core.current_claim(ctx, item.ref)
    incoming, _ = _accept(
        core, ctx, "TEST-owner 拥有 TEST-project。",
        value="TEST-project", kind="fact", predicate="拥有", subject="TEST-owner",
    )
    capture(core, ctx, f"删除 {item.ref}。TEST-project 配色 H200。", when="2026-09-04T12:00:00Z")
    core.forget(ctx, {
        "protocol_version": "1.1", "target_refs": [item.ref], "mode": "delete",
        "expected_revisions": {item.ref: current.revision},
    }, remaining_seconds=10)
    after_delete = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-deleted", "subject": "TEST-project",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert item.ref not in {entry["ref"] for entry in after_delete["sections"]["facts"]}
    capture(core, ctx, f"删除 {incoming.ref}。TEST-owner 拥有 TEST-project。", when="2026-09-05T12:00:00Z")
    core.forget(ctx, {
        "protocol_version": "1.1", "target_refs": [incoming.ref], "mode": "delete",
        "expected_revisions": {incoming.ref: 1},
    }, remaining_seconds=10)
    reverse = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-reverse-gone", "subject": "TEST-project",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert incoming.ref not in {entry["ref"] for entry in reverse["statements"]}

    other = replace(ctx, project_id="TEST-other")
    branch = replace(ctx, branch_id="TEST-exp")
    for denied in (other, branch):
        hidden = core.profile(denied, {
            "protocol_version": "1.1", "request_id": "TEST-scope", "subject": "TEST-project",
            "max_items": 16, "budget_tokens": 4096,
        })
        assert hidden["status"] == "no_match"
        assert hidden["sections"]["facts"] == []

    raw_only = replace(context(ctx.binding.data_directory / "raw"), project_id="TEST-raw", branch_id="TEST-main")
    raw_core = MemoryCore(CoreConfig(raw_only.binding), clock=Clock())
    raw_core.initialize()
    raw_core.test_sequence = itertools.count(1)
    capture(raw_core, raw_only, "随便聊了 TEST-project 配色，但没有整理。")
    empty = raw_core.profile(raw_only, {
        "protocol_version": "1.1", "request_id": "TEST-empty", "subject": "TEST-project",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert empty["status"] == "no_match"
    assert empty["coverage"] == "unknown"
    assert "consolidation_required" in empty["gaps"]
    encoded = str(empty)
    assert "随便聊了" not in encoded


def test_alias_ambiguity_and_person_alias_stay_unmerged(app):
    core, ctx = app
    alpha_source = capture(core, ctx, "Alpha 项目名称是 名称A。")
    alpha = accept(core, ctx, draft(alpha_source, "名称A", kind="fact", predicate="项目名称", subject="Alpha")).items[0]
    beta_source = capture(core, ctx, "Beta 项目名称是 名称B。")
    beta = accept(core, ctx, draft(beta_source, "名称B", kind="fact", predicate="项目名称", subject="Beta")).items[0]
    rename_a = capture(core, ctx, "Alpha 项目以后改名为 共用别名，内容不变。")
    rename_b = capture(core, ctx, "Beta 项目以后改名为 共用别名，内容不变。")
    alias_a = accept(core, ctx, _alias(rename_a, alpha, "共用别名") | {"subject": "Alpha"}).items[0]
    alias_b = accept(core, ctx, _alias(rename_b, beta, "共用别名") | {"subject": "Beta"}).items[0]
    assert alias_a.state == "active" and alias_b.state == "active"

    ambiguous = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-amb", "subject": "共用别名",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert ambiguous["alias_resolution"] == "ambiguous"
    assert ambiguous["resolved_subject"] is None
    assert ambiguous["sections"]["facts"] == []
    assert ambiguous["answerability"] == "ambiguous"
    assert "alias_ambiguous" in ambiguous["gaps"]

    person = capture(core, ctx, "张三 喜欢茶。")
    person_claim = accept(core, ctx, draft(person, "茶", kind="preference", predicate="喜欢", subject="张三", statement_kind="assertion")).items[0]
    invented = capture(core, ctx, "张三 项目以后改名为 李四，内容不变。")
    person_alias = accept(core, ctx, _alias(invented, person_claim, "李四") | {"subject": "张三"}).items[0]
    assert person_alias.state == "proposed"
    unresolved = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-person", "subject": "李四",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert unresolved["alias_resolution"] == "literal"
    assert unresolved["status"] == "no_match"
    assert unresolved["resolved_subject"] == "李四"


def test_explicit_project_alias_resolves_and_provenance_is_stored(app):
    core, ctx = app
    mist = replace(ctx, project_id="TEST-mist")
    target = _identity(core, mist)
    source = capture(core, mist, "TEST-mist 项目以后改名为 TEST暮光，内容不变，保留旧称 TEST晨雾。")
    alias = accept(core, mist, _alias(source, target, "TEST暮光")).items[0]
    assert alias.state == "active"
    _accept(core, mist, "TEST-mist 配色 白色。", value="白色", kind="fact", predicate="配色", subject="TEST-mist")
    captured = capture(
        core, mist, "TEST-mist 发布窗口 周五。",
        source_context={"platform": "telegram", "chat_type": "private"},
    )
    accept(core, mist, draft(captured, "周五", kind="fact", predicate="发布窗口", subject="TEST-mist"))
    profile = core.profile(mist, {
        "protocol_version": "1.1", "request_id": "TEST-alias", "subject": "TEST暮光",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert profile["alias_resolution"] == "resolved"
    assert profile["resolved_subject"] == "TEST-mist"
    values = {item["value_text"] for item in profile["sections"]["facts"]}
    assert {"白色", "周五"} <= values
    contexts = [
        entry
        for item in profile["sections"]["facts"]
        for entry in item.get("source_contexts", [])
    ]
    assert {"platform": "telegram", "chat_type": "private"} in contexts
    for entry in contexts:
        assert entry["platform"] != "discord"


def test_proposed_disputed_completed_and_reads_do_not_write(app):
    core, ctx = app
    initial(core, ctx, value="蓝色", kind="fact", predicate="配色")
    hypothetical = capture(core, ctx, "假设TEST-project 字号 18。")
    proposed = accept(core, ctx, draft(hypothetical, "18", kind="fact", predicate="字号")).items[0]
    assert proposed.state == "proposed"
    a = capture(core, ctx, "TEST-project 验收 完成。", origin="external_document", when=None)
    accept(core, ctx, draft(a, "完成", kind="fact", predicate="验收"))
    b = capture(core, ctx, "TEST-project 验收 失败。", origin="external_document", when=None)
    accept(core, ctx, draft(b, "失败", kind="fact", predicate="验收"))
    done = capture(core, ctx, "TEST-project 散热检查已完成。")
    completed = accept(core, ctx, intention(done, "completed")).items[0]
    assert completed.state == "active"

    class Boom:
        def __getattr__(self, name):
            raise AssertionError(name)

    core.vectors = Boom()
    core.consolidation = Boom()
    writes = []
    original_write = core.storage.write

    def guarded(*args, **kwargs):
        writes.append(1)
        return original_write(*args, **kwargs)

    core.storage.write = guarded
    epoch = core.status(ctx).memory_epoch
    profile = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-states", "subject": "TEST-project",
        "max_items": 16, "budget_tokens": 4096,
    })
    entity = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-states-e", "subject": "TEST-project",
        "action": "probe", "direction": "outgoing", "max_items": 16, "budget_tokens": 4096,
    })
    assert writes == []
    assert core.status(ctx).memory_epoch == epoch
    fact_values = {item["value_text"] for item in profile["sections"]["facts"]}
    assert "18" not in fact_values
    assert proposed.ref not in {item["ref"] for item in profile["sections"]["facts"]}
    assert profile["disputed"]
    assert all(item["temporal_status"] == "disputed" for item in profile["disputed"])
    assert "disputed_facts_separated" in profile["gaps"]
    assert completed.ref not in {item["ref"] for item in profile["sections"]["pending_intentions"]}
    assert "18" not in {item["value_text"] for item in entity["statements"]}


def test_invalid_bounds_and_budget_honesty(app):
    core, ctx = app
    for index, value in enumerate(("红", "绿", "蓝", "白"), start=1):
        _accept(core, ctx, f"TEST-project 色板{index} {value}。", value=value, kind="fact", predicate=f"色板{index}")
    clipped = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-clip", "subject": "TEST-project",
        "max_items": 2, "budget_tokens": 4096,
    })
    assert clipped["truncated"] is True
    assert clipped["coverage"] == "partial"
    assert clipped["coverage"] != "complete_for_query"
    assert sum(len(clipped["sections"][name]) for name in clipped["sections"]) + len(clipped["disputed"]) == 2
    try:
        tight = core.profile(ctx, {
            "protocol_version": "1.1", "request_id": "TEST-budget", "subject": "TEST-project",
            "max_items": 16, "budget_tokens": 220,
        })
    except ContractError as exc:
        assert exc.code == "INPUT_INVALID" and exc.field == "budget_tokens"
    else:
        assert read_views._budget_bytes(tight) <= 220
        assert tight["coverage"] != "complete_for_query"
        assert tight["truncated"] or "budget_token_cap" in tight["gaps"]
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.profile(ctx, {
            "protocol_version": "1.1", "request_id": "TEST-bool", "subject": "TEST-project",
            "max_items": True, "budget_tokens": 4096,
        })
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.entity(ctx, {
            "protocol_version": "1.1", "request_id": "TEST-enum", "subject": "TEST-project",
            "action": "graph", "direction": "outgoing", "max_items": 16, "budget_tokens": 4096,
        })
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.profile(ctx, {
            "protocol_version": "1.0", "request_id": "TEST-proto", "subject": "TEST-project",
            "max_items": 16, "budget_tokens": 4096,
        })
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.profile(ctx, {
            "protocol_version": "1.1", "request_id": "TEST-scope", "subject": "TEST-project",
            "max_items": 16, "budget_tokens": 4096, "scope_id": "TEST-scope",
        })
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.entity(ctx, {
            "protocol_version": "1.1", "request_id": "TEST-path", "subject": "TEST-project",
            "action": "probe", "direction": "outgoing", "max_items": 16, "budget_tokens": 4096,
            "data_directory": "C:/secret",
        })
    omitted = core.profile(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-defaults", "subject": "TEST-project",
    })
    assert omitted["status"] in {"ok", "partial"}
    assert DEFAULT_MAX_ITEMS == 16 and DEFAULT_BUDGET_TOKENS == 4096


def _long_ids():
    subject = "测试主体名称加长中文项目" * 8
    request_id = "请求编号加长中文" * 8
    assert 1 <= len(subject) <= 240
    assert 1 <= len(request_id) <= 100
    return subject, request_id


def _assert_exact_envelope_fit(core, ctx, request, *, entity=False, just_under_must_reject=False):
    reader = core.entity if entity else core.profile
    fitted = reader(ctx, request)
    size = read_views._budget_bytes(fitted)
    assert size <= request["budget_tokens"]
    same = reader(ctx, {**request, "budget_tokens": size})
    assert read_views._budget_bytes(same) <= size
    just_under = {**request, "budget_tokens": size - 1}
    if just_under_must_reject:
        with pytest.raises(ContractError) as exc:
            reader(ctx, just_under)
        assert exc.value.code == "INPUT_INVALID"
        assert exc.value.field == "budget_tokens"
        return fitted
    try:
        reduced = reader(ctx, just_under)
    except ContractError as exc:
        assert exc.code == "INPUT_INVALID" and exc.field == "budget_tokens"
    else:
        assert read_views._budget_bytes(reduced) <= size - 1
    return fitted


def test_final_serialized_budget_boundary_empty_nonempty_unavailable(app, monkeypatch):
    _assert_candidate_origin()
    core, ctx = app
    subject, request_id = _long_ids()
    empty_req = {
        "protocol_version": "1.1", "request_id": request_id, "subject": subject,
        "max_items": 16, "budget_tokens": 4096,
    }
    empty = _assert_exact_envelope_fit(core, ctx, empty_req, just_under_must_reject=True)
    assert empty["status"] == "no_match"
    assert read_views._budget_bytes(empty) > 220
    with pytest.raises(ContractError) as exc:
        core.profile(ctx, {**empty_req, "budget_tokens": 64})
    assert exc.value.field == "budget_tokens"
    with pytest.raises(ContractError) as exc:
        core.profile(ctx, {**empty_req, "budget_tokens": 220})
    assert exc.value.field == "budget_tokens"

    _accept(core, ctx, f"{subject} 配色 蓝色。", value="蓝色", kind="fact", predicate="配色", subject=subject)
    nonempty = _assert_exact_envelope_fit(core, ctx, empty_req)
    assert nonempty["sections"]["facts"]
    entity_empty = _assert_exact_envelope_fit(core, ctx, {
        **empty_req, "subject": "不存在的中文主体",
        "action": "related", "direction": "incoming",
    }, entity=True, just_under_must_reject=True)
    assert entity_empty["status"] == "no_match"

    def boom(*_args, **_kwargs):
        raise ContractError("VERSION_CONFLICT", "memory_epoch")

    monkeypatch.setattr(read_views, "_release_selected", boom)
    unavailable = _assert_exact_envelope_fit(core, ctx, empty_req, just_under_must_reject=True)
    assert unavailable["status"] == "unavailable"
    assert unavailable["resolved_subject"] is None


def test_candidate_union_and_alias_scan_cap_use_local_seam(app, monkeypatch):
    _assert_candidate_origin()
    core, ctx = app
    monkeypatch.setattr(read_views, "CANDIDATE_CAP", 2)
    released_sizes: list[int] = []
    original = read_views.release_objects

    def spy(storage, clock, context, refs, **kwargs):
        released_sizes.append(len(refs))
        assert len(refs) <= read_views.CANDIDATE_CAP
        return original(storage, clock, context, refs, **kwargs)

    monkeypatch.setattr(read_views, "release_objects", spy)
    for index, value in enumerate(("红", "绿", "蓝"), start=1):
        _accept(core, ctx, f"TEST-project 色板{index} {value}。", value=value, kind="fact", predicate=f"色板{index}")
    _accept(core, ctx, "TEST-alice 负责 TEST-project。", value="TEST-project", kind="fact", predicate="负责", subject="TEST-alice")
    _accept(core, ctx, "TEST-bob 依赖 TEST-project。", value="TEST-project", kind="fact", predicate="依赖", subject="TEST-bob")
    related = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-union", "subject": "TEST-project",
        "action": "related", "direction": "both", "max_items": 16, "budget_tokens": 4096,
    })
    assert released_sizes and max(released_sizes) <= 2
    assert related["scan_capped"] is True
    assert related["coverage"] == "partial"
    assert related["coverage"] != "complete_for_query"
    assert len(related["statements"]) <= 2

    mist = replace(ctx, project_id="TEST-mist-cap")
    target = _identity(core, mist)
    source = capture(core, mist, "TEST-mist 项目以后改名为 TEST暮光，内容不变，保留旧称 TEST晨雾。")
    alias = accept(core, mist, _alias(source, target, "TEST暮光")).items[0]
    assert alias.state == "active"
    monkeypatch.setattr(read_views, "CANDIDATE_CAP", 1)
    released_sizes.clear()
    unresolved = core.profile(mist, {
        "protocol_version": "1.1", "request_id": "TEST-alias-cap", "subject": "TEST暮光",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert unresolved["alias_resolution"] == "ambiguous"
    assert unresolved["resolved_subject"] is None
    assert unresolved["scan_capped"] is True
    assert unresolved["answerability"] == "ambiguous"
    assert "alias_ambiguous" in unresolved["gaps"]
    assert unresolved["sections"]["facts"] == []
    assert all(size <= 1 for size in released_sizes)

    monkeypatch.setattr(read_views, "CANDIDATE_CAP", 200)
    released_sizes.clear()
    resolved = core.profile(mist, {
        "protocol_version": "1.1", "request_id": "TEST-alias-min-fence", "subject": "TEST暮光",
        "max_items": 16, "budget_tokens": 4096,
    })
    assert resolved["alias_resolution"] == "resolved"
    assert resolved["resolved_subject"] == "TEST-mist"
    assert released_sizes and max(released_sizes) <= 2


def test_inverse_lookup_uses_effective_not_head_revision(app):
    _assert_candidate_origin()
    core, ctx = app
    owner, _ = _accept(
        core, ctx, "TEST-owner 拥有 TEST-project。",
        value="TEST-project", kind="fact", predicate="拥有", subject="TEST-owner",
    )
    future_source = capture(core, ctx, "从2026年9月10日起，TEST-owner 拥有 TEST-other。", when="2026-09-06T12:00:00Z")
    future = accept(core, ctx, draft(
        future_source, "TEST-other", kind="fact", predicate="拥有", subject="TEST-owner",
        valid_from="2026-09-10T00:00:00Z",
    )).items[0]
    assert future.ref == owner.ref and future.revision == 2
    assert core.current_claim(ctx, owner.ref).payload["value_text"] == "TEST-project"
    current_in = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-future-in", "subject": "TEST-project",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert owner.ref in {item["ref"] for item in current_in["statements"]}
    assert {item["value_text"] for item in current_in["statements"]} == {"TEST-project"}
    future_in = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-future-hidden", "subject": "TEST-other",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert future_in["status"] == "no_match"
    assert future.ref not in {item["ref"] for item in future_in["statements"]}

    proposed_source = capture(core, ctx, "假设TEST-owner 拥有 TEST-ghost。")
    proposed = accept(core, ctx, draft(
        proposed_source, "TEST-ghost", kind="fact", predicate="拥有", subject="TEST-owner",
    )).items[0]
    assert proposed.state == "proposed"
    ghost = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-proposed-in", "subject": "TEST-ghost",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert ghost["status"] == "no_match"
    still_current = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-still-current", "subject": "TEST-project",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert owner.ref in {item["ref"] for item in still_current["statements"]}

    core.clock.now = "2026-09-10T00:00:00Z"
    after = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-effective-later", "subject": "TEST-other",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert {item["value_text"] for item in after["statements"]} == {"TEST-other"}
    expired = core.entity(ctx, {
        "protocol_version": "1.1", "request_id": "TEST-superseded", "subject": "TEST-project",
        "action": "related", "direction": "incoming", "max_items": 16, "budget_tokens": 4096,
    })
    assert expired["status"] == "no_match"
    assert expired["statements"] == []
