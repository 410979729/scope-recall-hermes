"""Regression contracts for complete, current and incrementally reused summaries."""

import json
from dataclasses import replace

import pytest
from scope_recall.core.consolidation_summary import grounded_sentences
from scope_recall.core.management import MemoryManagement
from scope_recall.core.recall_packet import render_recall_packet_context
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.summary_validity import summary_live
from test_claims import capture, initial
from test_importance_summaries import SummaryModel, app, build, scoped  # noqa: F401
from v11_support import recall_request, source_event


class FirstSentenceModel:
    """Record model input and select the first sentence without network access."""

    def __init__(self):
        self.pages = []

    def judge(self, messages, *, remaining_seconds):
        sources = json.loads(messages[-1]["content"])["sources"]
        self.pages.append(sources)
        return json.dumps(
            dict(
                sentences=[
                    dict(
                        text=(
                            s["content"].partition("。")[0] + "。"
                            if "。" in s["content"]
                            else s["content"].partition(". ")[0] + ("." if ". " in s["content"] else "")
                        ),
                        source_ref=s["ref"],
                        source_revision=s["revision"],
                    )
                    for s in sources
                ]
            )
        )


def rendered_conversation(core, ctx, query="昨天做了什么"):
    """Compile and render the packet through the product's conversation recall path."""
    from datetime import UTC

    reader = replace(ctx, session_id="TEST-summary-reader")
    packet = core.recall_packet(reader, recall_request(query=query), deadline_seconds=10, zone=UTC)
    prepared = render_recall_packet_context(
        packet, installation_id=ctx.binding.installation_id, session_id=reader.session_id
    )
    (core.storage.path.parent / "rendered-packet.json").write_text(
        json.dumps(dict(packet=packet, rendered=prepared.canonical_text), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return packet, prepared.canonical_text or ""


@pytest.mark.parametrize("role,limit", [("user", 1000), ("assistant", 600), ("tool", 300)])
def test_summary_request_truncates_each_role_without_changing_original(app, role, limit):
    from scope_recall.core.worker_summaries import summary_messages

    core, ctx = app
    origin = {"user": "human_direct", "assistant": "assistant_visible", "tool": "tool_observation"}[role]
    text = "TEST-source. " + "x" * 1500
    event = source_event(source_event_key="TEST-role-limit", role=role, origin=origin, content=text)
    saved = core.record_event(replace(ctx, actor_origin=origin), event, scope_id="TEST-scope", remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        original = tx.source(saved.event_refs[0].ref, 1)
        record = json.loads(summary_messages((original,))[-1]["content"])["sources"][0]
        assert record["content"] == text[:limit] + f"（已截断，全文 {len(text)} 字符）"
        assert tx.source(original.ref, original.revision).event["content"] == text


def test_long_completed_task_is_paged_and_keeps_unquoted_originals(app):
    from scope_recall.core.summary_storage import fully_quoted_sources

    core, ctx = app
    task = replace(ctx, task_anchor="TEST-long-task")
    capture(core, task, "Continue TEST-project and run checks.", when="2026-09-05T10:00:00Z")
    assistants = []
    for turn in range(30):
        capture(core, task, f"TEST-checkpoint {turn}.", when="2026-09-05T10:00:00Z")
        event = source_event(
            source_event_key=f"TEST-long-assistant-{turn}",
            role="assistant",
            origin="assistant_visible",
            content=f"TEST-result {turn}. " + "x" * 8000,
            occurred_at="2026-09-05T10:00:00Z",
        )
        saved = core.record_event(
            replace(task, actor_origin="assistant_visible"), event, scope_id="TEST-scope", remaining_seconds=10
        )
        assistants.append((saved.event_refs[0].ref, 1))
    capture(core, task, "The task is completed.", when="2026-09-05T11:02:00Z")
    model = FirstSentenceModel()
    for _ in range(4):
        result = build(core, task, model)
        assert result.failed == 0
        with core.storage.read(ctx) as tx:
            group = tx._check().execute("SELECT state,failure FROM summary_groups WHERE kind='task'").fetchone()
            if group and group[0] == "complete":
                break
    assert group[0] == "complete" and group[1] is None
    assert len(model.pages) <= 16
    with core.storage.read(ctx) as tx:
        rows = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts").fetchall()
        versions = [tx.claims.version(*row) for row in rows]
        assert not fully_quoted_sources(tx, versions, assistants)


def test_task_page_accounts_for_recovery_hint_in_request_limit(app, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.core import worker_summaries

    core, ctx = app
    sources = [capture(core, ctx, f"TEST-item {i}. " + "x" * 850) for i in range(16)]
    members = [dict(ref=s.ref, revision=s.revision, total=len(s.event["content"])) for s in sources]
    group = dict(manifest_json=json.dumps(members))
    episode = SimpleNamespace(ref="TEST-task", resume=dict(text="r" * 3000))
    monkeypatch.setattr(
        worker_summaries,
        "unprocessed_ranges",
        lambda tx, group: [(i, 0, member["total"]) for i, member in enumerate(members)],
    )
    monkeypatch.setattr(worker_summaries, "obsolete_ranges", lambda tx, members: {})
    with core.storage.read(ctx) as tx:
        page, coverage = worker_summaries._read_page(tx, group, episode)
    assert len(page) < len(sources) and len(coverage) == len(page)
    messages = worker_summaries.summary_messages(page, episode_ref=worker_summaries._task_hint(episode, page))
    assert sum(len(message["content"].encode("utf-8")) for message in messages) <= 12000


def test_truncated_assistant_stays_a_time_channel_candidate(app):
    core, ctx = app
    content = "TEST-project checks passed. " + "Additional detail " * 80
    event = source_event(
        source_event_key="TEST-long-visible-reply",
        role="assistant",
        origin="assistant_visible",
        content=content,
        occurred_at="2026-09-05T10:00:00Z",
    )
    saved = core.record_event(
        replace(ctx, actor_origin="assistant_visible"), event, scope_id="TEST-scope", remaining_seconds=10
    )
    model = FirstSentenceModel()
    assert build(core, ctx, model).failed == 0
    assert len(model.pages[0][0]["content"]) < len(content)
    assert any(item.ref == saved.event_refs[0].ref for item in scoped(core, ctx))


@pytest.mark.parametrize(
    "content,excerpt",
    [
        ("只有测试通过后，才允许发布。", "才允许发布。"),
        ("If approved, deploy TEST-project.", "deploy TEST-project."),
    ],
)
def test_partial_condition_excerpt_expands_to_its_whole_sentence(app, content, excerpt):
    core, ctx = app
    source = capture(core, ctx, content, when="2026-09-05T10:00:00Z")
    sentences, spans = grounded_sentences(
        dict(sentences=[dict(text=excerpt, source_ref=source.ref, source_revision=1)]), (source,)
    )
    assert sentences == [content]
    assert spans[0]["start"] == 0 and spans[0]["end"] == len(content)

    class Partial:
        def judge(self, messages, *, remaining_seconds):
            return json.dumps(dict(sentences=[dict(text=excerpt, source_ref=source.ref, source_revision=1)]))

    build(core, ctx, Partial())
    _packet, rendered = rendered_conversation(core, ctx)
    assert content in rendered


def test_overlong_expanded_sentence_is_refused_instead_of_cropping_its_condition(app):
    core, ctx = app
    source = capture(core, ctx, "条件" * 800 + "，才允许发布。")
    sentences, _spans = grounded_sentences(
        dict(sentences=[dict(text="才允许发布。", source_ref=source.ref, source_revision=1)]), (source,)
    )
    assert sentences == []


@pytest.mark.parametrize(
    "content,details",
    [
        ("TEST-project 发布了蓝版。回滚期限改为三天。", ("发布了蓝版", "回滚期限改为三天")),
        (
            "TEST-project released blue. The rollback deadline is three days.",
            ("released blue", "deadline is three days"),
        ),
    ],
)
def test_partial_excerpt_keeps_unquoted_answer_details_in_the_rendered_packet(app, content, details):
    core, ctx = app
    source = capture(core, ctx, content, when="2026-09-05T10:00:00Z")
    build(core, ctx, FirstSentenceModel())
    assert any(c.ref == source.ref for c in scoped(core, ctx))
    _packet, rendered = rendered_conversation(core, ctx)
    assert all(detail in rendered for detail in details)


def test_rebuilding_after_owner_correction_prunes_old_excerpts_without_another_request(app):
    core, ctx = app
    item, _source = initial(core, ctx, when="2026-09-05T10:00:00Z")
    build(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=item.revision, value="绿色")
    model = FirstSentenceModel()
    build(core, ctx, model)
    with core.storage.read(ctx) as tx:
        live = [
            tx.claims.version(*r).payload["value_text"]
            for r in tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts")
            if summary_live(tx, *r)
        ]
    assert not any("蓝色" in text for text in live)
    assert model.pages == []
    packet, rendered = rendered_conversation(core, ctx)
    assert all("蓝色" not in i["content"] for i in packet["items"] if i["kind"] == "claim")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "绿色"
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial'")
    raw_packet, raw_rendered = rendered_conversation(core, ctx)
    assert packet["items"] == raw_packet["items"]
    assert rendered == raw_rendered


def test_completed_task_with_tool_evidence_is_used_by_the_conversation_channel(app):
    core, ctx = app
    task = replace(ctx, task_anchor="TEST-summary-task")
    capture(core, task, "Implement TEST-project.", when="2026-09-05T10:00:00Z")
    actor = replace(task, actor_origin="assistant_visible")
    event = source_event(
        source_event_key="TEST-assistant",
        origin="assistant_visible",
        role="assistant",
        content="TEST-project checks completed.",
        occurred_at="2026-09-05T11:00:00Z",
    )
    core.record_event(actor, event, scope_id="TEST-scope", remaining_seconds=10)
    capture(core, task, "TEST-tool returned zero.", origin="tool_observation", when="2026-09-05T11:01:00Z")
    capture(core, task, "The task is completed.", when="2026-09-05T11:02:00Z")
    build(core, task)
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial' WHERE kind='session'")
    assert any(c.kind == "claim" for c in scoped(core, ctx))
    _packet, rendered = rendered_conversation(core, ctx)
    assert "TEST-tool returned zero" in rendered and "checks completed" in rendered


def test_pinning_preserves_the_base_importance_when_pin_is_removed(app):
    core, ctx = app
    item, _source = initial(core, ctx)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.importance(item.ref, expected_revision=1, importance=2, pinned=True)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT importance FROM claims WHERE claim_id=?", (item.ref,)).fetchone()[0] == 2
    assert owner.show(item.ref)["value"]["importance"] == 5
    owner.importance(item.ref, expected_revision=1, pinned=False)
    assert owner.show(item.ref)["value"]["importance"] == 2
    owner.importance(item.ref, expected_revision=1, pinned=True)
    owner.importance(item.ref, expected_revision=1, pinned=False)
    assert owner.show(item.ref)["value"]["importance"] == 2


def test_cli_default_subscription_budget_does_not_authorize_summary_requests(app):
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, summary_budget_enabled

    core, _ctx = app
    config = AuxiliaryRuntimeConfig.from_mapping(
        dict(
            external_embedding=False,
            external_consolidation=True,
            ledger_path=str(core.storage.path.parent / "TEST-ledger.sqlite3"),
            consolidation=dict(
                kind="codex_cli", executable=str(core.storage.path.parent / "TEST-cli.exe"), executable_sha256="a" * 64
            ),
        )
    )
    assert config.consolidation.subscription_budget.daily_calls == 4
    assert not summary_budget_enabled(config)


def test_continued_session_reuses_unchanged_completed_pages(app):
    core, ctx = app
    model = FirstSentenceModel()
    original_ref = None
    for turn in range(3):
        capture(core, ctx, f"TEST-project 第{turn}项已经记录。", when=f"2026-09-05T1{turn}:00:00Z")
        build(core, ctx, model)
        with core.storage.read(ctx) as tx:
            ref = (
                tx._check()
                .execute(
                    "SELECT p.claim_ref,p.claim_revision FROM summary_parts p JOIN summary_groups g USING(group_key) "
                    "WHERE p.generation=g.generation ORDER BY p.ordinal LIMIT 1"
                )
                .fetchone()
            )
            original_ref = tuple(ref) if original_ref is None else original_ref
            assert tuple(ref) == original_ref
    assert [len(page) for page in model.pages] == [1, 1, 1]
    _packet, rendered = rendered_conversation(core, ctx)
    assert all(f"第{turn}项已经记录" in rendered for turn in range(3))


def test_first_summary_masks_only_positioned_obsolete_evidence(app):
    from test_claims import accept, draft

    core, ctx = app
    old = "TEST-project 配色 蓝色。"
    source = capture(core, ctx, old + "回滚期限三天。", when="2026-09-05T10:00:00Z")
    proposal = draft(source, quote_override=old)
    proposal["evidence_spans"][0].update(start=0, end=len(old))
    item = accept(core, ctx, proposal).items[0]
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.revise(item.ref, expected_revision=1, value="绿色")
    model = FirstSentenceModel()
    build(core, ctx, model)
    assert model.pages and "蓝色" not in str(model.pages)
    assert "回滚期限三天" in str(model.pages)
    packet, rendered = rendered_conversation(core, ctx)
    assert "回滚期限三天" in rendered
    with core.storage.read(ctx) as tx:
        excerpts = [
            tx.claims.version(*r).payload["value_text"]
            for r in tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts")
            if summary_live(tx, *r)
        ]
    assert excerpts and all("蓝色" not in text for text in excerpts)
    assert any("回滚期限三天" in text for text in excerpts)
    assert all("蓝色" not in i["content"] for i in packet["items"] if i["kind"] == "claim")


def test_changed_source_reuses_other_completed_pages(app):
    core, ctx = app
    model = FirstSentenceModel()
    for turn in range(3):
        capture(core, ctx, f"TEST-project 第{turn}项记录。", key=f"TEST-page-{turn}", when=f"2026-09-05T1{turn}:00:00Z")
        build(core, ctx, model)
    capture(core, ctx, "TEST-project 第二项更换记录。", key="TEST-page-1", revision=2, when="2026-09-05T11:00:00Z")
    build(core, ctx, model)
    assert [len(page) for page in model.pages] == [1, 1, 1, 1]
    assert model.pages[-1][0]["revision"] == 2


def test_cli_route_without_explicit_budget_never_calls_the_summary_model(app):
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.instance import build_runtime_instance
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig

    core, ctx = app
    capture(core, ctx, "TEST-project is ready.")
    with core.storage.write(ctx) as tx:
        from scope_recall.core.summary_storage import close_session

        close_session(tx, ctx.session_id, now=core.clock.utc_now())
        tx._check().execute("UPDATE work_items SET state='done'")
    auxiliary = AuxiliaryRuntimeConfig.from_mapping(
        dict(
            external_embedding=False,
            external_consolidation=True,
            ledger_path=str(core.storage.path.parent / "TEST-ledger.sqlite3"),
            consolidation=dict(
                kind="codex_cli", executable=str(core.storage.path.parent / "TEST-cli.exe"), executable_sha256="a" * 64
            ),
        )
    )

    class NoRequests:
        def propose(self, messages, *, remaining_seconds):
            raise AssertionError("no explicit summary budget")

    config = RuntimeInstanceConfig(
        ctx.binding,
        ctx.session_id,
        ctx.allowed_scope_ids,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        auxiliary=auxiliary,
        supervisor_enabled=False,
    )
    instance = build_runtime_instance(config, consolidation=NoRequests())
    try:
        assert instance.drain(remaining_seconds=20).failed == 0
    finally:
        instance.close()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM summary_parts").fetchone()[0] == 0


def test_hundred_idle_turns_generate_only_new_short_message_pages(app):
    from datetime import UTC, datetime, timedelta

    from scope_recall.core.summary_storage import queue_summaries
    from scope_recall.core.worker import WorkerConfig, drain_worker

    core, ctx = app
    model = FirstSentenceModel()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    increments = []
    for turn in range(100):
        now = (start + timedelta(seconds=turn * 2000)).isoformat().replace("+00:00", "Z")
        core.clock.now = now
        for role, origin in (("user", "human_direct"), ("assistant", "assistant_visible")):
            actor = replace(ctx, actor_origin=origin)
            event = source_event(
                source_event_key=f"TEST-idle-{turn}-{role}",
                origin=origin,
                role=role,
                content=f"TEST-project item {turn} {role} completed.",
                occurred_at=now,
                recorded_at=now,
            )
            core.record_event(actor, event, scope_id="TEST-scope", remaining_seconds=10)
        core.clock.now = (start + timedelta(seconds=turn * 2000 + 1801)).isoformat().replace("+00:00", "Z")
        with core.storage.write(ctx) as tx:
            tx._check().execute("UPDATE work_items SET state='done' WHERE work_type!='rebuild_projection'")
            queue_summaries(tx, now=core.clock.utc_now())
        previous = len(model.pages)
        result = drain_worker(
            core.storage,
            core.clock,
            ctx,
            config=WorkerConfig("TEST-idle-summary", max_items=32, summary_capable=True),
            consolidation=model,
            remaining_seconds=30,
        )
        assert result.failed == 0
        increments.append(len(model.pages) - previous)
    assert len(model.pages) == 100 and increments == [1] * 100
    assert all(len(page) == 2 for page in model.pages)
    once_ctx = replace(ctx, session_id="TEST-end-once")
    once_model = FirstSentenceModel()
    for turn in range(100):
        for role, origin in (("user", "human_direct"), ("assistant", "assistant_visible")):
            event = source_event(
                source_event_key=f"TEST-once-{turn}-{role}",
                origin=origin,
                role=role,
                content=f"TEST-project item {turn} {role} completed.",
                occurred_at=core.clock.now,
            )
            core.record_event(
                replace(once_ctx, actor_origin=origin), event, scope_id="TEST-scope", remaining_seconds=10
            )
    assert build(core, once_ctx, once_model).failed == 0
    assert len(once_model.pages) == 13
    receipt = dict(
        turns=100,
        sources=200,
        requests=100,
        max_requests_per_resume=max(increments),
        previous_full_rebuild=sum((2 * k + 15) // 16 for k in range(1, 101)),
        ended_once=len(once_model.pages),
    )
    (core.storage.path.parent / "continuation-cost.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


@pytest.mark.parametrize("blocked_position,expected", [(0, True), (1, False)])
def test_reused_repeated_quote_retains_its_recorded_occurrence(app, blocked_position, expected):
    core, ctx = app
    sentence = "TEST-project 配色蓝色。"
    source = capture(core, ctx, sentence + sentence)
    second = len(sentence)
    value = dict(sentences=[dict(text=sentence, source_ref=source.ref, source_revision=1)])
    positions = [dict(start=second, end=second * 2)]
    blocked = {(source.ref, 1): [(blocked_position * second, (blocked_position + 1) * second)]}
    sentences, spans = grounded_sentences(value, (source,), blocked=blocked, positions=positions)
    assert bool(sentences) == expected
    if expected:
        assert spans[0]["start"] == second


def test_task_tool_from_another_session_cannot_enter_a_session_scoped_packet(app):
    core, ctx = app
    task = replace(ctx, task_anchor="TEST-cross-session-task")
    capture(core, task, "Implement TEST-project.", when="2026-09-05T10:00:00Z")
    capture(
        core,
        replace(task, session_id="TEST-tool-session"),
        "TEST-other-session-tool: returned zero.",
        origin="tool_observation",
        when="2026-09-05T11:01:00Z",
    )
    capture(core, task, "The task is completed.", when="2026-09-05T11:02:00Z")
    build(core, task)
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial' WHERE kind='session'")
    packet, rendered = rendered_conversation(core, ctx, "上次聊到哪")
    assert "Implement TEST-project" in rendered
    assert all("TEST-other-session-tool" not in item["content"] for item in packet["items"] if item["kind"] == "claim")
    with core.storage.write(ctx) as tx:
        tx._check().execute("UPDATE summary_groups SET state='partial'")
    baseline, _raw = rendered_conversation(core, ctx, "上次聊到哪")
    assert packet["items"] == baseline["items"]


def test_task_summary_members_stay_in_their_own_entry(tmp_path):
    import itertools

    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.summary_validity import group_members
    from test_claims import Clock
    from v11_support import context

    original = context(tmp_path / "TEST-shared-summaries")
    binding = replace(original.binding, installation_kind="shared")
    ctx = replace(original, binding=binding, entry_id="test-entry-a", project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    with core.storage.write(replace(ctx, entry_id=None)) as tx:
        tx.registry.register_entry("test-entry-a", "Entry A", "test", now=core.clock.utc_now())
        tx.registry.register_entry("test-entry-b", "Entry B", "test", now=core.clock.utc_now())
    task = replace(ctx, task_anchor="TEST-cross-entry-task")
    capture(core, task, "Implement TEST-project.", when="2026-09-05T10:00:00Z")
    tool = capture(
        core,
        replace(task, entry_id="test-entry-b"),
        "TEST-other-entry-tool: returned zero.",
        origin="tool_observation",
        when="2026-09-05T11:01:00Z",
    )
    capture(core, task, "The task is completed.", when="2026-09-05T11:02:00Z")
    with core.storage.read(ctx) as tx:
        group = (
            tx._check().execute("SELECT * FROM summary_groups WHERE kind='task' AND entry_id='test-entry-a'").fetchone()
        )
        assert all(m["ref"] != tool.ref for m in group_members(tx, group))


def test_summary_repeated_in_shown_original_is_omitted(app):
    core, ctx = app
    source = capture(core, ctx, "If approved, deploy TEST-project.", when="2026-09-05T10:00:00Z")
    build(core, ctx)
    packet, rendered = rendered_conversation(core, ctx)
    assert any(i["ref"] == source.ref for i in packet["items"])
    assert not any(i.get("qualification_reason") == "grounded_session_summary" for i in packet["items"])
    assert rendered.count("If approved, deploy TEST-project.") == 1


@pytest.mark.parametrize(
    "raw,expected_summary",
    [("TEST-project chose blue.", True), ("TEST-project chose blue. Rollback takes three days.", False)],
)
def test_summary_compares_only_displayed_original_content(app, raw, expected_summary):
    from scope_recall.core.retrieval import CandidateRef, SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    capture(core, ctx, "TEST-project chose blue. Rollback takes three days.")
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts LIMIT 1").fetchone()
        context = SearchContext.from_request(
            recall_request(), ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 10
        )
        summary = RetrievalStorage(clock=core.clock).hydrate(tx, CandidateRef("claim", *row, "scoped"), context)
    assert summary is not None
    shown = _said("TEST-displayed-source", raw)
    packet = _compiled(core, ctx, "TEST-project", [("scoped", summary), ("lexical", shown)])
    assert any(i["ref"] == summary.ref for i in packet["items"]) == expected_summary
    if expected_summary:
        assert "Rollback takes three days." in next(i["content"] for i in packet["items"] if i["ref"] == summary.ref)


@pytest.mark.parametrize("weakest", ["human_direct", "assistant_visible", "tool_observation"])
@pytest.mark.parametrize("imported", [False, True])
def test_summary_origin_is_its_weakest_member(app, weakest, imported):
    from scope_recall.contracts import ImportProvenance, import_source_fingerprint
    from scope_recall.core.retrieval import CandidateRef, SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    task = replace(ctx, task_anchor="TEST-origin-task")
    capture(core, task, "Implement TEST-project.", when="2026-09-05T10:00:00Z")
    role = {"human_direct": "user", "assistant_visible": "assistant", "tool_observation": "tool"}[weakest]
    origin = "imported" if imported else weakest
    event = source_event(
        source_event_key="TEST-weakest-member",
        origin=origin,
        role=role,
        content="TEST-project check returned zero.",
        occurred_at="2026-09-05T11:01:00Z",
        **({"source_original_origin": weakest} if imported else {}),
    )
    provenance = (
        ImportProvenance(weakest, "a" * 64, frozenset({import_source_fingerprint(event)})) if imported else None
    )
    core.record_event(
        replace(task, actor_origin=origin, import_provenance=provenance),
        event,
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    capture(core, task, "The task is completed.", when="2026-09-05T11:02:00Z")
    build(core, task)
    with core.storage.read(ctx) as tx:
        rows = (
            tx._check()
            .execute(
                "SELECT p.claim_ref,p.claim_revision FROM summary_parts p "
                "JOIN summary_groups g USING(group_key) WHERE g.kind='task'"
            )
            .fetchall()
        )
        context = SearchContext.from_request(
            recall_request(), ctx, now=core.clock.utc_now(), deadline=core.clock.monotonic() + 10
        )
        objects = [
            RetrievalStorage(clock=core.clock).hydrate(tx, CandidateRef("claim", *r, "scoped"), context) for r in rows
        ]
    assert objects and all(o is not None and o.origin == weakest for o in objects)


def test_summary_claim_cannot_be_a_fact_evidence_root(app):
    from scope_recall.contracts import ContractError
    from scope_recall.core.evidence_question import DERIVATION_ROOT_ORIGINS
    from scope_recall.core.worker_consolidation import _root_only_sources
    from test_claims import accept, draft

    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    build(core, ctx)
    with core.storage.read(ctx) as tx:
        ref, revision = tx._check().execute("SELECT claim_ref,claim_revision FROM summary_parts LIMIT 1").fetchone()
        assert tx.source(ref, revision) is None
        assert _root_only_sources(tx, (source,)) == (source,)
    assert "derived_summary" not in DERIVATION_ROOT_ORIGINS
    proposal = draft(source)
    proposal["evidence_spans"][0].update(source_ref=ref, source_revision=revision)
    with pytest.raises(ContractError):
        accept(core, ctx, proposal)


def _quoted_summary(ref, *quotes):
    """An already released summary shape for deterministic packet budget tests."""
    from scope_recall.core.retrieval import RetrievedObject

    return RetrievedObject(
        ref,
        1,
        "claim",
        "\n".join("user: " + quote for quote in quotes),
        "human_direct",
        "current",
        "trusted scope",
        ("TEST-source@1",),
        "derived_summary",
        True,
        metadata=(
            ("qualification_reason", "grounded_session_summary"),
            ("payload_json", json.dumps({"evidence_spans": [{"quote": q} for q in quotes]})),
        ),
    )


def test_summary_slots_refill_earlier_budget_rejections(app):
    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    first = _quoted_summary("TEST-summary-one", "TEST-project approved.")
    second = _quoted_summary("TEST-summary-two", "TEST-project released.")
    detail = _said("TEST-extra", "Rollback deadline: three days.")
    original = _said("TEST-original", "TEST-project approved. TEST-project released.")
    found = [("scoped", first), ("scoped", second), ("lexical", detail), ("lexical", original)]
    packet = _compiled(core, ctx, "TEST-project", found, max_items=3)
    assert {i["ref"] for i in packet["items"]} == {detail.ref, original.ref}


@pytest.mark.parametrize("length", [11000, 13000])
def test_unshippable_original_never_suppresses_the_fitting_summary(app, length):
    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    quote = "TEST-project approved."
    summary = _quoted_summary("TEST-summary-fallback", quote)
    original = _said("TEST-too-large-original", quote + "x" * length)
    packet = _compiled(core, ctx, "TEST-project", [("scoped", summary), ("lexical", original)], budget_tokens=2850)
    assert any(i["ref"] == summary.ref for i in packet["items"])
    assert not any(i["ref"] == original.ref for i in packet["items"])


def test_continuous_excerpt_requires_one_displayed_original_to_contain_it(app):
    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    summary = _quoted_summary("TEST-multi-sentence-summary", "TEST-project approved. TEST-project released.")
    first = _said("TEST-approved", "TEST-project approved.")
    second = _said("TEST-released", "TEST-project released.")
    packet = _compiled(core, ctx, "TEST-project", [("scoped", summary), ("lexical", first), ("lexical", second)])
    assert {i["ref"] for i in packet["items"]} == {summary.ref, first.ref, second.ref}


def test_partial_original_delivery_preserves_the_complete_summary(app):
    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    summary = _quoted_summary("TEST-complete-summary", "TEST-first detail.", "TEST-second detail.")
    first = _said("TEST-large-first-original", "TEST-first detail." + "x" * 11000)
    second = _said("TEST-rejected-second-original", "TEST-second detail." + "x" * 13000)
    packet = _compiled(
        core, ctx, "TEST-project", [("scoped", summary), ("lexical", first), ("lexical", second)], budget_tokens=3000
    )
    assert any(i["ref"] == summary.ref for i in packet["items"])
    assert "TEST-second detail." in " ".join(i["content"] for i in packet["items"])


def test_covered_summary_outside_the_item_cap_is_not_an_answer_drop(app):
    from tests.contract.test_recall_packet_compiler import _compiled, _said

    core, ctx = app
    summary = _quoted_summary("TEST-covered-summary", "TEST-project approved.")
    original = _said("TEST-only-original", "TEST-project approved.")
    packet = _compiled(core, ctx, "TEST-project", [("scoped", summary), ("lexical", original)], max_items=1)
    assert [i["ref"] for i in packet["items"]] == [original.ref]
    assert packet["status"] == "ok" and packet["answerability"] == "supported"
    assert not any(gap.startswith("budget_") for gap in packet["gaps"])


def test_uncompiled_recall_keeps_its_existing_item_limit(app):
    from datetime import UTC

    core, ctx = app
    capture(core, ctx, "TEST-project approved.", when="2026-09-05T10:00:00Z")
    capture(core, ctx, "TEST-project released.", when="2026-09-05T11:00:00Z")
    build(core, ctx)
    result = core.recall(ctx, recall_request(query="昨天做了什么", max_items=1), deadline_seconds=10, zone=UTC)
    assert len(result.items) <= 1
