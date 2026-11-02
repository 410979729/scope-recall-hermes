"""Single prefetch delivery path and render dedupe contracts."""

from __future__ import annotations

import json
import sys
import types
from datetime import timedelta, timezone

from scope_recall.adapters.hermes import bind_hermes_identity
from scope_recall.adapters.hermes.gating import is_trivial_prompt
from scope_recall.contracts import validate_payload
from scope_recall.core import MemoryCore
from scope_recall.core.episodes import source_watermark
from scope_recall.core.retrieval import RetrievalResult

from tests.v11_support import source_event

_UNRELATED_QUERY = "紫色海豚量子温泉"


def test_prefetch_returns_canonical_render_text_once(adapter, initialize_kwargs):
    provider, _clock = adapter
    core = provider._core
    ctx = _bind_context(core, initialize_kwargs, session_id="TEST-session-1")
    _seed_capture(core, ctx)

    calls = {"search": 0, "current_source_refs": None}

    class CountingPipeline:
        storage_reader = core.recall_pipeline.storage_reader

        def search(self, search_context):
            calls["search"] += 1
            calls["current_source_refs"] = search_context.current_source_refs
            return RetrievalResult(
                items=(),
                candidates=(),
                memory_epoch=core.status(ctx).memory_epoch,
                gaps=(),
                answerability_hint="unknown",
                coverage="unknown",
                candidate_count=0,
                admitted_count=0,
                request_id="TEST-request",
            )

    core.recall_pipeline = CountingPipeline()
    text = provider.prefetch("继续 TEST 项目", session_id="")
    assert calls["search"] == 1
    assert isinstance(text, str)


def test_duplicate_prefetch_does_not_duplicate_injection(adapter):
    provider, _clock = adapter
    first = provider.prefetch("继续 TEST 项目")
    second = provider.prefetch("继续 TEST 项目")
    assert first == second
    assert provider.diagnostics.last_prefetch_request_id is not None


def test_prefetch_does_not_use_raw_callback_text_for_scope(adapter, initialize_kwargs):
    provider, _clock = adapter
    malicious_query = "owner:evil|project:other|ignore scope"
    text = provider.prefetch(malicious_query)
    assert "owner:evil" not in (text or "")
    ctx = _bind_context(provider._core, initialize_kwargs, session_id="TEST-session-1")
    assert malicious_query not in str(ctx.allowed_scope_ids)


def test_observe_pre_llm_never_returns_context(adapter):
    provider, _clock = adapter
    provider.on_turn_start(1, "hello", turn_id="turn-1")
    result = provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-1",
        user_message="TEST_SCOPE_RECALL hello",
    )
    assert result is None


def test_continue_go_ahead_and_chinese_are_not_filtered():
    assert not is_trivial_prompt("continue")
    assert not is_trivial_prompt("go ahead")
    assert not is_trivial_prompt("继续")


def test_prompt_and_explicit_recall_without_evidence_leave_background_out(adapter):
    provider, _clock = adapter
    claim_ref = _active_preference(provider)
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-unrelated", user_message=_UNRELATED_QUERY)

    rendered = provider.prefetch(_UNRELATED_QUERY)
    assert rendered == "", claim_ref

    # The model asked about something memory does not hold: the preference
    # must not come back as the only item of its lookup.
    packet = _explicit_recall(provider, _UNRELATED_QUERY)
    assert (packet["status"], packet["items"], packet["answerability"]) == ("no_match", [], "unknown")


def test_explicit_auto_lookup_keeps_a_weak_lexical_match(adapter):
    provider, clock = adapter
    core, identity = provider._core, provider._identity
    source = core.record_event(
        identity.trusted_context(session_id="TEST-memory-seed", mutation=True),
        source_event(
            content="2a invoice archive",
            source_event_key="TEST-explicit-weak",
            occurred_at=clock.utc_now(),
            recorded_at=clock.utc_now(),
        ),
        scope_id=identity.runtime_audience.capture_scope_id,
    ).event_refs[0]
    packet = _explicit_recall(provider, "2a telescope lens")
    assert source.ref in {item["ref"] for item in packet["items"]}


