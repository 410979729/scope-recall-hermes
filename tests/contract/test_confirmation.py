"""A person may adopt a proposal the evidence alone would not establish.

Covers ``core/confirmation.py``, ``core/mutate.capture_confirmation`` and the
``requalify`` re-judgement.  Both exist because a claim is judged exactly once,
by one source: without them a gate repair never reaches the 366 proposals
already stored, and a person who reads one has no way to keep it.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.confirmation import (
    CONFIRMED_REASON,
    adoption_clause,
    confirmation_targets,
    is_confirmation,
)
from scope_recall.core.source_qualification import bound_literal
from test_v11_claims import app, accept, capture, draft

#: An elliptical statement: no active-voice subject->relation->value order, so
#: the fact gate refuses it on its own. Exactly the population confirmation is
#: for.
ELLIPTICAL = "TEST-instrument：SN-4471。"


def _claim(core, ctx, *, key="TEST-confirm/1"):
    source = capture(core, ctx, ELLIPTICAL, key=key)
    result = accept(core, ctx, draft(source, "SN-4471", kind="fact", subject="TEST-instrument",
                                     predicate="序列号", statement_kind="assertion"))
    return result.items[0].ref


def _head(core, ctx, ref):
    with core.storage.read(ctx) as tx:
        row = tx._check().execute(
            """SELECT state, qualification_reason, revision FROM claim_versions
               WHERE claim_id=? AND recorded_to IS NULL""", (ref,),
        ).fetchone()
    return (row["state"], row["qualification_reason"])


# --------------------------------------------------------------------------
# Recognising the act
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "记住 TEST-instrument 的序列号。", "这条记下来。", "保留这条。", "以后就这样。",
    "remember this one", "please save that", "confirm this",
])
def test_an_explicit_adoption_is_recognised(text):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", [
    "不要记住这个。", "先别记。", "要不要记住？", "如果他说了就记住。",
    "他说记住这条。", "举例：记住这条。", "do not remember this", "should I remember this?",
    "好的。", "ok", "收到", "",
])
def test_anything_short_of_adoption_is_not(text):
    assert is_confirmation(text) is False


#: Reported by review.  Every one of these read as adoption under the first
#: version of the rule, and the first one would have promoted the very claim the
#: person was rejecting by name.
@pytest.mark.parametrize("text", [
    "记住 references/topic.md 这条是错的，不要用。",
    "上次你记住的那条 configuration 值是错的",
    "别把 topic.md 那条记住，它已经过时了",
    "remember this: the port was wrong",
    "我记住了，下次注意",
])
def test_rejecting_a_claim_by_name_is_not_adopting_it(text):
    assert is_confirmation(text) is False
    assert adoption_clause(text) is None


def test_the_marker_and_the_name_must_share_a_clause():
    """Two instructions in one message are two instructions."""
    versions = [_Version("claim-b", "topic.md", "is", "x")]
    assert confirmation_targets("记住昨天那件事，另外 topic.md 那条删掉。",
                                versions, bound_literal=bound_literal) == ()


def test_a_negative_clause_named_by_polarity_is_also_refused():
    """Both kinds of negative count: evaluation and grammatical polarity."""
    assert is_confirmation("记住 topic.md 那条不能用。") is False


def test_a_negation_containing_the_marker_is_read_as_the_negation():
    """"不要记住" contains "记住"; order of checks decides the meaning."""
    assert is_confirmation("不要记住 TEST-instrument 的序列号。") is False


@pytest.mark.parametrize("bad", [None, 17, "   "])
def test_non_text_is_never_a_confirmation(bad):
    assert is_confirmation(bad) is False


class _Version:
    def __init__(self, ref, subject, predicate, value):
        self.ref = ref
        self.payload = {"subject": subject, "predicate": predicate, "value_text": value}


def test_targets_require_the_claim_to_be_literally_named():
    versions = [_Version("claim-a", "TEST-instrument", "序列号", "SN-4471"),
                _Version("claim-b", "TEST-other", "型号", "TX-9")]
    assert [v.ref for v in confirmation_targets(
        "记住 TEST-instrument 的序列号。", versions, bound_literal=bound_literal)] == ["claim-a"]
    assert confirmation_targets("记住这条。", versions, bound_literal=bound_literal) == ()


def test_targets_use_the_same_identifier_boundary_as_the_gates():
    versions = [_Version("claim-a", "SN-4471", "是", "x")]
    assert confirmation_targets("记住 SN-4471。", versions, bound_literal=bound_literal)
    assert confirmation_targets("记住 SN-44710。", versions, bound_literal=bound_literal) == ()


# --------------------------------------------------------------------------
# Through the real capture path
# --------------------------------------------------------------------------

def test_naming_a_proposal_adopts_it(app):
    core, ctx = app
    ref = _claim(core, ctx)
    assert _head(core, ctx, ref)[0] == "proposed"
    capture(core, ctx, "记住 TEST-instrument 的序列号。", key="TEST-confirm/yes")
    assert _head(core, ctx, ref) == ("active", CONFIRMED_REASON)


def test_a_bare_confirmation_adopts_nothing(app):
    """Guessing which proposal somebody meant is the inference this refuses."""
    core, ctx = app
    ref = _claim(core, ctx)
    capture(core, ctx, "这条记下来。", key="TEST-confirm/bare")
    assert _head(core, ctx, ref)[0] == "proposed"


def test_an_ambiguous_confirmation_adopts_nothing(app):
    core, ctx = app
    first = _claim(core, ctx, key="TEST-confirm/a")
    second_source = capture(core, ctx, "TEST-instrument：TX-9。", key="TEST-confirm/b")
    second = accept(core, ctx, draft(second_source, "TX-9", kind="fact", subject="TEST-instrument",
                                     predicate="型号", statement_kind="assertion")).items[0].ref
    capture(core, ctx, "记住 TEST-instrument 的那条。", key="TEST-confirm/ambiguous")
    assert _head(core, ctx, first)[0] == "proposed"
    assert _head(core, ctx, second)[0] == "proposed"


def test_a_refusal_to_remember_adopts_nothing(app):
    core, ctx = app
    ref = _claim(core, ctx)
    capture(core, ctx, "不要记住 TEST-instrument 的序列号。", key="TEST-confirm/no")
    assert _head(core, ctx, ref)[0] == "proposed"


def test_only_a_person_can_adopt(app):
    core, ctx = app
    ref = _claim(core, ctx)
    capture(core, ctx, "记住 TEST-instrument 的序列号。", key="TEST-confirm/tool",
            origin="tool_observation")
    assert _head(core, ctx, ref)[0] == "proposed"


def test_the_confirmation_is_kept_as_evidence(app):
    """Who vouched for a claim has to be answerable later."""
    core, ctx = app
    ref = _claim(core, ctx)
    confirmation = capture(core, ctx, "记住 TEST-instrument 的序列号。", key="TEST-confirm/evidence")
    with core.storage.read(ctx) as tx:
        head = next(v for v in tx.claims.versions(ref) if v.revision == v.current_revision)
    cited = {span["source_ref"] for span in head.payload["evidence_spans"]}
    assert confirmation.ref in cited


# --------------------------------------------------------------------------
# Re-judging what is already stored
# --------------------------------------------------------------------------

def test_requalify_changes_nothing_when_the_rules_have_not_moved(app):
    core, ctx = app
    _claim(core, ctx)
    report = core.requalify_claims(ctx, limit=32, dry_run=True)
    assert report["changed"] == [] and report["examined"] >= 1


def test_requalify_previews_without_writing(app, monkeypatch):
    """A preview that could write would be the wrong thing to reach for first."""
    core, ctx = app
    ref = _claim(core, ctx)
    from scope_recall.core import claims as claims_module
    from scope_recall.core.claims import Qualification

    monkeypatch.setattr(claims_module, "qualify",
                        lambda *a, **k: Qualification("active", "direct_report", "TEST-rule"))
    report = core.requalify_claims(ctx, limit=32, dry_run=True)
    assert [item["now"] for item in report["changed"]] == ["active:TEST-rule"]
    assert report["applied"] is False
    assert _head(core, ctx, ref)[0] == "proposed", "the preview wrote nothing"


def test_requalify_applies_the_new_verdict(app, monkeypatch):
    core, ctx = app
    ref = _claim(core, ctx)
    from scope_recall.core import claims as claims_module
    from scope_recall.core.claims import Qualification

    monkeypatch.setattr(claims_module, "qualify",
                        lambda *a, **k: Qualification("active", "direct_report", "TEST-rule"))
    report = core.requalify_claims(ctx, limit=32, dry_run=False)
    assert report["applied"] is True and len(report["changed"]) == 1
    assert _head(core, ctx, ref) == ("active", "TEST-rule")
    # Idempotent: the second pass has nothing left to move.
    assert core.requalify_claims(ctx, limit=32, dry_run=False)["changed"] == []


def test_requalify_can_also_withdraw_support(app, monkeypatch):
    """A repair that could only promote would be a ratchet, not a re-judgement."""
    core, ctx = app
    source = capture(core, ctx, "TEST-instrument 的序列号是 SN-4471。", key="TEST-requal/active")
    ref = accept(core, ctx, draft(source, "SN-4471", kind="fact", subject="TEST-instrument",
                                  predicate="序列号", statement_kind="assertion")).items[0].ref
    assert _head(core, ctx, ref)[0] == "active"

    from scope_recall.core import claims as claims_module
    from scope_recall.core.claims import Qualification

    monkeypatch.setattr(claims_module, "qualify",
                        lambda *a, **k: Qualification("proposed", "inferred_suggestion", "TEST-tightened"))
    core.requalify_claims(ctx, limit=32, dry_run=False)
    assert _head(core, ctx, ref) == ("proposed", "TEST-tightened")


@pytest.mark.parametrize("limit", [0, -1, 33, 1.0, True])
def test_requalify_refuses_an_unbounded_page(app, limit):
    core, ctx = app
    with pytest.raises(ContractError):
        core.requalify_claims(ctx, limit=limit, dry_run=True)


def test_requalify_never_undoes_a_confirmation(app, monkeypatch):
    """The gate answers "does this text prove it"; a confirmation answered
    something else, so re-reading the text must not silently withdraw it.

    Without this, every user confirmation would be reversed by the next
    maintenance pass -- the claim would go back to exactly the refusal the
    person had just overruled.
    """
    core, ctx = app
    ref = _claim(core, ctx)
    capture(core, ctx, "记住 TEST-instrument 的序列号。", key="TEST-confirm/requal")
    assert _head(core, ctx, ref) == ("active", CONFIRMED_REASON)

    report = core.requalify_claims(ctx, limit=32, dry_run=False)
    assert _head(core, ctx, ref) == ("active", CONFIRMED_REASON)
    assert any(item["ref"] == ref and item["why"].startswith("not_text_derived")
               for item in report["skipped"])


def test_requalify_never_undoes_corroboration(app):
    from scope_recall.core.corroboration import CORROBORATED_REASON

    core, ctx = app
    from dataclasses import replace

    ref = _claim(core, ctx, key="TEST-corr-requal/1")
    later = replace(ctx, session_id="TEST-session-2")
    second = capture(core, later, ELLIPTICAL, key="TEST-corr-requal/2")
    accept(core, later, draft(second, "SN-4471", kind="fact", subject="TEST-instrument",
                              predicate="序列号", statement_kind="assertion"))
    assert _head(core, ctx, ref) == ("active", CORROBORATED_REASON)

    core.requalify_claims(ctx, limit=32, dry_run=False)
    assert _head(core, ctx, ref) == ("active", CORROBORATED_REASON)
