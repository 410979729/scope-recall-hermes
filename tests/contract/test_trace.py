import itertools
import json
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError, validate_payload
from scope_recall.core import CoreConfig, MemoryCore

from tests.contract.test_v11_claims import draft
from tests.contract.test_v11_profile_entity import app as _entity_app
from tests.v11_support import source_event


@pytest.fixture
def app(tmp_path):
    return _entity_app.__wrapped__(tmp_path)


def query(core, ctx, **kwargs):
    return core.trace(
        ctx,
        {
            "protocol_version": "1.1",
            "request_id": "TEST-trace",
            "subject": "TEST-A",
            **kwargs,
        },
    )


def edge(core, ctx, subject, target, **kwargs):
    predicate = kwargs.pop("predicate", "负责")
    text = "，".join(kwargs.get("conditions", [])) + f" {subject} {predicate} {target}。"
    scope = sorted(ctx.allowed_scope_ids)[0]
    event = source_event(
        source_event_key=f"TEST-trace/{next(core.test_sequence)}",
        content=text,
        occurred_at="2026-09-01T12:00:00Z",
        time_precision="instant",
    )
    captured = core.record_event(ctx, event, scope_id=scope)
    source = core.source(ctx, captured.event_refs[0].ref, 1)
    claim = draft(
        source,
        value=target,
        subject=subject,
        predicate=predicate,
        kind="fact",
        **kwargs,
    )
    result = core.accept_claim_proposals(
        ctx,
        dict(
            protocol_version="1.1",
            source_refs=[f"{source.ref}@1"],
            claim_proposals=[claim],
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id=scope,
    )
    assert result.items[0].state == "active", result
    return result.items[0], source


def test_two_and_three_hop_paths_use_real_edges_and_never_write(app):
    core, ctx = app
    refs = [edge(core, ctx, a, b)[0].ref for a, b in [("TEST-A", "TEST-B"), ("TEST-B", "TEST-C"), ("TEST-C", "TEST-D")]]
    before = core.status(ctx)
    result = query(core, ctx, target="TEST-C", direction="outgoing")
    assert len(result["paths"]) == 1
    assert result["paths"][0]["hops"] == 2
    assert [e["ref"] for e in result["paths"][0]["edges"]] == refs[:2]
    assert not query(core, ctx, target="TEST-D", direction="outgoing")["paths"]
    result = query(core, ctx, target="TEST-D", max_hops=3, direction="outgoing")
    assert result["paths"][0]["hops"] == 3
    assert core.status(ctx) == before
    validate_payload("trace_view", result)


def test_cycles_conditions_and_text_cooccurrence_are_not_invented_edges(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-B", "TEST-A")
    edge(core, ctx, "TEST-B", "TEST-C", conditions=["未授权"])
    edge(core, ctx, "TEST-A", "TEST-X, TEST-Y", predicate="名单")
    edge(core, ctx, "TEST-X", "TEST-Z")
    result = query(core, ctx, max_hops=3, direction="outgoing")
    assert all(len({n["id"] for n in p["nodes"]}) == len(p["nodes"]) for p in result["paths"])
    assert not any(n["label"] in {"TEST-C", "TEST-Z"} for p in result["paths"] for n in p["nodes"])
    assert "conditional_relation_not_traversed" in result["gaps"]


def test_identical_names_in_other_authorized_scope_do_not_join(app):
    core, ctx = app
    binding = replace(
        ctx.binding,
        data_directory=ctx.binding.data_directory / "multi",
        scope_ids=frozenset({"TEST-scope", "TEST-other"}),
    )
    ctx = replace(ctx, binding=binding, allowed_scope_ids=binding.scope_ids)
    core = MemoryCore(CoreConfig(binding), clock=core.clock)
    core.initialize()
    core.test_sequence = itertools.count(1)
    scopes = sorted(ctx.binding.scope_ids)
    assert len(scopes) >= 2
    one = replace(ctx, allowed_scope_ids=frozenset({scopes[0]}))
    two = replace(ctx, allowed_scope_ids=frozenset({scopes[1]}))
    edge(core, one, "TEST-A", "TEST-B")
    edge(core, two, "TEST-B", "TEST-C")
    result = query(core, ctx, target="TEST-C", direction="outgoing")
    assert not result["paths"]


def test_path_and_byte_limits_keep_whole_paths_and_report_partial(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-B", "TEST-C")
    result = query(core, ctx, max_paths=1, direction="outgoing")
    assert result["truncated"] and "path_limit" in result["gaps"]
    result = query(core, ctx, budget_bytes=1024, direction="outgoing")
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert all(len(p["nodes"]) == len(p["edges"]) + 1 for p in result["paths"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_hops", 4),
        ("max_nodes", True),
        ("scope_id", "private"),
        ("budget_bytes", 1),
    ],
)
def test_trace_rejects_unbounded_or_forged_inputs(app, field, value):
    core, ctx = app
    with pytest.raises(ContractError):
        query(core, ctx, **{field: value})


def test_graph_can_be_omitted_without_affecting_existing_entity(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    # No additional initialization/schema/index dependency is required.
    fresh = MemoryCore(CoreConfig(ctx.binding), clock=core.clock)
    result = fresh.entity(
        ctx,
        {
            "protocol_version": "1.1",
            "request_id": "TEST-direct",
            "subject": "TEST-A",
            "action": "probe",
        },
    )
    assert result["statements"][0]["value_text"] == "TEST-B"


def test_long_requirement_is_a_terminal_value_not_an_invalid_entity(app):
    core, ctx = app
    edge(core, ctx, "TEST-A", "TEST-B")
    requirement = "验收需求内容" * 50
    edge(core, ctx, "TEST-B", requirement, predicate="需求")
    result = query(core, ctx, max_hops=3, direction="outgoing")
    assert any(p["hops"] == 2 and p["nodes"][-1]["label"] == requirement for p in result["paths"])
    validate_payload("trace_view", result)


def test_deleted_edge_and_racing_epoch_never_release_stale_paths(app, monkeypatch):
    import scope_recall.core.trace as trace

    from tests.contract.test_v11_claims import capture

    core, ctx = app
    first, _ = edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-B", "TEST-C")
    assert query(core, ctx, target="TEST-C")["paths"]
    original = trace.read_entity
    changed = False

    def racing(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            capture(core, ctx, f"删除 {first.ref}。TEST-A 负责 TEST-B。")
            core.forget(
                ctx,
                {
                    "protocol_version": "1.1",
                    "target_refs": [first.ref],
                    "mode": "delete",
                    "expected_revisions": {first.ref: 1},
                },
                remaining_seconds=10,
            )
        return result

    monkeypatch.setattr(trace, "read_entity", racing)
    result = query(core, ctx, target="TEST-C")
    assert result["status"] == "unavailable" and result["paths"] == []
    monkeypatch.setattr(trace, "read_entity", original)
    assert not query(core, ctx, target="TEST-C")["paths"]


def test_index_page_excludes_deleted_sources_and_is_idempotent(app):
    from scope_recall.core.index_rebuild import queue_embedding_page
    from scope_recall.core.storage import SQLiteStorage

    from tests.contract.test_v11_claims import capture

    core, ctx = app
    _, source = edge(core, ctx, "TEST-A", "TEST-B")
    edge(core, ctx, "TEST-B", "TEST-C")
    capture(core, ctx, f"删除 {source.ref}。TEST-A 负责 TEST-B。")
    core.forget(
        ctx,
        {
            "protocol_version": "1.1",
            "target_refs": [source.ref],
            "mode": "delete",
            "expected_revisions": {source.ref: 1},
        },
        remaining_seconds=10,
    )
    storage = SQLiteStorage(ctx.binding)
    page = queue_embedding_page(storage, ctx)
    assert page["finished"]
    with storage.read(ctx) as tx:
        before = (
            tx._check()
            .execute("SELECT subject_ref FROM work_items WHERE work_type='embed' AND state='pending'")
            .fetchall()
        )
    assert source.ref not in {r[0] for r in before}
    queue_embedding_page(storage, ctx)
    with storage.read(ctx) as tx:
        after = (
            tx._check()
            .execute("SELECT subject_ref FROM work_items WHERE work_type='embed' AND state='pending'")
            .fetchall()
        )
    assert [r[0] for r in after] == [r[0] for r in before]


def _imported(core, ctx, text, *, role, key):
    """A source an import brought in, and no embedding for it, as a store that never had one would leave it."""
    import sqlite3

    from scope_recall.contracts import ImportProvenance, import_source_fingerprint

    original = (
        "human_direct"
        if role == "user"
        else "assistant_visible"
        if role == "assistant"
        else "tool_observation"
        if role == "tool"
        else "origin_unknown"
    )
    event = source_event(
        source_event_key=key,
        source_revision=1,
        origin="imported",
        role=role,
        content=text,
        occurred_at="2026-07-01T12:00:00Z",
        time_precision="instant",
        source_original_origin=original,
    )
    importer = ImportProvenance(original, "a" * 64, frozenset({import_source_fingerprint(event)}))
    saved = core.record_event(
        replace(ctx, actor_origin="imported", import_provenance=importer),
        event,
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    ref = saved.event_refs[0].ref
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DELETE FROM work_items WHERE subject_ref=? AND work_type='embed'", (ref,))
        conn.commit()
    return ref


def _embeds(core):
    import sqlite3

    with sqlite3.connect(core.storage.path) as conn:
        return {row[0] for row in conn.execute("SELECT subject_ref FROM work_items WHERE work_type='embed'")}


def test_an_import_s_history_gets_the_embedding_its_store_never_had(app):
    """An import queued an embedding only where its source store had one: on the pilot tianshu's history, 1,928 of
    the owner's messages, 6,552 replies and 3,009 notes, could be found by their words alone.  Tool output is left
    out (200,000 imported outputs), and so is what the admission rules keep without one."""
    from scope_recall.core.index_rebuild import queue_import_embeddings
    from scope_recall.core.storage import SQLiteStorage

    core, ctx = app
    said = _imported(core, ctx, "TEST 家里的猫叫小橘。", role="user", key="TEST-import/said")
    told = _imported(core, ctx, "TEST 好的，记住了，猫叫小橘。", role="assistant", key="TEST-import/told")
    note = _imported(core, ctx, "TEST 旧笔记：小橘怕打雷。", role="unknown", key="TEST-import/note")
    tool = _imported(core, ctx, "TEST ls 输出：a.txt b.txt", role="tool", key="TEST-import/tool")
    ack = _imported(core, ctx, "好的", role="user", key="TEST-import/ack")
    storage = SQLiteStorage(ctx.binding)
    first = queue_import_embeddings(storage, ctx, limit=2)
    assert first["scanned"] == 2 and not first["finished"] and not first["held"] and first["queued"] in (1, 2)
    rest = queue_import_embeddings(storage, ctx, after_key=first["after_key"], limit=200)
    assert rest["finished"]
    assert {said, told, note} <= _embeds(core) and not {tool, ack} & _embeds(core)
    # Looked at again from the start, nothing is queued twice and the acknowledgement is passed over again.
    again = queue_import_embeddings(storage, ctx, limit=200)
    assert again["queued"] == 0 and again["finished"]


def test_the_backfill_queues_only_what_this_worker_embeds(app):
    """An import in another project was queued where this worker neither counts nor claims it (84 of 84 in three
    pages, the queue read empty); a deleted, a suppressed and a superseded import were never tested."""
    import sqlite3

    from scope_recall.core.index_rebuild import queue_import_embeddings
    from scope_recall.core.storage import SQLiteStorage

    core, ctx = app
    said = _imported(core, ctx, "TEST 家里的猫叫小橘。", role="user", key="TEST-import/here")
    elsewhere = _imported(core, ctx, "TEST 另一个项目里的话。", role="user", key="TEST-import/elsewhere")
    deleted = _imported(core, ctx, "TEST 已经删掉的话。", role="user", key="TEST-import/deleted")
    hidden = _imported(core, ctx, "TEST 被隐藏的话。", role="user", key="TEST-import/hidden")
    old = _imported(core, ctx, "TEST 旧的说法。", role="user", key="TEST-import/revised")
    conn = sqlite3.connect(core.storage.path)
    try:
        conn.execute("UPDATE source_events SET project_id='TEST-other-project' WHERE event_id=?", (elsewhere,))
        conn.execute("UPDATE source_events SET read_blocked=1 WHERE event_id=?", (deleted,))
        conn.execute("UPDATE source_events SET suppressed=1 WHERE event_id=?", (hidden,))
        # A later revision of the same source: only it is current.
        columns = [row[1] for row in conn.execute("PRAGMA table_info(source_events)")]
        picked = [
            "source_revision+1" if name == "source_revision" else "NULL" if name == "source_id" else name
            for name in columns
        ]
        conn.execute(
            f"INSERT INTO source_events({','.join(columns)}) SELECT {','.join(picked)} FROM source_events"
            " WHERE event_id=?",
            (old,),
        )
        conn.commit()
    finally:
        conn.close()
    storage = SQLiteStorage(ctx.binding)
    after_key = None
    for _ in range(10):
        page = queue_import_embeddings(storage, ctx, after_key=after_key, limit=1)
        after_key = page["after_key"]
        if page["finished"]:
            break
    conn = sqlite3.connect(core.storage.path)
    try:
        queued = set(conn.execute("SELECT subject_ref,subject_revision FROM work_items WHERE work_type='embed'"))
    finally:
        conn.close()
    assert (said, 1) in queued and (old, 2) in queued and (old, 1) not in queued
    assert not {ref for ref, _revision in queued} & {elsewhere, deleted, hidden}
    with storage.read(ctx) as tx:
        assert tx.work.pending_depth("embed") == len(queued), "every queued embedding is one this worker claims"


def test_each_worker_partition_keeps_its_own_backfill_place(app, tmp_path):
    """One state file for the store let a worker with no imports of its own write ``finished`` for another worker's,
    which then waited a day before queueing its imports (review of 3.4.0rc10)."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.runtime.vector_upkeep import backfill_if_due

    core, ctx = app
    theirs = _imported(core, ctx, "TEST 另一个项目里的话。", role="user", key="TEST-import/project")
    conn = sqlite3.connect(core.storage.path)
    try:
        conn.execute("UPDATE source_events SET project_id='TEST-project-p' WHERE event_id=?", (theirs,))
        conn.commit()
    finally:
        conn.close()
    vectors = SimpleNamespace(storage_dir=tmp_path)
    storage = SQLiteStorage(ctx.binding)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    assert backfill_if_due(storage, ctx, vectors, now=now)["outcome"] == "finished"
    receipt = backfill_if_due(storage, replace(ctx, project_id="TEST-project-p"), vectors, now=now + timedelta(hours=1))
    assert receipt is not None and receipt["queued"] == 1 and theirs in _embeds(core)


def test_the_backfill_waits_while_captured_messages_wait_for_their_embeddings(app):
    """A message captured now is never queued behind an import's history for its vector."""
    import sqlite3

    from scope_recall.core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING, queue_import_embeddings
    from scope_recall.core.storage import SQLiteStorage

    core, ctx = app
    said = _imported(core, ctx, "TEST 家里的猫叫小橘。", role="user", key="TEST-import/held")
    with sqlite3.connect(core.storage.path) as conn:
        for index in range(IMPORT_EMBED_QUEUE_CEILING):
            conn.execute(
                """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at)
                VALUES ('embed',?,1,'TEST-scope','2026-09-28T00:00:00Z')""",
                (f"event-TEST-waiting-{index}",),
            )
        conn.commit()
    page = queue_import_embeddings(SQLiteStorage(ctx.binding), ctx)
    assert page["held"] and page["queued"] == 0 and page["after_key"] == ("", 0)
    assert said not in _embeds(core)


def test_the_drain_s_backfill_keeps_its_place_and_looks_again_after_a_day(app, tmp_path):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.runtime.vector_upkeep import EMBED_BACKFILL_RECHECK, backfill_if_due

    core, ctx = app
    said = _imported(core, ctx, "TEST 家里的猫叫小橘。", role="user", key="TEST-import/drain")
    vectors = SimpleNamespace(storage_dir=tmp_path)
    storage = SQLiteStorage(ctx.binding)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    receipt = backfill_if_due(storage, ctx, vectors, now=now)
    assert receipt["outcome"] == "finished" and receipt["queued"] == 1 and said in _embeds(core)
    assert backfill_if_due(storage, ctx, vectors, now=now + timedelta(hours=1)) is None
    later = _imported(core, ctx, "TEST 后来又导入的一句话。", role="user", key="TEST-import/later")
    looked = backfill_if_due(storage, ctx, vectors, now=now + EMBED_BACKFILL_RECHECK)
    assert looked["outcome"] == "finished" and later in _embeds(core)
    assert looked["queued_total"] == 2, "the total runs on across the daily looks"


def test_the_backfill_leaves_room_for_candidate_evaluations(app, tmp_path):
    """Embeddings are claimed before candidate evaluations, and the backfill kept a page of them waiting, so every
    pass took embeddings alone: 251 evaluations waited on the pilot for the hours it ran (rc10).  Stopping for them
    stopped it for as long as they could not be done (review of rc11).  While one it is told of is ready, it keeps
    the queue to its yield ceiling and goes on; one not due yet changes nothing."""
    import sqlite3
    from types import SimpleNamespace

    from scope_recall.core.index_rebuild import queue_import_embeddings
    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.runtime.vector_upkeep import backfill_if_due

    core, ctx = app
    said = [
        _imported(core, ctx, f"TEST 导入的第{index}句话。", role="user", key=f"TEST-import/room-{index}")
        for index in range(4)
    ]
    with sqlite3.connect(core.storage.path) as conn:
        for ref, due in (
            ("candidate-TEST-ready", "2026-09-28T00:00:00Z"),
            ("candidate-TEST-later", "2999-01-01T00:00:00Z"),
        ):
            conn.execute(
                """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at)
                VALUES ('evaluate_candidate',?,1,'TEST-scope',?)""",
                (ref, due),
            )
        conn.commit()
    storage = SQLiteStorage(ctx.binding)
    evaluations = frozenset({"evaluate_candidate"})
    page = queue_import_embeddings(storage, ctx, yield_to=evaluations, yield_ceiling=2)
    assert (page["held"], page["queued"], page["finished"]) == (False, 2, False), page
    assert sum(ref in _embeds(core) for ref in said) == 2
    receipt = backfill_if_due(
        storage, ctx, SimpleNamespace(storage_dir=tmp_path), yield_to=evaluations, yield_ceiling=2
    )
    assert (receipt["outcome"], receipt["queued"]) == ("held", 0), "the drain's upkeep passes it on"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE subject_ref='candidate-TEST-ready'")
        conn.commit()
    page = queue_import_embeddings(storage, ctx, after_key=page["after_key"], yield_to=evaluations, yield_ceiling=2)
    assert (page["queued"], page["finished"]) == (2, True) and all(ref in _embeds(core) for ref in said)


def test_a_page_never_takes_the_embedding_queue_past_its_ceiling(app):
    """The queue was measured before a page of 64 joined it, so up to 127 embeddings waited (review of rc11).  A page
    now looks at no more sources than can join, and is not taken for the last because it looked at fewer."""
    import sqlite3

    from scope_recall.core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING, queue_import_embeddings
    from scope_recall.core.storage import SQLiteStorage

    core, ctx = app
    said = [
        _imported(core, ctx, f"TEST 导入的第{index}句。", role="user", key=f"TEST-import/over-{index}")
        for index in range(3)
    ]
    with sqlite3.connect(core.storage.path) as conn:
        for index in range(IMPORT_EMBED_QUEUE_CEILING - 1):
            conn.execute(
                """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,available_at)
                VALUES ('embed',?,1,'TEST-scope','2026-09-28T00:00:00Z')""",
                (f"event-TEST-waiting-{index}",),
            )
        conn.commit()
    page = queue_import_embeddings(SQLiteStorage(ctx.binding), ctx)
    assert (page["queued"], page["scanned"], page["finished"]) == (1, 1, False), page
    assert sum(ref in _embeds(core) for ref in said) == 1