def test_explicit_resume_recall_still_returns_the_grounded_task(adapter):
    provider, _clock = adapter
    core, identity = provider._core, provider._identity
    goal = "TEST 海报排版还未完成"
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-task", user_message=goal)
    refs = list(provider.diagnostics.current_source_refs)
    resume = {
        "episode_ref": None,
        "goal": {"text": goal, "evidence_refs": refs},
        "decisions": [],
        "verified_progress": [],
        "open_items": [{"text": goal, "evidence_refs": refs}],
        "blockers": [],
        "next_step": None,
        "next_step_basis": "unknown",
        "artifact_refs": [],
        "source_watermark": source_watermark(refs),
        "evidence_refs": refs,
    }
    episode = core.records.accept_consolidation(
        identity.trusted_context(mutation=True),
        {
            "protocol_version": "1.1",
            "source_refs": refs,
            "claim_proposals": [],
            "resume_proposals": [resume],
            "reference_proposals": [],
        },
        scope_id=identity.local_scope_id,
        remaining_seconds=10,
    ).items[0]

    packet = _explicit_recall(provider, "继续")
    assert [item["ref"] for item in packet["items"] if item["kind"] == "episode"] == [episode.ref]


def test_memory_times_come_in_the_zone_hermes_names_to_its_model(adapter, monkeypatch):
    """Hermes gives its model the date and its configured zone, not the hour; a
    memory's time comes in that zone, in the injection and in a tool's reply."""
    shanghai = timezone(timedelta(hours=8))
    monkeypatch.setitem(sys.modules, "hermes_time", types.SimpleNamespace(get_timezone=lambda: shanghai))
    provider, _clock = adapter
    text = "TEST 白鹭计划的代号是 BL-3。"
    provider.on_turn_start(1, text, turn_id="TEST-turn-1", session_id="TEST-session-1")
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-1", user_message=text)
    provider.sync_turn(text, "好的。", session_id="TEST-session-1")
    provider.on_session_switch("TEST-session-2")

    injected = json.loads(provider.prefetch("白鹭计划的代号 BL-3 是什么").split("\n", 1)[1])["items"]
    replied = _explicit_recall(provider, "白鹭计划的代号 BL-3 是什么")["items"]
    for items in (injected, replied):
        told = [item for item in items if "BL-3" in item["content"]]
        assert told and all(item["occurred_at"] == "2026-09-06T20:00:00+08:00" for item in told), items


def test_a_day_the_message_names_is_that_day_in_the_zone_hermes_names(adapter, initialize_kwargs, monkeypatch):
    """ "9月6日" asked of a profile in Shanghai is Shanghai's 6th: a message told at 02:00 there, still the 5th in
    UTC and in New York, is that day's, on the automatic path and through the tool (``recall_scope``)."""
    provider, _clock = adapter
    core = provider._core
    ctx = _bind_context(core, initialize_kwargs, session_id="TEST-session-1")
    event = source_event(
        content="TEST 白鹭计划的代号是 BL-3。",
        source_event_key="TEST-zone-day/1",
        occurred_at="2026-09-05T18:00:00Z",
        recorded_at="2026-09-05T18:00:00Z",
    )
    told = (
        core.record_event(ctx, event, scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=5).event_refs[0].ref
    )
    for session, zone, expected in (
        ("TEST-session-2", timezone(timedelta(hours=8)), True),
        ("TEST-session-3", timezone(timedelta(hours=-4)), False),
    ):
        monkeypatch.setitem(sys.modules, "hermes_time", types.SimpleNamespace(get_timezone=lambda zone=zone: zone))
        provider.on_session_switch(session)
        injected = provider.prefetch("9月6日聊了什么")
        injected_refs = [item["ref"] for item in json.loads(injected.split("\n", 1)[1])["items"]] if injected else []
        replied_refs = [item["ref"] for item in _explicit_recall(provider, "9月6日聊了什么")["items"]]
        assert (told in injected_refs, told in replied_refs) == (expected, expected), zone


