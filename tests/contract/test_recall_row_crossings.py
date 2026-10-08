"""A read transaction keeps what it loaded, and hydration loads its candidates' sources, visibility and versions together:
the same answers, in a count of statements and rows that does not grow with the evidence (3.7.7).

A recall loaded each evidence source up to five times, three statements each: 16,222 statements and 7,087 rows for one of
yuheng's questions on a copy of the shared store.  In a busy Hermes gateway every statement and every row waited for the
GIL (``test_capture_row_crossings``).  Now 469 statements and 755 rows.
"""

import json
import sqlite3
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import storage as storage_module
from scope_recall.core.storage import SQLiteStorage, Transaction
from scope_recall.core.visibility import allowed
from test_capture_row_crossings import Crossings
from test_shared_store import NOW, put, shared, shared_context  # noqa: F401  (fixture)
from test_v11_claims import accept, app, capture, draft, initial, revise_request  # noqa: F401  (fixtures)
from v11_support import recall_request


def _pairs(sources):
    return [(source.ref, source.revision) for source in sources]


def _sources(core, ctx, count: int):
    return [capture(core, ctx, f"TEST 第{index}条证据 ident{index:04d}。") for index in range(count)]


def _shapes(core, ctx):
    """A plain source, a part of a message whose other part never came, a part of a whole one, and a version a newer
    one replaced."""
    plain = capture(core, ctx, "TEST 普通的一条。")
    part = capture(
        core,
        ctx,
        "TEST 只有前一半",
        key="TEST-part1",
        segment=dict(group_key="TEST-full-source", index=0, total=2, truncated=False),
    )
    whole = capture(
        core,
        ctx,
        "TEST 完整的前一半",
        key="TEST-whole1",
        segment=dict(group_key="TEST-whole-source", index=0, total=2, truncated=False),
    )
    capture(
        core,
        ctx,
        "TEST 完整的后一半",
        key="TEST-whole2",
        segment=dict(group_key="TEST-whole-source", index=1, total=2, truncated=False),
    )
    older = capture(core, ctx, "TEST 旧版本", key="TEST-revised")
    capture(core, ctx, "TEST 新版本", key="TEST-revised", revision=2)
    return [plain, part, whole, older]


def test_loaded_together_a_source_reads_exactly_as_loaded_alone(app):
    core, ctx = app
    shapes = _shapes(core, ctx)
    assert "source_segments_incomplete" in shapes[1].capture_gaps
    with core.storage.read(ctx) as tx:
        assert "source_segments_incomplete" not in tx.source(shapes[2].ref, shapes[2].revision).capture_gaps
    with core.storage.read(ctx) as alone:
        expected = [alone.source(*pair) for pair in _pairs(shapes)]
        heads = []
        for pair in _pairs(shapes):
            try:
                alone.claims.require_live_source(*pair)
                heads.append(True)
            except ContractError as exc:
                heads.append(exc.code)
    with core.storage.read(ctx) as together:
        together.prefetch_sources(_pairs(shapes))
        assert [together.source(*pair) for pair in _pairs(shapes)] == expected
        got = []
        for pair in _pairs(shapes):
            try:
                together.claims.require_live_source(*pair)
                got.append(True)
            except ContractError as exc:
                got.append(exc.code)
    assert got == heads == [True, True, True, "VERSION_CONFLICT"]


def test_a_prefetched_source_is_read_again_without_a_statement(app, monkeypatch):
    core, ctx = app
    sources = _sources(core, ctx, 30)
    crossings = Crossings(monkeypatch)
    with core.storage.read(ctx) as tx:
        loading = crossings.during(lambda: tx.prefetch_sources(_pairs(sources)))
        again = crossings.during(
            lambda: [
                (tx.source(*pair), tx.claims.require_live_source(*pair), tx.source(*pair)) for pair in _pairs(sources)
            ]
        )
    assert loading[0] <= 3 and loading[1] <= 3, loading
    assert again == (0, 0)


