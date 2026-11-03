import json
from dataclasses import replace
from datetime import UTC

import pytest
from scope_recall.core.recall import RetrievalPipeline
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext, SearchLimits
from scope_recall.core.retrieval_storage import RetrievalStorage
from test_claims import accept, app, capture, draft
from test_episodes import apply, artifact, ref
from v11_support import recall_request


@pytest.fixture
def versions(app, tmp_path):
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-history-states")
    one, _, _ = artifact(core, ctx, tmp_path, label="TEST-v1")
    two, _, _ = artifact(core, ctx, tmp_path, version=2, label="TEST-v2")
    source = capture(core, ctx, "TEST 那个颜色是TEST-v1。")
    proposal = dict(
        mention="那个颜色",
        candidate_refs=[ref(one), ref(two)],
        resolved_ref=ref(one),
        resolution="resolved",
        evidence_refs=[ref(source)],
    )
    initial = apply(core, ctx, references=[proposal]).items[0]
    assert core.records.reference(ctx, initial.ref, 1).payload["resolution"] == "resolved"
    clarification = capture(core, ctx, "刚才“那个颜色”说的是TEST-v2。")
    final = apply(core, ctx, references=[dict(proposal, evidence_refs=[ref(clarification)])]).items[0]
    assert initial.ref == final.ref and final.revision == 2
    (episode,) = core.episodes(ctx)
    return (
        core,
        ctx,
        {
            "artifact": (one.ref, two.revision),
            "episode": (episode.ref, episode.revision),
            "reference": (initial.ref, final.revision),
        },
    )


@pytest.mark.parametrize("kind", ["artifact", "episode", "reference"])
@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_old_object_version_never_claims_to_be_the_current_head(versions, kind, mode):
    core, ctx, objects = versions
    identity, head = objects[kind]
    assert head > 1
    as_of = "2026-09-07T12:00:00Z" if mode == "as_of" else None
    search = SearchContext(
        "TEST", mode, as_of, (f"{identity}@1",), SearchLimits(), core.clock.monotonic() + 10, core.clock.utc_now(), ctx
    )
    with core.storage.read(ctx) as tx:
        old = RetrievalStorage().hydrate(tx, CandidateRef(kind, identity, 1, "exact_ref"), search)
        current = RetrievalStorage().hydrate(tx, CandidateRef(kind, identity, head, "exact_ref"), search)
    assert old is not None and old.temporal_status == "historical"
    assert current is not None and current.temporal_status == "current"
    packet = core.recall_packet(
        ctx,
        recall_request(
            query="TEST",
            mode=mode,
            **({"as_of": as_of} if as_of else {}),
            focus_refs=[f"{identity}@1", f"{identity}@{head}"],
            max_items=16,
            budget_tokens=4000,
        ),
        deadline_seconds=10,
    )
    states = {(item["ref"], item["revision"]): item["temporal_status"] for item in packet["items"]}
    assert states[(identity, 1)] == "historical"
    assert states[(identity, head)] == "current"


#: When the old value is said, and when the value that replaces it is.
_SAID, _CHANGED = "2026-09-01T12:00:00Z", "2026-09-03T12:00:00Z"
#: The old message holds every word of the question; the one that changed it holds fewer.
_QUESTION = "TEST-project 的 CI 服务是什么？"


def _fact(core, ctx, text, value, when, *, origin="human_direct", **changes):
    """The fact about TEST-project's CI service that ``text``, said at ``when``, states; and its message."""
    source = capture(core, ctx, text, when=when, origin=origin)
    claim = dict(kind="fact", predicate="CI 服务", statement_kind="assertion", **changes)
    return accept(core, ctx, draft(source, value, **claim)).items[0], source


@pytest.fixture
def replaced(app):
    core, ctx = app
    old, said = _fact(core, ctx, "TEST-project 的 CI 服务是 Jenkins。", "Jenkins", _SAID)
    new, changed = _fact(core, ctx, "TEST-project 的 CI 服务换成 GitHub Actions 了。", "GitHub Actions", _CHANGED)
    assert (new.ref, new.state, new.disposition) == (old.ref, "active", "inserted")
    return core, ctx, old, said, new, changed