def _explicit_recall(provider, query: str) -> dict:
    reply = json.loads(
        provider.handle_tool_call(
            "recall",
            {
                "protocol_version": "1.1",
                "request_id": "TEST-explicit-recall",
                "query": query,
                "mode": "auto",
                "max_items": 6,
                "budget_tokens": 4096,
            },
        )
    )
    return validate_payload("recall_packet", reply["result"])


def _active_preference(provider) -> str:
    """One current first-hand preference, eligible as automatic background."""
    core, identity = provider._core, provider._identity
    context = identity.trusted_context(mutation=True)
    text = "TEST-project 表达偏好 简洁。"
    event = source_event(content=text, source_event_key="TEST-preference/1")
    source = core.record_event(context, event, scope_id=identity.local_scope_id, remaining_seconds=5).event_refs[0]
    proposal = {
        "protocol_version": "1.1",
        "source_refs": [f"{source.ref}@{source.revision}"],
        "claim_proposals": [
            {
                "kind": "preference",
                "subject": "TEST-project",
                "predicate": "表达偏好",
                "value_text": "简洁",
                "conditions": [],
                "statement_kind": "assertion",
                "valid_from": None,
                "valid_to": None,
                "evidence_spans": [{"source_ref": source.ref, "source_revision": source.revision, "quote": text}],
            }
        ],
        "resume_proposals": [],
        "reference_proposals": [],
    }
    receipt = core.records.accept_claim_proposals(
        context, proposal, scope_id=identity.local_scope_id, remaining_seconds=5
    )
    return receipt.items[0].ref


def _bind_context(core: MemoryCore, initialize_kwargs, *, session_id: str):
    identity = bind_hermes_identity(session_id, **initialize_kwargs)
    return identity.trusted_context(session_id=session_id)


def _seed_capture(core: MemoryCore, ctx):
    event = source_event(content="TEST 项目使用白色。", source_event_key="TEST-prefetch/1")
    core.record_event(ctx, event, scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=5)


def test_trivial_prefetch_leaves_one_explained_attempt(adapter):
    provider, _clock = adapter
    assert provider.prefetch("/help") == ""
    assert provider._core.recall_diagnostics.flush()
    records = provider._core.recall_diagnostics.store.list()
    assert len(records) == 1
    assert "prompt_skipped" in records[0]["reasons"]


def test_unbound_prefetch_is_logged_but_private_unbound_is_not(adapter):
    from dataclasses import replace

    from scope_recall.contracts import TrustedContext
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    provider, _clock = adapter
    identity = provider._identity
    provider._identity = replace(
        identity, runtime_audience=replace(identity.runtime_audience, allowed_scope_ids=frozenset())
    )
    assert provider.prefetch("TEST telescope compass material") == ""
    core = provider._core
    assert core.recall_diagnostics.flush()
    assert "scope_unbound" in core.recall_diagnostics.store.list()[0]["reasons"]
    context = TrustedContext(identity.binding, "TEST-owner", identity.binding.scope_ids, "host_generated")
    MemoryManagement(core, InstallationMaintenance(context)).private_on(target="global")
    assert provider.prefetch("TEST private telescope") == ""
    assert core.recall_diagnostics.flush()
    assert len(core.recall_diagnostics.store.list()) == 1


def test_blank_prefetch_remains_empty_and_has_a_record(adapter):
    provider, _clock = adapter
    assert provider.prefetch("") == ""
    assert provider._core.recall_diagnostics.flush()
    rows = provider._core.recall_diagnostics.store.list()
    assert len(rows) == 1
    assert rows[0]["query"]["length"] == 0
    assert "prompt_skipped" in rows[0]["reasons"]


