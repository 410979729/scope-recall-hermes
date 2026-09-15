from dataclasses import replace
import json
import itertools

import pytest

from scope_recall.contracts import ContractError, validate_payload
from scope_recall.core import CoreConfig, MemoryCore
from tests.contract.test_v11_profile_entity import app as _entity_app
from tests.contract.test_v11_claims import draft
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
    text = (
        "，".join(kwargs.get("conditions", [])) + f" {subject} {predicate} {target}。"
    )
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
    refs = [
        edge(core, ctx, a, b)[0].ref
        for a, b in [("TEST-A", "TEST-B"), ("TEST-B", "TEST-C"), ("TEST-C", "TEST-D")]
    ]
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
    assert all(
        len({n["id"] for n in p["nodes"]}) == len(p["nodes"]) for p in result["paths"]
    )
    assert not any(
        n["label"] in {"TEST-C", "TEST-Z"} for p in result["paths"] for n in p["nodes"]
    )
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
    assert (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        <= 1024
    )
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
    assert any(
        p["hops"] == 2 and p["nodes"][-1]["label"] == requirement
        for p in result["paths"]
    )
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
            .execute(
                "SELECT subject_ref FROM work_items WHERE work_type='embed' AND state='pending'"
            )
            .fetchall()
        )
    assert source.ref not in {r[0] for r in before}
    queue_embedding_page(storage, ctx)
    with storage.read(ctx) as tx:
        after = (
            tx._check()
            .execute(
                "SELECT subject_ref FROM work_items WHERE work_type='embed' AND state='pending'"
            )
            .fetchall()
        )
    assert [r[0] for r in after] == [r[0] for r in before]