def test_every_read_gets_a_source_of_its_own(app):
    core, ctx = app
    source = capture(core, ctx, "TEST 一条。")
    with core.storage.read(ctx) as tx:
        first, second = tx.source(source.ref, source.revision), tx.source(source.ref, source.revision)
        first.event["content"] = "TEST changed by a reader"
        assert first.event is not second.event and second.event["content"] == "TEST 一条。"
        assert tx.source(source.ref, source.revision).event["content"] == "TEST 一条。"


def test_a_write_transaction_reads_what_it_wrote(app):
    core, ctx = app
    source = capture(core, ctx, "TEST 一条。")
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        assert tx.source(source.ref, source.revision) is not None and allowed(tx, "claim", "claim-TEST-absent")
        conn.execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (source.ref,))
        conn.execute(
            """INSERT INTO restored_absence_blocks VALUES ('claim','claim-TEST-absent','TEST-scope',NULL,NULL,?,
                        'TEST')""",
            ("0" * 64,),
        )
        assert tx.source(source.ref, source.revision) is None and not allowed(tx, "claim", "claim-TEST-absent")
        tx.prefetch_sources([(source.ref, source.revision)])
        assert not tx.remembers and not tx.knows(("source", source.ref, source.revision))


def test_versions_loaded_together_read_as_loaded_alone(app, monkeypatch):
    core, ctx = app
    item, _source = initial(core, ctx)
    correction = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    assert core.revise(ctx, revise_request(item, correction), remaining_seconds=10).items[0].revision == 2
    shell = capture(core, ctx, "TEST-project 外壳 银色。")
    other = accept(core, ctx, draft(shell, "银色", predicate="外壳")).items[0]
    refs = [item.ref, other.ref, "claim-TEST-missing"]
    assert item.ref != other.ref
    with core.storage.read(ctx) as alone:
        expected = [alone.claims.versions(ref) for ref in refs]
    assert len(expected[0]) == 2 and expected[1] and expected[2] == ()
    crossings = Crossings(monkeypatch)
    with core.storage.read(ctx) as together:
        loading = crossings.during(lambda: together.claims.prefetch_versions(refs))
        got: list = []
        again = crossings.during(lambda: got.extend(together.claims.versions(ref) for ref in refs))
    assert got == expected and again == (0, 0) and loading[0] <= 4, (loading, again)


def test_past_its_limits_a_read_transaction_answers_alike_and_keeps_no_more(app, monkeypatch):
    core, ctx = app
    sources = _sources(core, ctx, 6)
    with core.storage.read(ctx) as alone:
        expected = [alone.source(*pair) for pair in _pairs(sources)]
    monkeypatch.setattr(storage_module, "_MEMO_ENTRIES", 4)
    with core.storage.read(ctx) as tx:
        tx.prefetch_sources(_pairs(sources))
        assert [tx.source(*pair) for pair in _pairs(sources)] == expected
        assert (
            sum(
                tx.knows(key)
                for pair in _pairs(sources)
                for key in (("source", *pair), ("allowed", "event", pair[0], False))
            )
            <= 4
        )
    monkeypatch.setattr(storage_module, "_MEMO_ENTRIES", 16384)
    monkeypatch.setattr(storage_module, "_MEMO_BYTES", 10)
    with core.storage.read(ctx) as tx:
        tx.prefetch_sources(_pairs(sources))
        assert [tx.source(*pair) for pair in _pairs(sources)] == expected
        assert not any(tx.knows(("source", *pair)) for pair in _pairs(sources))


def _packet(core, ctx, query):
    packet = core.recall_packet(
        ctx, recall_request(query=query, request_id="TEST-same"), current_source_refs=(), deadline_seconds=30.0
    )
    # A recall's diagnostic ref is its own whatever it found.
    return {key: value for key, value in packet.items() if key != "diagnostic_ref"}


