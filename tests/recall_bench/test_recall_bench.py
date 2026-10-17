"""The recall benchmark's dev split at the CI size, in the retrieval tier, and the rules it scores by.

``recall_bench.py`` beside this file builds a synthetic shared store, asks every question the way hosts do and
scores the answers; it also runs on its own, on the held-out split and at a larger size.  Here it fails on any item
from a scope the asking entry does not read, on any metric ``baseline.json`` holds that moved further than its
tolerance to the wrong side, and on more active claims standing on hard negatives than it records.  A change meant to
move the numbers records them with ``python tests/recall_bench/recall_bench.py --update``.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
GOLD = "dev/00001"


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


def test_a_run_with_a_leak_is_never_recorded(bench) -> None:
    before = bench.BASELINE.read_bytes()
    assert bench.update_baseline({"leaks": 1, "world": "w"}, None, [], allow_worse=True) == 1
    assert bench.BASELINE.read_bytes() == before


def test_a_hard_link_to_the_baseline_is_the_baseline(bench, tmp_path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    link = tmp_path / "report.json"
    os.link(baseline, link)
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    assert bench.is_baseline(link, baseline) and bench.is_baseline(baseline, baseline)
    assert not bench.is_baseline(other, baseline) and not bench.is_baseline(tmp_path / "new.json", baseline)


def test_every_hard_negative_is_proposed_as_a_careful_extractor_marks_it(bench) -> None:
    """Each hypothetical and quotation is proposed with its statement kind, each denial with its negation in the
    value, and each assistant's guess and tool output as a statement that comes from no person: what the store then
    makes of them is what is measured.  A list's parts are proposed as no claim, so neither is its hypothetical."""
    world = bench.build_world("dev")
    by_key = {message.key: message for message in world.messages}
    kinds = {"hypothetical": "hypothetical", "quoted": "quotation"}
    for question in world.questions:
        for kind, key in question.negatives:
            if kind not in bench.NEVER_FACTS:
                continue
            message = by_key[key]
            claims = world.claims.get(message.text, ())
            if question.task == "aggregation":
                assert not claims and not any(world.claims.get(by_key[part].text) for part in question.gold), key
                continue
            assert claims, f"{kind} {key} was proposed with no claim: {message.text}"
            for claim in claims:
                assert claim.statement_kind == kinds.get(kind, "assertion"), (kind, message.text, claim)
                if kind == "negated":
                    assert claim.value in message.text, (message.text, claim)
                    assert any(word in claim.value for word in ("not ", "n't ", "不", "讨厌")), (message.text, claim)
            if kind in {"assistant", "tool"}:
                assert message.origin != "human_direct", message
    hypothetical = next(by_key[k] for q in world.questions for kind, k in q.negatives if kind == "hypothetical")
    source = SimpleNamespace(event={"content": hypothetical.text, "occurred_at": hypothetical.at}, ref="e", revision=1)
    proposed = json.loads(bench.ManifestConsolidation(world.claims).propose([source]))["claim_proposals"]
    assert [proposal["statement_kind"] for proposal in proposed] == ["hypothetical"]


def _claim(value: str, *, statement_kind: str = "assertion", subject: str = "Orchard", cites: str = "event-gold"):
    payload = {"kind": "fact", "subject": subject, "predicate": "staging database port", "value_text": value}
    payload["statement_kind"] = statement_kind
    return {"kind": "claim", "ref": "claim-1", "evidence_refs": [f"{cites}@1"], "content": json.dumps(payload)}


def _event(ref: str, content: str = "a message") -> dict:
    return {"kind": "event", "ref": ref, "evidence_refs": [f"{ref}@1"], "content": content}


def test_a_claim_answers_only_by_stating_the_answer(bench) -> None:
    """The gold message itself answers; a claim drawn from it answers only by asserting what it says."""
    key_of = {"event-gold": GOLD, "event-other": "dev/00002", "event-quote": "dev/00003"}
    frames = {GOLD: (bench.Frame("fact", frozenset({"Orchard"}), "staging database port", "6543"),)}

    def credited(item) -> set[str]:
        return set(bench.credits(item, (GOLD,), key_of, frames))

    assert credited(_claim("6543")) == {GOLD}
    assert credited(_claim("7012")) == set()
    assert credited(_claim("not 6543")) == set()
    assert credited(_claim("6543", statement_kind="quotation")) == set()
    assert credited(_claim("6543", cites="event-other")) == set()
    assert credited(_event("event-gold")) == {GOLD}
    assert credited(_event("event-other")) == set()
    # Another message that quotes the answer's words is not the answer.
    assert credited(_event("event-quote", "The Orchard staging database port is 6543.")) == set()


