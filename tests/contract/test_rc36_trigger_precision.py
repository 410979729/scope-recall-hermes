"""A source becomes a candidate's evidence when it can speak to that candidate.

Any shared term used to be enough. On alpha that had attached 116,000 sources to 1,215 live candidates, and
6% of them restated the candidate they were attached to: tool transcripts sharing one bigram with nearly every
candidate, each owing pages of trigger work and crowding the evidence an evaluation is shown. First-hand
testimony keeps the old rule, because a person can confirm a value without repeating it.
"""
from __future__ import annotations

import sqlite3

from scope_recall.core.claims import Qualification
from tests.contract.test_v11_claims import app, capture, draft  # noqa: F401  (fixture)


def _candidate(core, ctx, *, subject, predicate, value, key):
    source = capture(core, ctx, f"{subject} {predicate} {value}。", key=key)
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append("TEST-scope", draft(source, value, subject=subject, predicate=predicate),
                                 Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
                                 recorded_at=core.clock.utc_now())
        tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    return saved.ref


def _holds(core, candidate_ref, source_ref):
    with sqlite3.connect(core.storage.path) as db:
        return db.execute("SELECT count(*) FROM candidate_evidence WHERE candidate_ref=? AND source_ref=?",
                          (candidate_ref, source_ref)).fetchone()[0] == 1


def test_a_tool_transcript_sharing_only_a_word_is_not_evidence(app):
    core, ctx = app
    candidate = _candidate(core, ctx, subject="TEST-poster", predicate="配色", value="深蓝色", key="TEST-rc36/c1")
    unrelated = capture(core, ctx, "日志：配色方案模块已重新加载。", key="TEST-rc36/t1", origin="tool_observation")
    restating = capture(core, ctx, "当前主题读取结果：深蓝色。", key="TEST-rc36/t2", origin="tool_observation")
    naming = capture(core, ctx, "TEST-poster 的导出任务已排队。", key="TEST-rc36/t3", origin="tool_observation")

    assert not _holds(core, candidate, unrelated.ref)
    assert _holds(core, candidate, restating.ref)
    assert _holds(core, candidate, naming.ref)


def test_first_hand_testimony_sharing_a_word_is_still_evidence(app):
    core, ctx = app
    candidate = _candidate(core, ctx, subject="TEST-poster", predicate="配色", value="深蓝色", key="TEST-rc36/c2")
    said = capture(core, ctx, "配色就按刚才说的定下来。", key="TEST-rc36/h1")

    assert _holds(core, candidate, said.ref)


def test_a_self_subject_is_not_a_name_that_makes_a_transcript_evidence(app):
    core, ctx = app
    candidate = _candidate(core, ctx, subject="我", predicate="配色偏好", value="深蓝色", key="TEST-rc36/c3")
    transcript = capture(core, ctx, "我 执行了配色偏好检查脚本。", key="TEST-rc36/t4", origin="tool_observation")

    assert not _holds(core, candidate, transcript.ref)