def _keys(objects):
    return [(obj.kind, obj.ref, obj.revision) for obj in objects]


def _item_keys(packet):
    return [(item["kind"], item["ref"], item["revision"]) for item in packet["items"]]


@pytest.mark.parametrize("mode", ["current", "auto"])
def test_current_recall_withholds_the_message_whose_value_was_replaced(replaced, mode):
    core, ctx, _old, said, new, changed = replaced
    result = core.recall(ctx, recall_request(query=_QUESTION, mode=mode), deadline_seconds=30)
    keys = _keys(result.items)
    current = [keys.index(key) for key in (("claim", new.ref, new.revision), ("event", changed.ref, 1)) if key in keys]
    assert current and ("event", said.ref, 1) not in keys
    status = {key: item.temporal_status for key, item in zip(keys, result.items, strict=True)}
    assert status[("event", changed.ref, 1)] == "current"
    assert "superseded_evidence_withheld:1" in result.gaps


def test_a_packet_too_small_for_both_leads_with_the_value_that_holds(replaced):
    core, ctx, _old, said, new, changed = replaced
    packet = core.recall_packet(
        ctx,
        recall_request(query=_QUESTION, mode="current", max_items=2),
        deadline_seconds=30,
        background_without_evidence=False,
    )
    keys = _item_keys(packet)
    assert keys[0] in {("claim", new.ref, new.revision), ("event", changed.ref, 1)}
    assert ("event", said.ref, 1) not in keys
    assert keys[1] in {("claim", new.ref, new.revision), ("event", changed.ref, 1)}


def test_current_focus_is_not_a_historical_inspection_bypass(replaced):
    core, ctx, _old, said, _new, _changed = replaced
    request = recall_request(query=_QUESTION, mode="current", focus_refs=[f"{said.ref}@1"])
    result = core.recall(ctx, request, deadline_seconds=30)
    assert ("event", said.ref, 1) not in _keys(result.items)
    assert "superseded_evidence_withheld:1" in result.gaps
    assert core.inspect_object(ctx, said.ref, 1).value.event["content"] == said.event["content"]


@pytest.mark.parametrize("mode", ["history", "as_of"])
def test_a_read_of_the_past_still_finds_the_value_said_then(replaced, mode):
    core, ctx, old, said, _new, changed = replaced
    as_of = {"as_of": "2026-09-02T12:00:00Z"} if mode == "as_of" else {}
    packet = core.recall_packet(
        ctx,
        recall_request(query=_QUESTION, mode=mode, max_items=10, budget_tokens=4000, **as_of),
        deadline_seconds=30,
        background_without_evidence=False,
    )
    items = dict(zip(_item_keys(packet), packet["items"], strict=True))
    assert items[("event", said.ref, 1)]["temporal_status"] == "current"
    if as_of:
        assert ("event", changed.ref, 1) not in items
        assert json.loads(items[("claim", old.ref, 1)]["content"])["value_text"] == "Jenkins"
        assert core.current_claim(ctx, old.ref, as_of=as_of["as_of"]).payload["value_text"] == "Jenkins"


def test_a_day_in_current_query_does_not_override_the_evidence_lifecycle(replaced):
    core, ctx, _old, said, _new, _changed = replaced
    question = f"9月1日 {_QUESTION}"
    result = core.recall(ctx, recall_request(query=question, mode="current"), deadline_seconds=30, zone=UTC)
    assert ("event", said.ref, 1) not in _keys(result.items)
    assert "superseded_evidence_withheld:1" in result.gaps


def test_before_wording_requires_an_explicit_historical_mode_for_old_evidence(replaced):
    core, ctx, _old, said, _new, _changed = replaced
    question = "TEST-project 以前用的 CI 服务是什么？"
    result = core.recall(ctx, recall_request(query=question, mode="current"), deadline_seconds=30)
    assert ("event", said.ref, 1) not in _keys(result.items)
    assert "superseded_evidence_withheld:1" in result.gaps