def test_a_claim_about_the_person_answers_only_when_bound_to_its_owner(bench) -> None:
    owner = "principal:human:mira"
    frames = {GOLD: (bench.Frame("fact", frozenset({"user", owner}), "staging database port", "6543"),)}
    key_of = {"event-gold": GOLD}
    assert bench.credits(_claim("6543", subject=owner), (GOLD,), key_of, frames) == {GOLD}
    assert bench.credits(_claim("6543", subject="principal:human:siyuan"), (GOLD,), key_of, frames) == set()


def test_an_item_held_in_another_scope_answers_nothing_and_is_a_leak(bench) -> None:
    """A claim held in a scope the entry does not read, though it cites the gold message the entry does read."""
    question = bench.Question("q", "single_fact", "attribute", "en", "desk", frozenset({"mira-home"}), "?", (GOLD,))

    def outcome(scope: str):
        answer = bench.Answer(
            packet={"answerability": "supported", "items": [_claim("6543")]},
            credited=(frozenset({GOLD}),),
            carried=(frozenset({GOLD}),),
            scopes=(scope,),
            channels=("claim_lexical",),
            tokens=10,
            ms=1.0,
        )
        return bench.outcome(question, answer, {GOLD: "mira-home"})

    held, elsewhere = outcome("mira-home"), outcome("siyuan-home")
    assert (held.first, held.leaks) == (1, 0)
    assert (elsewhere.first, elsewhere.leaks) == (None, 1)


def test_every_active_claim_on_a_hard_negative_counts_once_whatever_its_slot(bench) -> None:
    """The ceiling reads every claim version the store holds, with no list of slots and no cap: each active claim
    citing a hard negative counts once, however many of its versions cite it, and a claim not active does not."""
    negated = "dev/00002"
    question = bench.Question(
        "q", "single_fact", "attribute", "en", "desk", frozenset({"mira-home"}), "?", (GOLD,), (("negated", negated),)
    )

    def version(ref: str, revision: int = 1, state: str = "active", cites: str = "event-negated"):
        return SimpleNamespace(ref=ref, revision=revision, metadata={"state": state}, evidence_refs=(f"{cites}@1",))

    # More claims than the 200 a listing of one slot returned.
    versions = [version(f"claim-{n}") for n in range(250)]
    versions += [version("claim-0", revision=2), version("claim-lead", state="proposed"), version("g", cites="gold")]
    store = SimpleNamespace(
        world=SimpleNamespace(questions=(question,)),
        key_of={"event-negated": negated, "gold": GOLD},
        _claim_versions=lambda: iter(versions),
    )
    assert bench.BenchStore.active_claims_on(store) == {"answer": 1, "negated": 250}


def test_ndcg_weighs_each_delivered_item_once(bench) -> None:
    both = frozenset({"a", "b"})
    assert bench.ndcg((both,), 2) == 1.0
    assert bench.ndcg((frozenset({"a"}), frozenset({"b"})), 2) == 1.0
    assert bench.ndcg((frozenset(), frozenset({"a"})), 1) == pytest.approx(1 / math.log2(3))
    # One part never delivered: the ideal ranking still holds an item for it.
    assert bench.ndcg((frozenset({"a"}),), 2) == pytest.approx(1 / (1 + 1 / math.log2(3)))


def test_a_slow_machine_scores_what_a_fast_one_does(bench, monkeypatch) -> None:
    """The store's waits for its lease, the worker's passes and every recall read the bench's clock: a machine so
    slow that a minute of real time passes between two readings builds the same store and gets the same packets."""
    world = bench.build_world("dev")
    messages = world.messages[:240]
    kept = {message.key for message in messages}
    small = replace(world, messages=messages, questions=tuple(q for q in world.questions if set(q.gold) <= kept)[:12])
    assert small.questions
    steady = _scored(bench, small)
    readings = itertools.count()
    monkeypatch.setattr(time, "monotonic", lambda: next(readings) * 60.0)
    assert _scored(bench, small) == steady


def _scored(bench, world):
    root = Path(tempfile.mkdtemp(prefix="scope-recall-bench-clock-"))
    try:
        store = bench.BenchStore(world, root)
        try:
            packets = [
                [(item["ref"], item["revision"]) for item in store.ask(question, path).packet["items"]]
                for question in world.questions
                for path in bench.PATHS
            ]
            status = store.core.status(store.worker)
            return packets, status.pending_work, status.failed_work, store.qualification(), store.active_claims_on()
        finally:
            store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
