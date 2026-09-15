"""R1 semantic acceptance cases over synthetic, offline source text."""
from dataclasses import replace
import itertools
import sqlite3

import pytest

from scope_recall.contracts import ContractError, TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import RootEvidence, qualify
from scope_recall.core.candidate_storage import CandidateLifecycle
from tests.contract.test_v11_claims import (
    Clock,
    accept,
    capture,
    draft,
    initial,
    revise_request,
)
from tests.v11_support import context


def qualification(
    text,
    *,
    kind="decision",
    subject="TEST-project",
    predicate="配色",
    value="蓝色",
    conditions=(),
    statement_kind="assertion",
):
    root = RootEvidence(
        "TEST-root",
        1,
        "human_direct",
        None,
        text,
        "2026-09-06T12:00:00Z",
        "complete",
        "TEST-session",
    )
    proposal = dict(
        kind=kind,
        subject=subject,
        predicate=predicate,
        value_text=value,
        conditions=list(conditions),
        statement_kind=statement_kind,
        valid_from=root.occurred_at,
        valid_to=None,
        evidence_spans=[
            dict(source_ref=root.ref, source_revision=root.revision, quote=text)
        ],
    )
    return qualify(proposal, (root,))


def test_request_statement_cannot_become_a_durable_claim():
    result = qualification(
        "TEST-project 需要发布说明。",
        kind="fact",
        predicate="需要",
        value="发布说明",
        statement_kind="request",
    )
    assert result.state == "proposed"
    assert result.reason == "statement_not_asserted"


def test_raw_execution_request_cannot_be_laundered_as_an_assertion():
    result = qualification(
        "请帮TEST-project生成发布说明。",
        kind="decision",
        predicate="生成",
        value="发布说明",
        statement_kind="assertion",
    )
    assert result.state == "proposed"
    assert result.reason == "transient_request_not_durable"


def test_durable_directive_is_not_misclassified_as_a_transient_request():
    result = qualification(
        "以后请让TEST-project默认使用蓝色。",
        kind="decision",
        predicate="使用",
        value="蓝色",
        statement_kind="decision",
    )
    assert result.state == "active"


@pytest.mark.parametrize(
    "text",
    [
        "也许TEST-project配色蓝色。",
        "我猜TEST-project配色蓝色。",
        "听说TEST-project配色蓝色。",
        "据说TEST-project配色蓝色。",
    ],
)
def test_uncertain_or_hearsay_text_cannot_become_current_truth(text):
    result = qualification(text)
    assert result.state == "proposed"
    assert result.reason == "hypothetical_or_undecided"


def test_reported_third_party_preference_is_not_the_current_users_preference():
    result = qualification(
        "同事说TEST-project偏好蓝色。",
        kind="preference",
        predicate="偏好",
    )
    assert result.state == "proposed"
    assert result.reason == "other_speaker"


def test_separate_filename_and_hash_mentions_do_not_prove_the_relationship():
    result = qualification(
        "发布包已经生成，文件名是release.whl。另一个产物的SHA-256是abc123。",
        kind="fact",
        subject="release.whl",
        predicate="SHA-256",
        value="abc123",
    )
    assert result.state == "proposed"
    assert result.reason == "fact_entailment_unproved"


def test_complete_subject_relation_value_frame_remains_eligible():
    result = qualification(
        "发布包 哈希是 abc123。",
        kind="fact",
        subject="发布包",
        predicate="哈希是",
        value="abc123",
    )
    assert result.state == "active"


def test_condition_must_be_supported_in_the_same_assertion():
    invented = qualification(
        "TEST-project配色蓝色。",
        conditions=("写小说时",),
    )
    assert invented.state == "proposed"
    assert invented.reason == "condition_not_supported"

    unrelated = qualification(
        "写小说时使用红色。TEST-project配色蓝色。",
        conditions=("写小说时",),
    )
    assert unrelated.state == "proposed"
    assert unrelated.reason == "condition_not_supported"

    supported = qualification(
        "写小说时，TEST-project配色蓝色。",
        conditions=("写小说时",),
    )
    assert supported.state == "active"


