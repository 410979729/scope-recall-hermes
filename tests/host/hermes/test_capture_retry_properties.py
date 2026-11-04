"""Generated capture replay failures, disk cooldowns and recovery retain original identities."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from scope_recall.adapters.hermes import capture_retry, kept_captures
from scope_recall.adapters.hermes.capture import RetryCapture
from scope_recall.contracts import ContractError
from scope_recall.core.deadline import fits
from scope_recall.core.failure_policy import HERMES_DISK_RETRY
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import _capture_queue_checks
from v11_support import source_event

from tests.queue_state_support import QUEUE_SETTINGS, check_transitions, state_table


def _check_replay_state(provider, retry, previous, cancelled, successes, elapsed):
    """Check real replay removals, request outcomes, cooldowns and doctor-visible disk backlog."""
    assert set(state_table("Hermes capture replay")) == {
        "buffered",
        "in_flight",
        "kept",
        "completed",
        "cancelled",
    }
    assert len(retry.captures) <= 16
    current = {key: "kept" for key in retry.on_disk.keys() - retry.captures.keys()}
    current.update({key: "buffered" for key in retry.captures})
    removed = {}
    for key in previous.keys() - current.keys():
        if key in cancelled:
            removed[key] = "cancelled"
        else:
            assert key[0] in successes
            removed[key] = "completed"
    check_transitions(state_table("Hermes capture replay"), previous, current | removed)
    next_state = current
    if retry.given_up:
        assert retry.disk_retry_at is not None
        assert retry.disk_retry_at <= elapsed + HERMES_DISK_RETRY.delay()
    context = provider._require_identity().trusted_context()
    with provider._core.storage.read(context) as tx:
        report = DoctorReport("hermes", "ok")
        _capture_queue_checks(report, context.binding, tx._check())
    if retry.on_disk:
        assert "capture_replay" in str(report.checks)
    return next_state


def test_hermes_capture_replay_state_sequences(adapter, monkeypatch):
    provider, _clock = adapter
    retry = provider._retry
    elapsed = [0.0]
    monkeypatch.setattr(capture_retry, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(capture_retry, "fits", lambda deadline, took: fits(deadline, took, now=elapsed[0]))
    # The state machine drives passes itself; production's single retry thread uses these same methods.
    monkeypatch.setattr(kept_captures, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(retry, "start", lambda: None)
    identity = provider._require_identity()
    context = identity.trusted_context()
    monkeypatch.setattr(provider._writer, "private_state", lambda *args, **kwargs: None)
    capable = [True]
    successes = set()
    requests = []
    record = provider._core.record_host_event

    def record_when_available(*args, **kwargs):
        if not capable[0]:
            raise ContractError("DEADLINE_EXCEEDED", "TEST writer unavailable")
        requests.append(args[1]["source_event_key"])
        receipt = record(*args, **kwargs)
        successes.add(args[1]["source_event_key"])
        return receipt

    monkeypatch.setattr(provider._core, "record_host_event", record_when_available)

    class ReplayStates(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.sequence = 0
            self.previous = {}
            self.cancelled = set()
            capable[0] = True

        def teardown(self):
            for key in tuple(set(retry.captures) | set(retry.on_disk)):
                retry.done(key)
            retry.given_up.clear()
            retry.disk_retry_at = None
            retry.more_on_disk = False

        @rule()
        def enqueue(self):
            if len(retry.captures) >= 16:
                return
            self.sequence += 1
            key = (f"TEST-property/{self.sequence}", 1)
            retry.captures[key] = RetryCapture(
                context,
                source_event(source_event_key=key[0]),
                (),
                next(iter(context.allowed_scope_ids)),
                identity.scope,
                elapsed[0],
            )

        @rule(available=st.booleans())
        def capability(self, available):
            capable[0] = available

        @rule(seconds=st.integers(0, 8000))
        def tick(self, seconds):
            elapsed[0] += seconds
            retry.give_up_expired(tuple(retry.captures.items()))
            retry.load_kept()

        @rule()
        def replay(self):
            retry.write_buffered(seconds=10)

        @rule()
        def revoke(self):
            if not retry.captures:
                return
            keys = set(retry.captures)
            before = len(requests)
            with monkeypatch.context() as patch:
                patch.setattr(capture_retry, "resolve_runtime_audience", lambda *args: None)
                retry.write_buffered(seconds=10)
            self.cancelled.update(keys - retry.captures.keys())
            assert len(requests) == before

        @rule()
        def restart(self):
            retry.keep_all_on_disk()
            retry.given_up.clear()
            retry.disk_retry_at = None
            retry.load_kept()

        @invariant()
        def each_capture_is_buffered_or_scheduled_on_disk(self):
            self.previous = _check_replay_state(provider, retry, self.previous, self.cancelled, successes, elapsed[0])

    run_state_machine_as_test(ReplayStates, settings=QUEUE_SETTINGS)


def test_disk_capture_wakes_again_without_a_host_restart(adapter, monkeypatch):
    provider, _clock = adapter
    retry = provider._retry
    elapsed = [0.0]
    monkeypatch.setattr(capture_retry, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(capture_retry, "fits", lambda deadline, took: fits(deadline, took, now=elapsed[0]))
    monkeypatch.setattr(kept_captures, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(retry, "start", lambda: None)
    monkeypatch.setattr(provider._writer, "private_state", lambda *args, **kwargs: None)
    identity = provider._require_identity()
    context = identity.trusted_context()
    key = ("TEST-disk-wake", 1)
    retry.captures[key] = RetryCapture(
        context, source_event(source_event_key=key[0]), (), next(iter(context.allowed_scope_ids)), identity.scope, -2000
    )
    assert retry.give_up_expired(tuple(retry.captures.items())) == [key]
    retry.load_kept()
    assert key not in retry.captures
    attempts = []

    def wait(seconds):
        elapsed[0] += seconds
        assert elapsed[0] <= HERMES_DISK_RETRY.delay() + 60
        return False

    def write(**kwargs):
        if key in retry.captures:
            attempts.append(elapsed[0])
            retry.done(key)

    monkeypatch.setattr(retry, "wake", SimpleNamespace(wait=wait, clear=lambda: None, set=lambda: None))
    monkeypatch.setattr(retry, "write_buffered", write)
    retry._run()
    assert attempts == [HERMES_DISK_RETRY.delay()]
    assert not retry.given_up and retry.thread is None


def test_disk_cooldown_does_not_load_or_scan_before_its_deadline(adapter, monkeypatch):
    provider, _clock = adapter
    retry = provider._retry
    elapsed, loads = [0.0], []
    monkeypatch.setattr(capture_retry, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(capture_retry, "fits", lambda deadline, took: fits(deadline, took, now=elapsed[0]))
    retry.given_up.add(("TEST-cooling", 1))
    retry.disk_retry_at = HERMES_DISK_RETRY.delay()

    def load(*args, **kwargs):
        loads.append(elapsed[0])
        return [], False

    def wait(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= retry.disk_retry_at:
            provider._initialized = False
        return False

    monkeypatch.setattr(kept_captures, "load", load)
    monkeypatch.setattr(retry, "write_buffered", lambda **kwargs: None)
    monkeypatch.setattr(retry, "wake", SimpleNamespace(wait=wait, clear=lambda: None, set=lambda: None))
    retry._run()
    assert loads == []


def test_concurrent_replay_keeps_one_in_flight_writer_and_does_not_expire_it(adapter, monkeypatch):
    provider, _clock = adapter
    retry = provider._retry
    elapsed = [0.0]
    monkeypatch.setattr(capture_retry, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(capture_retry, "fits", lambda deadline, took: fits(deadline, took, now=elapsed[0]))
    monkeypatch.setattr(retry, "start", lambda: None)
    identity = provider._require_identity()
    context = identity.trusted_context()
    key = ("TEST-concurrent-replay", 1)
    retry.captures[key] = RetryCapture(
        context, source_event(source_event_key=key[0]), (), next(iter(context.allowed_scope_ids)), identity.scope, 0
    )
    entered, finish = threading.Event(), threading.Event()
    calls, errors = [], []
    record = provider._core.record_host_event

    def write(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert finish.wait(5), "concurrent replay did not release its writer"
        return record(*args, **kwargs)

    def replay():
        try:
            retry.write_buffered(release=True, seconds=10)
        except (AssertionError, ContractError, OSError, RuntimeError) as exc:
            errors.append(exc)

    monkeypatch.setattr(provider._core, "record_host_event", write)
    thread = threading.Thread(target=replay)
    thread.start()
    try:
        assert entered.wait(5)
        assert key in retry.in_flight
        check_transitions(state_table("Hermes capture replay"), {key: "buffered"}, {key: "in_flight"})
        retry.write_buffered(force=True, release=True, seconds=10)
        assert calls == [1]
        elapsed[0] = 2000
        assert retry.give_up_expired(tuple(retry.captures.items())) == []
        assert key in retry.in_flight
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert calls == [1] and key not in retry.in_flight and key not in retry.captures
    check_transitions(state_table("Hermes capture replay"), {key: "in_flight"}, {key: "completed"})


def test_revoked_replay_is_cancelled_without_a_store_request(adapter, monkeypatch):
    provider, _clock = adapter
    retry = provider._retry
    identity = provider._require_identity()
    context = identity.trusted_context()
    key = ("TEST-revoked-replay", 1)
    retry.captures[key] = RetryCapture(
        context, source_event(source_event_key=key[0]), (), next(iter(context.allowed_scope_ids)), identity.scope, 0
    )
    monkeypatch.setattr(capture_retry, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(capture_retry, "fits", lambda deadline, took: fits(deadline, took, now=0))
    monkeypatch.setattr(capture_retry, "resolve_runtime_audience", lambda *args: None)
    calls = []
    monkeypatch.setattr(provider._core, "record_host_event", lambda *args, **kwargs: calls.append(1))
    retry.write_buffered(seconds=10)
    assert key not in retry.captures and not calls
    assert "capture_gap:retry_authorization_revoked" in provider._diagnostics.pending_outcome_gaps
    check_transitions(state_table("Hermes capture replay"), {key: "buffered"}, {key: "cancelled"})