def test_a_recall_answers_alike_whether_or_not_it_keeps_what_it_loads(app, monkeypatch):
    core, ctx = app
    item, source = initial(core, ctx)
    sources = _sources(core, ctx, 12)
    accept(
        core,
        ctx,
        draft(
            sources[0],
            "银色",
            subject="TEST-project",
            predicate="外壳",
            evidence_spans=[
                dict(source_ref=s.ref, source_revision=s.revision, quote=s.event["content"]) for s in sources[:8]
            ],
        ),
    )
    queries = ("TEST-project 配色", "TEST 第3条证据", "ident0005", "外壳 银色")
    kept = [_packet(core, ctx, query) for query in queries]
    assert any(packet.get("items") for packet in kept)
    monkeypatch.setattr(Transaction, "remember", lambda self, key, value, size=0: None)
    monkeypatch.setattr(Transaction, "prefetch_sources", lambda self, pairs: None)
    assert [_packet(core, ctx, query) for query in queries] == kept


def _supported_claim(core, ctx, said_in, count: int, predicate: str, value: str):
    """An active claim with ``count`` sources, each saying it, said in another session."""
    sources = [capture(core, said_in, f"TEST-project {predicate} {value}，第{index}次。") for index in range(count)]
    spans = [dict(source_ref=s.ref, source_revision=s.revision, quote=s.event["content"]) for s in sources]
    item = accept(
        core, ctx, draft(sources[0], value, subject="TEST-project", predicate=predicate, evidence_spans=spans)
    ).items[0]
    assert item.state == "active"
    return item


def test_hydrating_a_claim_does_not_grow_with_its_evidence(app, monkeypatch):
    """A claim's evidence was loaded one source at a time, three statements each, five times over (``_deliverable``,
    ``_source_live``, its origins, contexts and entries)."""
    from scope_recall.core.retrieval import CandidateRef, SearchContext

    core, ctx = app
    earlier = replace(ctx, session_id="TEST-earlier-session")
    small = _supported_claim(core, ctx, earlier, 2, "外壳", "银色")
    large = _supported_claim(core, ctx, earlier, 16, "底座", "金色")
    reader = core.recall_pipeline.storage_reader
    search = SearchContext.from_request(
        recall_request(query="TEST-project"),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 30,
        current_source_refs=(),
        background_without_evidence=True,
    )

    def hydrate(item):
        with core.storage.read(ctx) as tx:
            obj = reader.hydrate(tx, CandidateRef("claim", item.ref, item.revision, "claim_lexical"), search)
            assert obj is not None and len(obj.evidence_refs) in {2, 16}

    crossings = Crossings(monkeypatch)
    few = crossings.during(lambda: hydrate(small))
    lots = crossings.during(lambda: hydrate(large))
    assert lots[0] - few[0] <= 4 and lots[1] - few[1] <= 4, (few, lots)


# --- what is loaded together keeps the reader's audience (review of 3.7.7) -------------------------------------------


def _both(core, ctx, pairs):
    with core.storage.read(ctx) as alone:
        expected = [alone.source(*pair) for pair in pairs]
    with core.storage.read(ctx) as together:
        together.prefetch_sources(pairs)
        got = [together.source(*pair) for pair in pairs]
    return expected, got


def test_a_source_of_another_project_stays_out_of_reach_loaded_together(app):
    core, ctx = app
    mine = capture(core, ctx, "TEST 本项目的一条。")
    theirs = capture(core, replace(ctx, project_id="TEST-other"), "TEST 别的项目的一条。")
    expected, got = _both(core, ctx, [(mine.ref, mine.revision), (theirs.ref, theirs.revision)])
    assert expected[1] is None and got == expected


def test_a_source_blocked_from_reading_stays_out_of_reach_loaded_together(app):
    core, ctx = app
    plain = capture(core, ctx, "TEST 一条。")
    blocked = capture(core, ctx, "TEST 另一条。")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (blocked.ref,))
    expected, got = _both(core, ctx, [(plain.ref, plain.revision), (blocked.ref, blocked.revision)])
    assert expected[1] is None and got == expected


def test_a_part_whose_other_part_was_blocked_reads_incomplete_loaded_together(app):
    core, ctx = app
    first = capture(
        core,
        ctx,
        "TEST 完整的前一半",
        key="TEST-whole1",
        segment=dict(group_key="TEST-whole-source", index=0, total=2, truncated=False),
    )
    second = capture(
        core,
        ctx,
        "TEST 完整的后一半",
        key="TEST-whole2",
        segment=dict(group_key="TEST-whole-source", index=1, total=2, truncated=False),
    )
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (second.ref,))
    expected, got = _both(core, ctx, [(first.ref, first.revision)])
    assert "source_segments_incomplete" in expected[0].capture_gaps and got == expected