def _message(ref, *replaced_by):
    """A ranked message and its candidate, replaced by the messages ``replaced_by`` names."""
    metadata = (("replaced_by", json.dumps([["event", other, 1] for other in replaced_by])),) if replaced_by else ()
    status = "historical" if replaced_by else "current"
    obj = RetrievedObject(
        ref,
        1,
        "event",
        f"TEST {ref}",
        "human_direct",
        status,
        "TEST",
        (f"{ref}@1",),
        "direct_report",
        True,
        (),
        (),
        metadata,
    )
    return CandidateRef("event", ref, 1, "lexical"), obj


def _reordered(app, query, *ranked):
    core, ctx = app
    search = SearchContext(
        query, "current", None, (), SearchLimits(), core.clock.monotonic() + 10, core.clock.utc_now(), ctx
    )
    return [candidate.ref for candidate, _obj in RetrievalPipeline._behind_replacements(list(ranked), search)]


@pytest.mark.parametrize(
    ("query", "order"),
    [
        ("Which CI service does TEST-project use?", ["TEST-new", "TEST-old"]),
        ("Which CI service did TEST-project use before?", ["TEST-old", "TEST-new"]),
        ("TEST-project 以前用的 CI 服务是什么？", ["TEST-old", "TEST-new"]),
        ("TEST-project 之前说的 CI 服务是什么？", ["TEST-new", "TEST-old"]),
    ],
)
def test_only_a_question_about_now_moves_a_replaced_message_behind_its_replacement(app, query, order):
    assert _reordered(app, query, _message("TEST-old", "TEST-new"), _message("TEST-new")) == order


def test_a_question_about_what_was_said_before_still_leads_with_the_value_that_holds(replaced):
    core, ctx, _old, said, new, changed = replaced
    question = "TEST-project 之前说的 CI 服务是什么？"
    result = core.recall(ctx, recall_request(query=question, mode="current"), deadline_seconds=30)
    keys = _keys(result.items)
    current = [keys.index(key) for key in (("claim", new.ref, new.revision), ("event", changed.ref, 1)) if key in keys]
    assert current and ("event", said.ref, 1) not in keys


@pytest.mark.parametrize(
    ("ranked", "order"),
    [
        (("old1>new", "old2>new", "new"), ["new", "old1", "old2"]),
        (("old1>new", "old2>new", "old3>new", "new"), ["new", "old1", "old2", "old3"]),
        (("old1>new", "other", "old2>new", "new"), ["other", "new", "old1", "old2"]),
        (("old1>second", "old2>first", "first", "second"), ["first", "old2", "second", "old1"]),
        (("first", "old1>first", "old2>first"), ["first", "old1", "old2"]),
    ],
)
def test_messages_moved_behind_one_item_keep_the_order_they_were_ranked_in(app, ranked, order):
    """Messages sharing one replacement retain their relative order before packet truncation."""
    pairs = [_message(f"TEST-{name}", *(f"TEST-{by}" for by in rest)) for name, *rest in (n.split(">") for n in ranked)]
    assert _reordered(app, "Which CI service does TEST-project use?", *pairs) == [f"TEST-{name}" for name in order]


