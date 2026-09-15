"""P12 regression: automatic recall must not let an old query echo crowd out facts."""
from __future__ import annotations

from dataclasses import replace
import itertools

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p12-query-echo"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _packet(core: MemoryCore, ctx, **changes):
    return core.recall_packet(ctx, recall_request(**changes), deadline_seconds=5)


def _contents(packet):
    return {item["content"] for item in packet["items"]}


def test_auto_query_echoes_do_not_crowd_out_real_fact_but_explicit_modes_keep_them(app):
    core, ctx = app
    # Keep lexical retrieval deterministic: all echoes are newer than the fact,
    # and the long single identifier remains one admissible query term.
    query = "P12-echo-" + ("x" * 220)
    normalized_echo = f"\u3000{query.replace('-', '－')}\u3000"
    for index in range(1, 4):
        capture(
            core,
            ctx,
            normalized_echo if index == 3 else query,
            key=f"TEST-p12/ack/{index}",
            when="2026-09-01T12:00:00Z",
        )
    fact_text = f"{query} fact: staged in vault-sim-42; backup window is 03:20."
    capture(core, ctx, fact_text, key="TEST-p12/fact", when="2026-08-01T12:00:00Z")

    auto = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-auto")
    assert fact_text in _contents(auto), auto
    assert query not in _contents(auto)
    assert normalized_echo not in _contents(auto)

    for mode in ("current", "history"):
        explicit = _packet(
            core,
            ctx,
            query=query,
            mode=mode,
            budget_tokens=8000,
            request_id=f"TEST-p12-{mode}",
        )
        assert query in _contents(explicit), (mode, explicit)


@pytest.mark.parametrize(
    "content",
    (
        "P12q-route question: should the archive remain pending?",
        "P12q-route was not approved; keep the archive pending.",
        "P12q-route only after the vault check passes may the archive proceed.",
    ),
)
def test_auto_does_not_broadly_filter_question_negative_or_conditional_facts(app, content):
    core, ctx = app
    capture(core, ctx, content, key="TEST-p12/semantic-variant")

    packet = _packet(core, ctx, query="P12q-route", mode="auto", request_id="TEST-p12-variant")

    assert content in _contents(packet), packet


def test_auto_followup_keeps_original_query_filter(app):
    core, ctx = app
    query = "why P12-followup"
    capture(core, ctx, query, key="TEST-p12/followup-echo", when="2026-09-01T12:00:00Z")
    neutral = "P12-followup why status is pending; no cause recorded."
    capture(core, ctx, neutral, key="TEST-p12/followup-neutral", when="2026-08-01T12:00:00Z")

    packet = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-followup")

    # The why query leaves a bounded reason need and exercises the directed
    # followup round; its internal query must not redefine the original echo.
    assert query not in _contents(packet)
    assert neutral in _contents(packet), packet
