"""P07 M11 project-name alias authority and scope contracts."""
from dataclasses import replace
import itertools

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from tests.contract.test_v11_claims import Clock, capture, accept, draft
from tests.v11_support import context


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-aliases"), project_id="TEST-mist", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _identity(core, ctx, name="TEST晨雾"):
    source = capture(core, ctx, f"TEST-mist 项目名称是 {name}。")
    item = accept(core, ctx, draft(source, name, kind="fact", predicate="项目名称", subject="TEST-mist" )).items[0]
    assert item.state == "active"
    return item


def _alias(source, target, name):
    return dict(kind="alias", subject="TEST-mist", predicate="项目别名", value_text=name,
                conditions=[], statement_kind="assertion", valid_from=source.event["occurred_at"], valid_to=None,
                evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision,
                                     quote=source.event["content"])],
                alias=dict(name=name, target_ref=target.ref, scope_description="同一项目的改名，保留旧称"))


def test_M11_asserted_rename_keeps_old_identity_and_records_new_alias(app):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, "TEST-mist 项目以后改名为 TEST暮光，内容不变，保留旧称 TEST晨雾。")
    result = accept(core, ctx, _alias(source, target, "TEST暮光"))
    assert result.items[0].state == "active"
    assert core.current_claim(ctx, target.ref).payload["value_text"] == "TEST晨雾"
    alias = core.current_claim(ctx, result.items[0].ref)
    assert alias.payload["alias"]["target_ref"] == target.ref


def test_alias_target_from_other_project_is_not_accepted(app):
    core, ctx = app
    other = replace(ctx, project_id="TEST-other")
    source = capture(core, other, "TEST-other 项目名称是 TEST晨雾。")
    target = accept(core, other, draft(source, "TEST晨雾", kind="fact", predicate="项目名称", subject="TEST-other")).items[0]
    local = capture(core, ctx, "TEST-mist 项目改名为 TEST暮光，保留旧称。")
    result = accept(core, ctx, _alias(local, target, "TEST暮光"))
    assert result.items[0].state == "proposed"


@pytest.mark.parametrize("raw", [
    "TEST-mist 项目改名为 TEST暮光吗？",
    "TEST-mist 项目改名为 TEST暮光吗",
    "TEST-mist 如果改名为 TEST暮光，就保留旧称。",
    "TEST-mist 不是改名为 TEST暮光，仍叫 TEST晨雾。",
])
def test_question_or_negated_alias_source_stays_proposed(app, raw):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, raw)
    result = accept(core, ctx, _alias(source, target, "TEST暮光"))
    assert result.items[0].state == "proposed"


def test_model_invented_target_ref_stays_proposed(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-mist 项目改名为 TEST暮光，保留旧称。")
    result = accept(core, ctx, _alias(source, type("Target", (), {"ref": "missing-target"})(), "TEST暮光"))
    assert result.items[0].state == "proposed"


@pytest.mark.parametrize("raw", [
    "TEST-mist 项目将参考 TEST暮光 的设计。",
    "TEST-other 项目以后改名为 TEST暮光，内容不变。",
    "TEST-mist 项目改名为 TEST暮光二号，内容不变。",
])
def test_mention_other_identity_or_name_prefix_is_not_rename(app, raw):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, raw)
    result = accept(core, ctx, _alias(source, target, "TEST暮光"))
    assert result.items[0].state == "proposed"


def test_existing_unrelated_claim_cannot_supply_project_identity(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-mist 配色是蓝色。")
    target = accept(core, ctx, draft(source, "蓝色", kind="fact", predicate="配色", subject="TEST-mist")).items[0]
    assert target.state == "active"
    rename = capture(core, ctx, "TEST-mist 项目以后改名为 TEST暮光，内容不变。")
    result = accept(core, ctx, _alias(rename, target, "TEST暮光"))
    assert result.items[0].state == "proposed"


def test_M11_source_old_name_can_anchor_rename_without_runtime_id_in_quote(app):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, "TEST晨雾项目以后改名为TEST暮光，内容不变。")
    result = accept(core, ctx, _alias(source, target, "TEST暮光"))
    assert result.items[0].state == "active"
    assert core.current_claim(ctx, target.ref).payload["value_text"] == "TEST晨雾"


@pytest.mark.parametrize("raw,quote", [
    (
        "海上晨雾项目以后改名为暮光，内容不变。",
        "晨雾项目以后改名为暮光",
    ),
    (
        "并非 晨雾项目以后改名为暮光，内容不变。",
        "晨雾项目以后改名为暮光",
    ),
])
def test_alias_requires_complete_supported_name_and_local_polarity(app, raw, quote):
    core, ctx = app
    target = _identity(core, ctx, name="晨雾")
    source = capture(core, ctx, raw)
    proposal = _alias(source, target, "暮光")
    proposal["evidence_spans"][0]["quote"] = quote

    result = accept(core, ctx, proposal)

    assert result.items[0].state == "proposed"


def test_unrelated_negative_clause_does_not_block_supported_rename(app):
    core, ctx = app
    target = _identity(core, ctx, name="TEST晨雾")
    source = capture(
        core,
        ctx,
        "TEST晨雾项目以后改名为TEST暮光，另一个项目并非改名为黑曜石。",
    )

    result = accept(core, ctx, _alias(source, target, "TEST暮光"))

    assert result.items[0].state == "active"