def test_small_current_packet_never_spends_slots_on_replaced_evidence(app, monkeypatch):
    core, ctx = app
    _first, said_first = _fact(core, ctx, "TEST-project 的 CI 服务是 Jenkins。", "Jenkins", _SAID)
    _second, said_second = _fact(core, ctx, "TEST-project 的 CI 服务是 CircleCI。", "CircleCI", "2026-09-02T12:00:00Z")
    new, changed = _fact(core, ctx, "TEST-project 的 CI 服务换成 GitHub Actions 了。", "GitHub Actions", _CHANGED)
    with monkeypatch.context() as unordered:
        # The order the ranker gave, before a replaced message moves.
        unordered.setattr(RetrievalPipeline, "_behind_replacements", staticmethod(lambda ranked, _working: ranked))
        ranked = _keys(core.recall(ctx, recall_request(query=_QUESTION, mode="current"), deadline_seconds=30).items)
    old = [key for key in ranked if key in {("event", said_first.ref, 1), ("event", said_second.ref, 1)}]
    packet = core.recall_packet(
        ctx,
        recall_request(query=_QUESTION, mode="current", max_items=2),
        deadline_seconds=30,
        background_without_evidence=False,
    )
    keys = _item_keys(packet)
    assert keys[0] in {("claim", new.ref, new.revision), ("event", changed.ref, 1)}
    assert old == []
    assert ("event", said_first.ref, 1) not in keys and ("event", said_second.ref, 1) not in keys
    assert "superseded_evidence_withheld:2" in packet["gaps"]


def test_a_message_with_a_value_that_still_holds_is_not_replaced(app):
    core, ctx = app
    said = capture(core, ctx, "TEST-project 的 CI 服务是 Jenkins，TEST-project 的部署区域是 us-east-1。", when=_SAID)
    claims = [dict(kind="fact", statement_kind="assertion", predicate=p) for p in ("CI 服务", "部署区域")]
    stated = accept(core, ctx, draft(said, "Jenkins", **claims[0]), draft(said, "us-east-1", **claims[1])).items
    assert len({item.ref for item in stated}) == 2 and {item.state for item in stated} == {"active"}
    _fact(core, ctx, "TEST-project 的 CI 服务换成 GitHub Actions 了。", "GitHub Actions", _CHANGED)
    result = core.recall(ctx, recall_request(query=_QUESTION, mode="current"), deadline_seconds=30)
    status = {key: item.temporal_status for key, item in zip(_keys(result.items), result.items, strict=True)}
    assert status[("event", said.ref, 1)] == "current", "its deployment region still holds"


def test_unavailable_replacement_evidence_does_not_revive_an_obsolete_value(app):
    """A missing newer source supplies no answer and cannot restore the old assertion."""
    core, ctx = app
    _old, said = _fact(core, ctx, "TEST-project 的 CI 服务是 Jenkins。", "Jenkins", _SAID)
    moved = capture(core, ctx, "TEST-project 的 CI 服务换成 GitHub Actions 了。", key="TEST-moved", when=_CHANGED)
    claim = dict(kind="fact", predicate="CI 服务", statement_kind="assertion")
    assert accept(core, ctx, draft(moved, "GitHub Actions", **claim)).items[0].state == "active"
    capture(core, ctx, "TEST-project 的 CI 服务。", key="TEST-moved", revision=2, when=_CHANGED)
    result = core.recall(ctx, recall_request(query=_QUESTION, mode="current"), deadline_seconds=30)
    status = {key: item.temporal_status for key, item in zip(_keys(result.items), result.items, strict=True)}
    assert ("event", said.ref, 1) not in status
    assert "superseded_evidence_withheld:1" in result.gaps


def test_a_later_statement_the_store_did_not_take_is_not_replaced_by_the_older_value(app):
    core, ctx = app
    _held, said = _fact(core, ctx, "TEST-project 的 CI 服务是 Jenkins。", "Jenkins", _SAID)
    guess, guessed = _fact(
        core,
        ctx,
        "TEST-project 的 CI 服务应该是 GitHub Actions 吧。",
        "GitHub Actions",
        _CHANGED,
        origin="assistant_visible",
    )
    assert guess.state == "proposed"
    result = core.recall(ctx, recall_request(query=_QUESTION, mode="current"), deadline_seconds=30)
    status = {key: item.temporal_status for key, item in zip(_keys(result.items), result.items, strict=True)}
    assert status[("event", said.ref, 1)] == "current", "Jenkins still holds"
    assert status[("event", guessed.ref, 1)] == "current", "said after Jenkins, it is no value Jenkins replaced"