@pytest.fixture
def app(tmp_path):
    ctx = replace(
        context(tmp_path / "TEST-r1-semantics"),
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def test_direct_revise_api_rejects_late_old_correction_evidence(app):
    core, ctx = app
    item, _ = initial(
        core,
        ctx,
        value="银色",
        when="2026-09-03T12:00:00Z",
    )
    old = capture(
        core,
        ctx,
        "Please correct TEST-project 配色: 蓝色。",
        when="2026-09-01T12:00:00Z",
    )
    with pytest.raises(ContractError, match="late_historical_correction"):
        core.revise(ctx, revise_request(item, old, "蓝色"), remaining_seconds=10)
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "银色"
    assert len(core.claim_history(ctx, item.ref)) == 1


def test_ambiguous_pronoun_correction_is_preserved_without_guessing(app):
    core, ctx = app
    first, _ = initial(core, ctx, value="蓝色")
    second_source = capture(core, ctx, "测试设备 字号 16。")
    second = accept(
        core,
        ctx,
        draft(second_source, "16", kind="fact", subject="测试设备", predicate="字号"),
    ).items[0]
    assert second.state == "active"

    correction = capture(
        core,
        ctx,
        "把那个改掉。",
        when="2026-09-06T12:00:00Z",
    )
    unresolved = core.unresolved_updates(ctx)
    assert len(unresolved) == 1
    assert unresolved[0]["source_ref"] == correction.ref
    assert set(unresolved[0]["candidate_refs"]) == {first.ref, second.ref}
    assert core.current_claim(ctx, first.ref).revision == 1
    assert core.current_claim(ctx, second.ref).revision == 1


def _self_preference(source, value):
    return {
        "kind": "preference",
        "subject": "user",
        "predicate": "喜欢",
        "value_text": value,
        "conditions": [],
        "statement_kind": "assertion",
        "valid_from": source.event["occurred_at"],
        "valid_to": None,
        "evidence_spans": [
            {
                "source_ref": source.ref,
                "source_revision": source.revision,
                "quote": source.event["content"],
            }
        ],
    }


def test_verified_speakers_get_distinct_internal_subjects_in_a_shared_scope(app):
    core, ctx = app
    alice = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice"
        ),
    )
    bob = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-bob"
        ),
    )
    alice_source = capture(core, alice, "我喜欢蓝色。")
    alice_item = accept(core, alice, _self_preference(alice_source, "蓝色")).items[0]
    bob_source = capture(core, bob, "我喜欢绿色。")
    bob_item = accept(core, bob, _self_preference(bob_source, "绿色")).items[0]

    assert alice_item.state == bob_item.state == "active"
    assert alice_item.ref != bob_item.ref
    assert core.current_claim(ctx, alice_item.ref).payload["subject"] == "principal:TEST-alice"
    assert core.current_claim(ctx, bob_item.ref).payload["subject"] == "principal:TEST-bob"


def test_multiple_verified_speakers_cannot_jointly_authorize_one_self_claim(app):
    core, ctx = app
    alice = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice"
        ),
    )
    bob = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-bob"
        ),
    )
    alice_source = capture(core, alice, "我喜欢蓝色。")
    bob_source = capture(core, bob, "我喜欢蓝色。")
    proposal = _self_preference(alice_source, "蓝色")
    proposal["evidence_spans"].append(
        {
            "source_ref": bob_source.ref,
            "source_revision": bob_source.revision,
            "quote": bob_source.event["content"],
        }
    )
    result = accept(core, alice, proposal).items[0]
    history = core.claim_history(ctx, result.ref)

    assert result.state == "proposed"
    assert history[0].reason == "source_identity_conflict"
    assert history[0].payload["subject"].startswith("unresolved-source:")