def test_a_claim_of_another_project_stays_out_of_reach_loaded_together(app):
    core, ctx = app
    other = replace(ctx, project_id="TEST-other")
    source = capture(core, other, "TEST-project 外壳 银色。")
    item = accept(core, other, draft(source, "银色", predicate="外壳")).items[0]
    with core.storage.read(ctx) as alone:
        expected = alone.claims.versions(item.ref)
    with core.storage.read(ctx) as together:
        together.claims.prefetch_versions([item.ref])
        got = together.claims.versions(item.ref)
    assert expected == () and got == expected


def test_a_claim_in_a_scope_the_reader_lacks_stays_out_of_reach_loaded_together(shared):
    from scope_recall.core.claims import Qualification

    storage, binding = shared
    writer = shared_context(binding, entry_id="tianshu")
    written = put(storage, writer, "TEST-group-claim/1", scope="TEST-group-a", content="TEST-project 外壳 银色。")
    with storage.write(writer) as tx:
        source = tx.source(written.ref, written.revision)
        saved = tx.claims.append(
            "TEST-group-a",
            draft(source, "银色", predicate="外壳"),
            Qualification("proposed", "inferred_suggestion", "TEST"),
            recorded_at=NOW,
        )
    with storage.read(writer) as tx:
        assert tx.claims.versions(saved.ref), "the writer, holding the scope, reads it"
    narrow = shared_context(binding, scopes={"TEST-scope"})
    with storage.read(narrow) as alone:
        expected = alone.claims.versions(saved.ref)
    with storage.read(narrow) as together:
        together.claims.prefetch_versions([saved.ref])
        got = together.claims.versions(saved.ref)
    assert expected == () and got == expected


def test_an_episode_s_sources_keep_their_order(app):
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    said = [capture(core, ctx, f"TEST 第{index}句。") for index in range(4)]
    with core.storage.read(ctx) as tx:
        episode = tx.episodes.source_episode(said[0].ref, said[0].revision)
        metadata = dict(RetrievalStorage._episode_source_metadata(tx, episode.ref, episode))
    order = json.loads(metadata["source_order"])
    assert [item[0] for item in order] == [source.ref for source in said]
    assert [item[2] for item in order] == sorted(item[2] for item in order)


def test_a_source_in_a_scope_the_reader_lacks_stays_out_of_reach_loaded_together(shared):
    storage, binding = shared
    writer = shared_context(binding, entry_id="tianshu")
    theirs = put(storage, writer, "TEST-group-source/1", scope="TEST-group-a", content="TEST 别的范围的一条。")
    mine = put(storage, writer, "TEST-scope-source/1", scope="TEST-scope", content="TEST 本范围的一条。")
    narrow = shared_context(binding, scopes={"TEST-scope"})
    pairs = [(mine.ref, mine.revision), (theirs.ref, theirs.revision)]
    with storage.read(narrow) as alone:
        expected = [alone.source(*pair) for pair in pairs]
    with storage.read(narrow) as together:
        together.prefetch_sources(pairs)
        got = [together.source(*pair) for pair in pairs]
    assert expected[0] is not None and expected[1] is None and got == expected


def test_a_finished_read_transaction_answers_nothing_it_had_kept(app):
    """It lets go of what it kept: a finished transaction refuses as it always did, never answers from memory."""
    core, ctx = app
    source = capture(core, ctx, "TEST 一条。")
    with core.storage.read(ctx) as tx:
        assert tx.source(source.ref, source.revision) is not None and allowed(tx, "event", source.ref)
        assert tx.knows(("source", source.ref, source.revision))
    assert not tx.knows(("source", source.ref, source.revision))
    with pytest.raises(ContractError) as refused:
        allowed(tx, "event", source.ref)
    assert refused.value.code == "STORAGE_UNAVAILABLE"
