"""A verdict does not create permission to repeat its unchanged evidence question."""

from dataclasses import replace

import pytest
from scope_recall.core.claims import Qualification
from scope_recall.core.mutate import apply_claim

from tests.contract.test_candidate_lifecycle import Evaluator, _finish_source_work, _settle_evidence, _sweep
from tests.contract.test_claims import app, capture, draft


def _conflict(core, ctx):
    source = capture(core, ctx, "TEST-project 配色蓝色。TEST-project 配色银色。", when=None)
    a = draft(source, "蓝色", valid_from=None)
    b = draft(source, "银色", valid_from=None)
    with core.storage.write(ctx) as tx:
        old = tx.claims.append(
            "TEST-scope",
            a,
            Qualification("active", "direct_report", "direct_human_statement"),
            recorded_at=core.clock.utc_now(),
        )
        head = tx.claims.append(
            "TEST-scope",
            b,
            Qualification("disputed", "direct_report", "conflicting_evidence_order_unknown"),
            recorded_at=core.clock.utc_now(),
            previous=old,
        )
        tx.candidates.register(head.ref, head.revision, observed_at=core.clock.utc_now())
    return head, a, b


def test_final_conflict_qualification_deduplicates_the_same_evidence(app):
    core, ctx = app
    head, _a, b = _conflict(core, ctx)
    judge_ctx = replace(ctx, session_id="TEST-judge")
    with core.storage.write(judge_ctx) as tx:
        results = [apply_claim(tx, b, "TEST-scope", core.clock.utc_now()) for _ in range(3)]
    assert [(r.revision, r.disposition, r.state) for r in results] == [(2, "duplicate", "disputed")] * 3
    assert len(core.claim_history(ctx, head.ref)) == 2


def test_a_verdict_with_a_proved_condition_registers_its_distinct_fact(app):
    core, ctx = app
    source = capture(core, ctx, "离线时，TEST-project 配色蓝色。")
    proposal = draft(source)
    with core.storage.write(ctx) as tx:
        candidate = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "condition_not_preserved"),
            recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(candidate.ref, candidate.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)
    evaluator = Evaluator(dict(proposal, conditions=["离线"]))
    core.drain_worker(ctx, max_items=1, remaining_seconds=20, consolidation=evaluator)
    with core.storage.read(ctx) as tx:
        refs = tx.claims.list_refs(subject="TEST-project", predicate="配色")
        produced = next(ref for ref in refs if ref != candidate.ref)
        row = (
            tx._check()
            .execute("SELECT processing_state FROM candidate_lifecycle WHERE candidate_ref=?", (produced,))
            .fetchone()
        )
    assert row is not None
    assert row[0] == "resolved"


@pytest.mark.parametrize("first", ["same", "alternate"])
def test_evaluation_revisions_inherit_the_answered_question_until_new_evidence(app, first):
    core, ctx = app
    head, a, b = _conflict(core, ctx)
    ctx = replace(ctx, session_id="TEST-judge")
    _finish_source_work(core)

    class ChangingEvaluator(Evaluator):
        def evaluate_candidate(self, candidate, sources, **kwargs):
            self.proposal = b if first == "same" else dict(a if self.calls % 2 == 0 else b)
            if first == "alternate":
                self.proposal["evidence_spans"] = [
                    dict(a["evidence_spans"][0], start=0, end=len(a["evidence_spans"][0]["quote"]))
                ]
            return super().evaluate_candidate(candidate, sources, **kwargs)

    evaluator = ChangingEvaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=20, consolidation=evaluator)
    initial_versions = len(core.claim_history(ctx, head.ref))
    assert evaluator.calls == 1
    if first == "alternate":
        assert initial_versions == 3
    for _ in range(4):
        _settle_evidence(core)
        _sweep(core, ctx)
        core.drain_worker(ctx, max_items=8, remaining_seconds=20, consolidation=evaluator)
    assert evaluator.calls == 1
    assert len(core.claim_history(ctx, head.ref)) == initial_versions
    capture(core, ctx, "TEST-project 配色蓝色。TEST-project 配色银色。", key="TEST-new-evidence", when=None)
    _finish_source_work(core)
    _settle_evidence(core)
    assert _sweep(core, ctx) == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=20, consolidation=evaluator)
    assert evaluator.calls == 2


def test_identical_active_replay_outside_the_capture_session_preserves_the_head(app):
    from tests.contract.test_claims import accept

    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色蓝色。", when=None)
    proposal = draft(source, valid_from=None)
    saved = accept(core, ctx, proposal).items[0]
    assert saved.state == "active"
    replay_ctx = replace(ctx, session_id="TEST-background")
    with core.storage.write(replay_ctx) as tx:
        replay = apply_claim(tx, proposal, "TEST-scope", core.clock.utc_now())
    assert (replay.revision, replay.disposition, replay.state) == (1, "duplicate", "active")
    assert len(core.claim_history(ctx, saved.ref)) == 1
    current = core.current_claim(ctx, saved.ref)
    assert (current.revision, current.state, current.payload) == (1, "active", proposal)


@pytest.mark.parametrize("reply", ["echo", "unknown_label", "wrong_source", "internal_key"])
def test_candidate_speaker_label_round_trip_rechecks_the_cited_principal(app, reply):
    import json

    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core.consolidate import candidate_evaluation_messages

    core, ctx = app
    owners = [
        replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", f"principal:TEST-{n}"))
        for n in ("A", "B")
    ]
    sources = [capture(core, owner, "我喜欢蓝色。", key=f"TEST-speaker/{n}") for n, owner in enumerate(owners)]
    stored = draft(sources[0], kind="preference", subject="principal:TEST-A", predicate="喜欢")
    stored["evidence_spans"] += draft(sources[1])["evidence_spans"]
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            stored,
            Qualification("proposed", "inferred_suggestion", "synthetic_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    _finish_source_work(core)

    class EchoEvaluator(Evaluator):
        def evaluate_candidate(self, candidate, sources, **kwargs):
            messages = candidate_evaluation_messages(candidate, sources)
            assert "principal:" not in json.dumps(messages)
            label = json.loads(messages[-1]["content"].removeprefix("candidate="))["subject"]
            assert label == "speaker_1"
            selected = next(s for s in sources if s.ref == sources_by_reply[reply].ref)
            subject = {"unknown_label": "speaker_999", "internal_key": "principal:TEST-A"}.get(reply, label)
            self.proposal = draft(selected, kind="preference", subject=subject, predicate="喜欢")
            return super().evaluate_candidate(candidate, sources, **kwargs)

    sources_by_reply = dict.fromkeys(("echo", "unknown_label", "internal_key"), sources[0])
    sources_by_reply["wrong_source"] = sources[1]
    evaluator = EchoEvaluator()
    receipt = core.drain_worker(ctx, max_items=1, remaining_seconds=20, consolidation=evaluator)
    assert evaluator.calls == 1
    current = core.current_claim(ctx, saved.ref)
    if reply == "echo":
        assert receipt.items[0].error_code is None
        assert (current.state, current.payload["subject"]) == ("active", "principal:TEST-A")
    else:
        assert receipt.items[0].error_code == "derivation_invalid"
        assert current is None or current.state != "active"
        assert len(core.claim_history(ctx, saved.ref)) == 1
