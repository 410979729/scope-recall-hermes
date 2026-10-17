"""The recall benchmark's dev split at the CI size, in the retrieval tier, and the rules it scores by.

``recall_bench.py`` beside this file builds a synthetic shared store, asks every question the way hosts do and
scores the answers; it also runs on its own, on the held-out split and at a larger size.  Here it fails on any item
from a scope the asking entry does not read, on any metric ``baseline.json`` holds that moved further than its
tolerance to the wrong side, and on more hard negatives becoming facts than it records.  A change meant to move the
numbers records them with ``python tests/recall_bench/recall_bench.py --update``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


def _load_bench():
    spec = importlib.util.spec_from_file_location("recall_bench", HERE / "recall_bench.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench():
    return _load_bench()


@pytest.fixture(scope="module")
def measured(bench):
    # Latency is reported, never gated: its second pass is left to the standalone runs.
    return bench.run("dev", scale=1, latency=False)


def test_no_entry_recalls_another_scope(measured) -> None:
    leaked = [(q["id"], path) for q in measured["questions"] for path in ("auto", "explicit") if q[path]["leaks"]]
    assert measured["leaks"] == 0, f"items from a scope the asking entry does not read: {leaked}"
    revoked = measured["revoked_seen_before"]
    assert revoked["asked"] and revoked["seen"] == revoked["asked"], (
        f"only {revoked['seen']} of {revoked['asked']} revoked scopes showed in the packet before the revocation; "
        "for the others, not showing after proves nothing"
    )


def test_no_metric_falls_below_its_baseline(bench, measured) -> None:
    baseline = json.loads(bench.BASELINE.read_text(encoding="utf-8"))
    problems = bench.regressions(measured, baseline)
    assert not problems, (
        "\n".join(problems) + "\n\n" + bench.summary(measured) + "\n\nIf the change is meant to move these numbers: "
        "python tests/recall_bench/recall_bench.py --update (a drop also needs --allow-worse)"
    )


def test_every_hard_negative_is_proposed_as_a_careful_extractor_marks_it(bench) -> None:
    """A hypothetical and a quotation carry their statement kind, a denial keeps its negation in the value, and an
    assistant's guess and tool output come from no person: what the store then does with them is measured."""
    world = bench.build_world("dev")
    by_key = {message.key: message for message in world.messages}
    expected_kind = {"hypothetical": "hypothetical", "quoted": "quotation"}
    negations = ("not ", "n't ", "不", "讨厌")
    checked = 0
    for question in world.questions:
        for kind, key in question.negatives:
            message = by_key[key]
            for claim in world.claims.get(message.text, ()):
                checked += 1
                assert claim.statement_kind == expected_kind.get(kind, "assertion"), (kind, message.text, claim)
                if kind == "negated":
                    assert claim.value in message.text and any(word in claim.value for word in negations), claim
            if kind in {"assistant", "tool"}:
                assert message.origin != "human_direct", message
    assert checked
    hypothetical = next(m for m in world.messages if world.claims.get(m.text, ())[:1] and m.text.startswith("If "))
    source = SimpleNamespace(event={"content": hypothetical.text, "occurred_at": hypothetical.at}, ref="e", revision=1)
    proposed = json.loads(bench.ManifestConsolidation(world.claims).propose([source]))["claim_proposals"]
    assert [proposal["statement_kind"] for proposal in proposed] == ["hypothetical"]


def test_a_claim_answers_only_by_stating_the_answer(bench) -> None:
    """The gold message itself answers; a claim drawn from it answers only by asserting what it says."""
    frame = bench.Frame("fact", frozenset({"Orchard"}), "staging database port", "6543")
    key_of = {"event-gold": "dev/00001", "event-other": "dev/00002"}
    frames = {"dev/00001": (frame,)}
    gold = ("dev/00001",)

    def claim(value: str, statement_kind: str = "assertion", cites: str = "event-gold") -> dict:
        payload = {"kind": "fact", "subject": "Orchard", "predicate": "staging database port", "value_text": value}
        payload["statement_kind"] = statement_kind
        return {"kind": "claim", "ref": "claim-1", "evidence_refs": [f"{cites}@1"], "content": json.dumps(payload)}

    def event(ref: str) -> dict:
        return {"kind": "event", "ref": ref, "evidence_refs": [f"{ref}@1"], "content": "a message"}

    assert bench.credits(claim("6543"), gold, key_of, frames) == {"dev/00001"}
    assert bench.credits(claim("7012"), gold, key_of, frames) == set()
    assert bench.credits(claim("not 6543"), gold, key_of, frames) == set()
    assert bench.credits(claim("6543", "quotation"), gold, key_of, frames) == set()
    assert bench.credits(claim("6543", cites="event-other"), gold, key_of, frames) == set()
    assert bench.credits(event("event-gold"), gold, key_of, frames) == {"dev/00001"}
    assert bench.credits(event("event-other"), gold, key_of, frames) == set()


def test_ndcg_weighs_each_delivered_item_once(bench) -> None:
    both = frozenset({"a", "b"})
    assert bench.ndcg((both,), 2) == 1.0
    assert bench.ndcg((frozenset({"a"}), frozenset({"b"})), 2) == 1.0
    assert bench.ndcg((frozenset(), frozenset({"a"})), 1) == pytest.approx(1 / math.log2(3))
    # One part never delivered: the ideal ranking still holds an item for it.
    assert bench.ndcg((frozenset({"a"}),), 2) == pytest.approx(1 / (1 + 1 / math.log2(3)))
