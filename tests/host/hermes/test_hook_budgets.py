"""Slow store transactions defer captures without discarding their original envelopes."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace

import pytest
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, capture_retry, turn_capture
from scope_recall.contracts import ContractError
from scope_recall.core import MemoryCore, capture_inbox

from tests.host.hermes.test_runtime_bounds import _tool_result, _until
from tests.host.hermes.test_steers import _row, _steer, telegram  # noqa: F401


def keep_captures(provider, monkeypatch, count):
    """Keep synthetic tool results as a busy writer lease does."""
    real = provider._core.record_host_event

    def refused(*args, **kwargs):
        raise ContractError("DEADLINE_EXCEEDED", "TEST writer held")

    monkeypatch.setattr(provider._retry, "start", lambda: None)
    monkeypatch.setattr(provider._core, "record_host_event", refused)
    for number in range(count):
        _tool_result(provider, "turn-1", f"kept-{number}", f"TEST kept output {number}")
    monkeypatch.setattr(provider._core, "record_host_event", real)
    assert len(provider._retry.captures) == count


def stored_words(core):
    with sqlite3.connect(core.storage.path) as connection:
        return {row[0] for row in connection.execute("SELECT content FROM source_events")}


def inbox_words(core):
    """Words the durable inbox has accepted and not yet committed as source events."""
    with sqlite3.connect(core.storage.path) as connection:
        payloads = connection.execute("SELECT payload_json FROM capture_inbox").fetchall()
    return {
        event["content"]
        for (payload,) in payloads
        for event in json.loads(payload)["events"]
        if isinstance(event.get("content"), str)
    }


def inbox_pending(core) -> int:
    """Rows still waiting in the durable inbox."""
    with sqlite3.connect(core.storage.path) as connection:
        return connection.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]


def accounted_words(core, provider):
    """Words a capture pass can still recover: committed sources, the memory retry, or the durable inbox.

    The inbox commits before the source write. When that write does not fit, its receipt drops the memory copy,
    so sources and memory alone miss a capture the store has already kept. Callers keep retry threads stopped
    and use an unconfigured worker runtime, so these reads cannot race a background replay.
    """
    kept = {pending.event["content"] for pending in provider._retry.captures.values()}
    return stored_words(core) | kept | inbox_words(core)


@pytest.mark.parametrize("per_operation", [0.25, 1.0])
def test_compression_defers_steers_within_shared_budget(telegram, monkeypatch, metered_store, per_operation):  # noqa: F811
    monkeypatch.setattr(telegram._retry, "start", lambda: None)
    clock = metered_store(per_operation)
    assert clock is metered_store(per_operation), "semantic and budget fixtures share one transaction clock"
    before = clock.operations
    started = telegram._core.clock.monotonic()
    with telegram._core.storage.read(telegram._require_identity().trusted_context()):
        pass
    assert clock.operations == before + 1, "one transaction is charged once"
    assert per_operation <= telegram._core.clock.monotonic() - started <= per_operation + 0.1
    rows = [_row(_steer(f"TEST steer {number}", message_id=str(300 + number))) for number in range(8)]
    started = clock()
    telegram.on_pre_compress([dict(row) for row in rows])
    elapsed = clock() - started
    assert elapsed <= turn_capture._STEERS_SECONDS + 0.1, (per_operation, elapsed)
    wanted = {f"TEST steer {number}" for number in range(8)}
    assert wanted <= accounted_words(telegram._core, telegram)
    clock.per_operation = 0.01
    telegram._retry.write_observed(deadline=clock() + 10)
    telegram._retry.write_buffered(seconds=10)
    assert wanted <= stored_words(telegram._core)
    assert inbox_pending(telegram._core) == 0


def test_inbox_keeps_a_steer_when_its_source_write_does_not_fit(telegram, monkeypatch, metered_store):
    """A steer the inbox has committed stays accounted for when the source write does not fit.

    A delay after enqueue leaves insufficient admission time for the source commit while staying inside the hook
    budget. The receipt drops the memory copy; replay commits the source and leaves the inbox empty.
    """
    monkeypatch.setattr(telegram._retry, "start", lambda: None)
    clock = metered_store(0.25)
    real_enqueue = capture_inbox.enqueue
    stalled = False

    def enqueue_then_source_does_not_fit(*args, **kwargs):
        nonlocal stalled
        token, prepared = real_enqueue(*args, **kwargs)
        if not stalled:
            stalled = True
            clock.advance(0.25)
        return token, prepared

    monkeypatch.setattr(capture_inbox, "enqueue", enqueue_then_source_does_not_fit)
    rows = [_row(_steer(f"TEST steer {number}", message_id=str(300 + number))) for number in range(8)]
    started = clock()
    telegram.on_pre_compress([dict(row) for row in rows])
    elapsed = clock() - started
    assert elapsed <= turn_capture._STEERS_SECONDS + 0.1, elapsed
    wanted = {f"TEST steer {number}" for number in range(8)}
    stored = stored_words(telegram._core)
    kept = {pending.event["content"] for pending in telegram._retry.captures.values()}
    missing = wanted - (stored | kept)
    assert missing
    assert missing <= inbox_words(telegram._core)
    assert wanted <= accounted_words(telegram._core, telegram)
    clock.per_operation = 0.01
    telegram._retry.write_observed(deadline=clock() + 10)
    telegram._retry.write_buffered(seconds=10)
    assert wanted <= stored_words(telegram._core)
    assert inbox_pending(telegram._core) == 0


def test_session_end_retries_and_steers_share_one_capture_pass(telegram, monkeypatch, metered_store):
    """Pending retries and the steers read at session end share one 2 s capture pass.

    The worker start inside the session end is a separate budget. What this pass cannot store stays in the retry
    buffer, or in the durable inbox when its source write does not fit, and is written once the store has time.
    """
    monkeypatch.setattr(telegram._retry, "start", lambda: None)
    real = telegram._core.record_host_event

    def refused(*args, **kwargs):
        raise ContractError("DEADLINE_EXCEEDED", "TEST writer held")

    monkeypatch.setattr(telegram._core, "record_host_event", refused)
    telegram.observe_post_tool_call(
        session_id="TEST-session-tg",
        turn_id="turn-kept",
        tool_call_id="kept-session-end",
        tool_name="terminal",
        result="TEST kept before session end",
        status="success",
    )
    monkeypatch.setattr(telegram._core, "record_host_event", real)
    assert telegram._retry.captures
    # The worker's own start is not this capture pass.
    monkeypatch.setattr(telegram._worker, "submit", lambda *args, **kwargs: False)
    clock = metered_store(1.0)
    rows = [_row(_steer(f"TEST session steer {number}", message_id=str(400 + number))) for number in range(8)]
    started = clock()
    try:
        telegram.on_session_end([dict(row) for row in rows])
        elapsed = clock() - started
    finally:
        clock.per_operation = 0.01
    assert elapsed <= turn_capture._STEERS_SECONDS + 0.1, (elapsed, clock.operations)
    wanted = {f"TEST session steer {number}" for number in range(8)} | {"TEST kept before session end"}
    assert wanted <= accounted_words(telegram._core, telegram)
    telegram._retry.write_observed(deadline=clock() + 10)
    telegram._retry.write_buffered(seconds=10)
    assert wanted <= stored_words(telegram._core)
    assert inbox_pending(telegram._core) == 0


@pytest.mark.parametrize("per_operation", [0.5, 1.5])
def test_shutdown_keeps_unstored_captures_for_restart(
    adapter, initialize_kwargs, monkeypatch, metered_store, kept_capture_seconds, caplog, per_operation
):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 4)
    expected = {key: pending for key, pending in provider._retry.captures.items()}
    clock = metered_store(per_operation)
    started = clock()
    provider.shutdown()
    elapsed = clock() - started - kept_capture_seconds()
    assert elapsed <= capture_retry.SHUTDOWN_RETRY_SECONDS + 0.1, (per_operation, elapsed)
    assert not any("lost" in record.getMessage() for record in caplog.records)
    assert provider.diagnostics.shutdown_state["pending_capture_durability"] == "on_disk"
    unstored = set(provider._retry.on_disk)
    # A new core and adapter have no retry state from the stopped process.
    clock.per_operation = 0.01
    core = MemoryCore(provider._core.config, clock=provider._core.clock)
    restarted = ScopeRecallHermesAdapter(core=core, clock=core.clock)
    monkeypatch.setattr(restarted._retry, "start", lambda: None)
    restarted.initialize("TEST-next-session", **initialize_kwargs)
    try:
        assert set(restarted._retry.captures) == unstored
        for key, pending in restarted._retry.captures.items():
            assert pending.event == expected[key].event
            assert pending.context == expected[key].context
        restarted._retry.write_buffered(seconds=10)
        restarted._retry.write_observed(deadline=clock() + 10)
        assert {f"TEST kept output {number}" for number in range(4)} <= stored_words(core)
        assert not restarted._retry.captures
        assert not list((core.config.binding.data_directory / "kept-captures").glob("*.json"))
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("per_operation", [0.5, 1.5])
def test_turn_own_capture_reaches_store_before_older_retry(adapter, monkeypatch, metered_store, per_operation):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 1)
    clock = metered_store(per_operation)
    real, seen = provider._core.source_by_event_key, []

    def lookup(context, key, *args, **kwargs):
        seen.append((key, clock()))
        return real(context, key, *args, **kwargs)

    monkeypatch.setattr(provider._core, "source_by_event_key", lookup)
    started = clock()
    provider.sync_turn("TEST own user words", "TEST own reply words", session_id="TEST-session-1")
    assert seen and "kept-" not in seen[0][0]
    assert seen[0][1] - started <= 0.1


def test_shutdown_disk_failure_retains_memory_capture(adapter, monkeypatch, metered_store, caplog):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 1)
    clock = metered_store(1.5)

    def failed_sync(descriptor):
        raise OSError("TEST disk unavailable")

    monkeypatch.setattr(os, "fsync", failed_sync)
    provider.shutdown()
    assert len(provider._retry.captures) == 1
    state = provider.diagnostics.shutdown_state
    assert state["pending_capture_status"] == "unpersisted"
    assert state["pending_capture_durability"] == "memory_only"
    assert any("could not be written to disk" in record.getMessage() for record in caplog.records)
    assert any("retained in memory" in record.getMessage() for record in caplog.records)
    folder = provider._core.config.binding.data_directory / "kept-captures"
    assert not list(folder.glob("*.json"))
    assert not any("lost" in record.getMessage() for record in caplog.records)
    clock.per_operation = 0.01


def test_retry_expiry_keeps_capture_on_disk(adapter, monkeypatch, metered_store):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 1)
    clock = metered_store(0.01)
    clock.advance(capture_retry._RETRY_GIVE_UP_S + 1)
    with provider._lock:
        provider._retry.give_up_expired(tuple(provider._retry.captures.items()))
    assert not provider._retry.captures
    assert len(list((provider._core.config.binding.data_directory / "kept-captures").glob("*.json"))) == 1


@pytest.mark.parametrize("capacity", [2, 16])
def test_restart_writes_kept_captures_without_a_host_hook(
    adapter, initialize_kwargs, monkeypatch, metered_store, capacity
):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 4)
    clock = metered_store(1.5)
    provider.shutdown()
    monkeypatch.setattr(capture_retry, "RETRY_CAPACITY", capacity, raising=False)
    monkeypatch.setattr(capture_retry, "_RETRY_EVERY_S", 0.01)
    clock.per_operation = 0.01
    core = MemoryCore(provider._core.config, clock=provider._core.clock)
    restarted = ScopeRecallHermesAdapter(core=core, clock=core.clock)
    restarted.initialize("TEST-next-session", **initialize_kwargs)
    try:
        expected = {f"TEST kept output {number}" for number in range(4)}
        assert _until(lambda: expected <= stored_words(core), seconds=10)
    finally:
        restarted.shutdown()


def test_buffer_overflow_preserves_capture_on_disk(adapter, monkeypatch):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 16)
    monkeypatch.setattr(
        provider._core,
        "record_host_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(ContractError("DEADLINE_EXCEEDED")),
    )
    _tool_result(provider, "turn-1", "overflow", "TEST overflow output")
    assert len(provider._retry.captures) == 16
    assert len(list((provider._core.config.binding.data_directory / "kept-captures").glob("*.json"))) == 1


def test_unreadable_spool_file_is_reported_and_retained(adapter, initialize_kwargs, caplog):
    provider, _clock = adapter
    folder = provider._core.config.binding.data_directory / "kept-captures"
    folder.mkdir()
    path = folder / "TEST-unreadable.json"
    path.write_text("{", encoding="utf-8")
    restarted = ScopeRecallHermesAdapter(core=provider._core, clock=provider._core.clock)
    restarted.initialize("TEST-next-session", **initialize_kwargs)
    try:
        assert path.read_text(encoding="utf-8") == "{"
        assert any("could not be read" in record.getMessage() for record in caplog.records)
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("refusal", ["revoked", "invalid"])
def test_terminal_refusal_removes_spooled_capture(adapter, initialize_kwargs, monkeypatch, metered_store, refusal):
    provider, _clock = adapter
    keep_captures(provider, monkeypatch, 1)
    clock = metered_store(1.5)
    provider.shutdown()
    folder = provider._core.config.binding.data_directory / "kept-captures"
    assert len(list(folder.glob("*.json"))) == 1
    clock.per_operation = 0.01
    core = MemoryCore(provider._core.config, clock=provider._core.clock)
    restarted = ScopeRecallHermesAdapter(core=core, clock=core.clock)
    monkeypatch.setattr(restarted._retry, "start", lambda: None)
    restarted.initialize("TEST-next-session", **initialize_kwargs)
    if refusal == "revoked":
        real = capture_retry.resolve_runtime_audience
        monkeypatch.setattr(
            capture_retry,
            "resolve_runtime_audience",
            lambda *args: replace(real(*args), writable_scope_ids=frozenset()),
        )
    else:

        def invalid(*args, **kwargs):
            raise ContractError("INPUT_INVALID", "TEST envelope")

        monkeypatch.setattr(core, "record_host_event", invalid)
    try:
        restarted._retry.write_buffered(seconds=10)
        assert not restarted._retry.captures
        assert not list(folder.glob("*.json"))
        assert "TEST kept output 0" not in stored_words(core)
    finally:
        restarted.shutdown()