def test_default_conditional_and_one_turn_preferences_use_distinct_slots(app):
    core, ctx = app
    alice = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice"
        ),
    )
    sources = (
        capture(core, alice, "我平时喜欢简短回复。"),
        capture(core, alice, "写小说时，我喜欢详细回复。"),
        capture(core, alice, "这次我喜欢展开说明。"),
    )
    proposals = (
        _self_preference(sources[0], "简短回复"),
        {**_self_preference(sources[1], "详细回复"), "conditions": ["写小说时"]},
        {**_self_preference(sources[2], "展开说明"), "conditions": ["这次"]},
    )
    items = [accept(core, alice, proposal).items[0] for proposal in proposals]

    states = [
        (item.state, core.claim_history(ctx, item.ref)[0].reason)
        for item in items
    ]
    assert states == [
        ("active", "explicit_scoped_source"),
        ("active", "explicit_scoped_source"),
        ("active", "explicit_scoped_source"),
    ]
    assert len({item.ref for item in items}) == 3
    assert core.current_claim(ctx, items[0].ref).payload["value_text"] == "简短回复"
    assert core.current_claim(ctx, items[1].ref).payload["conditions"] == ["写小说时"]
    assert core.current_claim(ctx, items[2].ref).payload["conditions"] == ["这次"]


def test_c2_fact_application_registers_c3_candidate_in_the_same_transaction(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    proposal = draft(source, "蓝色")
    proposal["statement_kind"] = "proposal"

    item = accept(core, ctx, proposal).items[0]

    with core.storage.read(ctx) as tx:
        summary = tx.candidates.summary()
    with sqlite3.connect(core.storage.path) as conn:
        lifecycle = conn.execute(
            """SELECT processing_state,reason FROM candidate_lifecycle
               WHERE candidate_ref=? AND candidate_revision=?""",
            (item.ref, item.revision),
        ).fetchone()
        queued = conn.execute(
            """SELECT count(*) FROM work_items
               WHERE work_type='evaluate_candidate' AND subject_ref=?""",
            ("candidate:" + item.ref,),
        ).fetchone()[0]

    assert item.state == "proposed"
    assert lifecycle == ("pending_evaluation", "new_evidence")
    assert summary.pending_evaluation == 1
    assert queued == 1


def test_c3_registration_failure_rolls_back_the_c2_fact_write(app, monkeypatch):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")

    def fail_registration(*_args, **_kwargs):
        raise ContractError("STORAGE_UNAVAILABLE", "TEST_candidate_registration")

    monkeypatch.setattr(CandidateLifecycle, "register", fail_registration)
    with pytest.raises(ContractError, match="TEST_candidate_registration"):
        accept(core, ctx, draft(source, "蓝色"))

    with core.storage.read(ctx) as tx:
        assert tx.claims.list_refs() == ()
        assert tx.candidates.summary().resolved == 0


def test_unresolved_self_reports_are_isolated_and_never_become_current(app):
    core, ctx = app
    unresolved = replace(
        ctx,
        source_principal=TrustedSourcePrincipal("human", "unresolved"),
    )
    first_source = capture(core, unresolved, "我喜欢蓝色。")
    first = accept(core, unresolved, _self_preference(first_source, "蓝色")).items[0]
    second_source = capture(core, unresolved, "我喜欢绿色。")
    second = accept(core, unresolved, _self_preference(second_source, "绿色")).items[0]

    assert first.state == second.state == "proposed"
    assert first.ref != second.ref
    assert core.current_claim(ctx, first.ref) is None
    first_subject = core.claim_history(ctx, first.ref)[0].payload["subject"]
    second_subject = core.claim_history(ctx, second.ref)[0].payload["subject"]
    assert first_subject.startswith("unresolved-source:")
    assert second_subject.startswith("unresolved-source:")
    assert first_subject != second_subject


def test_verified_first_person_correction_cannot_cross_speakers(app):
    core, ctx = app
    alice = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice"
        ),
    )
    bob = replace(
        ctx,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-bob"
        ),
    )
    source = capture(core, alice, "我喜欢蓝色。")
    item = accept(core, alice, _self_preference(source, "蓝色")).items[0]

    capture(core, bob, "我把喜欢改成绿色。", when="2026-09-04T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"

    capture(core, alice, "我把喜欢改成红色。", when="2026-09-05T12:00:00Z")
    current = core.current_claim(ctx, item.ref)
    assert current.payload["value_text"] == "红色"
    assert current.revision == 2