@pytest.mark.parametrize(
    ("raw", "quote"),
    [
        (
            "OTHER-TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "OTHER-TEST-mist 项目以后改名为TEST暮光",
        ),
        (
            "OTHER-TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "TEST-mist 项目以后改名为TEST暮光",
        ),
        (
            "OTHER/TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "OTHER/TEST-mist 项目以后改名为TEST暮光",
        ),
        (
            "OTHER/TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "TEST-mist 项目以后改名为TEST暮光",
        ),
        (
            "OTHER.TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "OTHER.TEST-mist 项目以后改名为TEST暮光",
        ),
        (
            "OTHER.TEST-mist 项目以后改名为TEST暮光，内容不变。",
            "TEST-mist 项目以后改名为TEST暮光",
        ),
    ],
)
def test_alias_does_not_bind_suffix_of_composite_identifier(app, raw, quote):
    core, ctx = app
    target = _identity(core, ctx)
    source = capture(core, ctx, raw)
    proposal = _alias(source, target, "TEST暮光")
    proposal["evidence_spans"][0]["quote"] = quote

    result = accept(core, ctx, proposal)

    assert result.items[0].state == "proposed"


def _said(text, *, principal=("human", "verified"), origin="human_direct"):
    from scope_recall.core.claims import RootEvidence

    return RootEvidence("event-TEST-said", 1, origin, None, text, "2026-09-23T16:51:04Z", "complete", "TEST-session",
                        source_principal={"kind": principal[0], "resolution": principal[1], "principal_ref": "principal:TEST-owner"})


def _naming_alias(root, name, *, target_ref=None):
    """The shape the consolidation model gave the pilot: an alias of a thing nothing yet names, targeting the message itself."""
    return dict(kind="alias", subject="我的猫咪", predicate="别名", value_text=name, conditions=[],
                statement_kind="assertion", valid_from=root.occurred_at, valid_to=None,
                evidence_spans=[dict(source_ref=root.ref, source_revision=root.revision, quote=root.content)],
                alias=dict(name=name, target_ref=target_ref or f"{root.ref}@{root.revision}", scope_description="TEST 名字"))


@pytest.mark.parametrize("said,name,subject,verb", [
    ("我家猫咪叫年糕", "年糕", "我家猫咪", "叫"),
    ("我家猫咪的名字是年糕", "年糕", "我家猫咪", "的名字是"),
    ("My cat is called Xiaosuan", "Xiaosuan", "My cat", "is called"),
])
def test_naming_a_thing_nothing_is_known_about_is_a_fact_not_an_alias(said, name, subject, verb):
    """Told the cat's name, the model filed an alias with no known target; it could never be proved,
    stayed proposed, and no agent recalled the name.  Said as a name, it is the thing's name."""
    from scope_recall.core.claim_normalization import name_frame
    from scope_recall.core.claims import qualify

    root = _said(said)
    framed = name_frame(_naming_alias(root, name), (root,))
    assert tuple(framed[key] for key in ("kind", "subject", "predicate", "value_text")) == ("fact", subject, verb, name)
    assert "alias" not in framed
    assert qualify(framed, (root,)).state == "active"


@pytest.mark.parametrize("root,target_ref", [
    (_said("年糕这个词挺好听的"), None),                                    # not said as a name
    (_said("我家猫咪叫年糕"), "claim-" + "a" * 64),                        # a real alias, proved its own way
    (_said("我家猫咪叫年糕", principal=("human", "unresolved")), None),     # nobody verified said it
    (_said("我家猫咪叫年糕", origin="assistant_visible"), None),            # the agent said it
])
def test_only_a_first_hand_naming_statement_is_reframed(root, target_ref):
    from scope_recall.core.claim_normalization import name_frame

    proposal = _naming_alias(root, "年糕", target_ref=target_ref)
    assert name_frame(proposal, (root,)) is proposal


def test_a_naming_alias_an_earlier_release_stored_is_repaired_into_the_fact(app, monkeypatch):
    """The pilot's store already holds the cat's name as an unproved alias; the frame repair re-frames it."""
    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core import claim_normalization

    core, ctx = app
    owner = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", principal_ref="principal:TEST-owner",
                                                                 display_name="TEST"))
    source = capture(core, owner, "我家猫咪叫年糕")
    stored = dict(kind="alias", subject="我的猫咪", predicate="别名", value_text="年糕", conditions=[],
                  statement_kind="assertion", valid_from=source.event["occurred_at"], valid_to=None,
                  evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"])],
                  alias=dict(name="年糕", target_ref=f"{source.ref}@{source.revision}", scope_description="TEST 名字"))
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(claim_normalization, "name_frame", lambda proposal, roots: proposal)
        assert accept(core, owner, stored).items[0].state == "proposed"

    report = core.repair_claim_frames(owner, limit=16)
    active = [item for item in report["items"] if item["state"] == "active"]
    assert len(active) == 1 and not report["errors"], report
    fact = core.current_claim(owner, active[0]["ref"])
    assert tuple(fact.payload[key] for key in ("kind", "subject", "predicate", "value_text")) == ("fact", "我家猫咪", "叫", "年糕")