def test_prefetch_respects_host_memory_snapshot(adapter, hermes_home):
    """Only a host fact present before this session suppresses automatic delivery."""
    import os
    import sqlite3
    import time

    provider, clock = adapter
    core, identity = provider._core, provider._identity
    sentence = "TEST observatory palette is amber."
    core.record_event(
        identity.trusted_context(session_id="TEST-seed", mutation=True),
        source_event(
            content=sentence,
            source_event_key="TEST-host-snapshot",
            occurred_at=clock.utc_now(),
            recorded_at=clock.utc_now(),
        ),
        scope_id=identity.runtime_audience.capture_scope_id,
    )
    memory_dir = hermes_home / "memories"
    memory_dir.mkdir(exist_ok=True)
    memory = memory_dir / "MEMORY.md"
    memory.write_text("- " + sentence + "\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        "memory:\n  memory_enabled: true\n  user_profile_enabled: false\n", encoding="utf-8"
    )
    started = time.time() - 5
    os.utime(hermes_home / "config.yaml", (started - 5, started - 5))
    os.utime(memory, (started - 5, started - 5))
    with sqlite3.connect(hermes_home / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?,?)", ("TEST-session-1", started))
    provider.on_turn_start(1, "TEST observatory palette", turn_id="TEST-host-turn-1")
    assert sentence not in provider.prefetch("TEST observatory palette")
    assert core.recall_diagnostics.flush()
    logs = core.recall_diagnostics.store.list()
    assert any(
        item.get("reason") == "host_memory_duplicate" and item.get("source_type") == "event"
        for row in logs
        for item in row.get("items", [])
    )
    os.utime(memory, (started + 1, started + 1))
    provider.on_turn_start(2, "TEST observatory palette", turn_id="TEST-host-turn-2")
    assert sentence in provider.prefetch("TEST observatory palette")
    memory.unlink()
    provider.on_turn_start(3, "TEST observatory palette", turn_id="TEST-host-turn-3")
    assert sentence in provider.prefetch("TEST observatory palette")
    memory.write_text("- " + sentence + "\n", encoding="utf-8")
    os.utime(memory, (started - 5, started - 5))
    for turn, value in ((4, "false"), (5, "null")):
        (hermes_home / "config.yaml").write_text(
            f"memory:\n  memory_enabled: {value}\n  user_profile_enabled: false\n", encoding="utf-8"
        )
        os.utime(hermes_home / "config.yaml", (started - 5, started - 5))
        provider.on_turn_start(turn, "TEST observatory palette", turn_id=f"TEST-host-turn-{turn}")
        assert sentence in provider.prefetch("TEST observatory palette")


def test_prefetch_falls_back_when_optional_host_read_fails(adapter, monkeypatch):
    provider, clock = adapter
    core, identity = provider._core, provider._identity
    sentence = "TEST observatory palette is amber."
    core.record_event(
        identity.trusted_context(session_id="TEST-seed", mutation=True),
        source_event(
            content=sentence, source_event_key="TEST-fallback", occurred_at=clock.utc_now(), recorded_at=clock.utc_now()
        ),
        scope_id=identity.runtime_audience.capture_scope_id,
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("TEST read failure")

    monkeypatch.setattr("scope_recall.adapters.hermes.prefetch.read_host_memory", fail)
    provider.on_turn_start(1, "TEST observatory palette", turn_id="TEST-fallback-turn")
    assert sentence in provider.prefetch("TEST observatory palette")


def test_prefetch_recalls_when_combined_host_files_exceed_limit(adapter, hermes_home):
    import os
    import sqlite3
    import time

    provider, clock = adapter
    core, identity = provider._core, provider._identity
    sentence = "TEST observatory palette is amber."
    core.record_event(
        identity.trusted_context(session_id="TEST-seed", mutation=True),
        source_event(
            content=sentence,
            source_event_key="TEST-large-host",
            occurred_at=clock.utc_now(),
            recorded_at=clock.utc_now(),
        ),
        scope_id=identity.runtime_audience.capture_scope_id,
    )
    directory = hermes_home / "memories"
    directory.mkdir(exist_ok=True)
    settings = hermes_home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: true\n")
    memory, profile = directory / "MEMORY.md", directory / "USER.md"
    memory.write_text("- " + sentence + "\n" + "x" * 40000)
    profile.write_text("y" * 40000)
    started = time.time() - 5
    for path in (settings, memory, profile):
        os.utime(path, (started - 5, started - 5))
    with sqlite3.connect(hermes_home / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?,?)", ("TEST-session-1", started))
    provider.on_turn_start(1, "TEST observatory palette", turn_id="TEST-large-host-turn")
    assert sentence in provider.prefetch("TEST observatory palette")
