"""Idle drain: keep chewing the journal backlog until it is empty."""

from __future__ import annotations

import importlib

from scope_recall.provider import ScopeRecallMemoryProvider


def _provider() -> ScopeRecallMemoryProvider:
    provider = ScopeRecallMemoryProvider.__new__(ScopeRecallMemoryProvider)
    provider._shutdown_requested = type("E", (), {"is_set": lambda self: False})()
    provider._hermes_home = type("P", (), {})()
    provider._foreground_busy_count = 0
    provider._journal_digest_lock = __import__("threading").RLock()
    provider._journal_digest_consecutive_failures = 0
    provider._journal_digest_needs_resume = False
    provider._last_journal_digest_status = "never_run"
    provider._last_journal_digest_error = ""
    provider._last_journal_digest_finished = 0.0
    provider._last_adjudication_at = 0.0
    provider._last_adjudication_report = {}
    provider._memory_isolated_for_scope = lambda: False
    provider._background_digest_scope = lambda: None
    provider._recover_sqlite_connection_after_error = lambda _ctx: {"recovered": False}
    provider._rollback_conn_after_error = lambda _ctx: None
    provider._background.maybe_promote = lambda *, trigger: promotions.append("p")
    provider._background.maybe_adjudicate = lambda *, trigger: adjudications.append("a")
    provider._coerce_journal_float = ScopeRecallMemoryProvider._coerce_journal_float.__get__(
        provider, ScopeRecallMemoryProvider
    )
    return provider


promotions: list[str] = []
adjudications: list[str] = []


def setup_function() -> None:
    promotions.clear()
    adjudications.clear()


def test_idle_drain_repeats_until_backlog_empty(monkeypatch):
    provider = _provider()
    queue = [
        {"ok": True, "status": "ok", "processed_entries": 80, "backlog_after": 200, "backlog_delta": -80},
        {"ok": True, "status": "ok", "processed_entries": 80, "backlog_after": 40, "backlog_delta": -80},
        {"ok": True, "status": "ok", "processed_entries": 40, "backlog_after": 0, "backlog_delta": -40},
    ]
    calls: list[int] = []

    def fake_digest(**_kwargs):
        calls.append(1)
        return queue.pop(0)

    module = importlib.import_module(ScopeRecallMemoryProvider.__module__)
    monkeypatch.setattr(module, "run_journal_digest", fake_digest)
    monkeypatch.setattr(module.threading.current_thread(), "name", "scope-recall-journal-digest", raising=False)
    monkeypatch.setattr(module.threading, "current_thread", lambda: type("T", (), {"name": "scope-recall-journal-digest"})())

    provider._run_background_journal_digest(
        {
            "extractor": "heuristic",
            "background_digest_drain_while_idle": True,
            "background_digest_synchronous": False,
            "background_digest_max_passes": 20,
            "background_digest_idle_pause_seconds": 0,
            "digest_interval_hours": 2,
        }
    )

    assert calls == [1, 1, 1]
    assert provider._journal_digest_needs_resume is False
    assert promotions == ["p"]
    assert adjudications == ["a"]


def test_idle_drain_stops_when_nothing_moves(monkeypatch):
    provider = _provider()
    calls = {"n": 0}

    def fake_digest(**_kwargs):
        calls["n"] += 1
        return {"ok": True, "status": "ok", "processed_entries": 0, "backlog_after": 50, "backlog_delta": 0}

    module = importlib.import_module(ScopeRecallMemoryProvider.__module__)
    monkeypatch.setattr(module, "run_journal_digest", fake_digest)
    provider._run_background_journal_digest(
        {
            "extractor": "heuristic",
            "background_digest_drain_while_idle": True,
            "background_digest_synchronous": False,
            "background_digest_idle_pause_seconds": 0,
            "digest_interval_hours": 2,
        }
    )
    assert calls["n"] == 1
    assert provider._journal_digest_needs_resume is True


def test_idle_drain_yields_when_a_turn_starts(monkeypatch):
    provider = _provider()
    calls = {"n": 0}

    def fake_digest(**_kwargs):
        calls["n"] += 1
        provider._foreground_busy_count = 1
        return {"ok": True, "status": "ok", "processed_entries": 80, "backlog_after": 400, "backlog_delta": -80}

    module = importlib.import_module(ScopeRecallMemoryProvider.__module__)
    monkeypatch.setattr(module, "run_journal_digest", fake_digest)
    provider._run_background_journal_digest(
        {
            "extractor": "heuristic",
            "background_digest_drain_while_idle": True,
            "background_digest_synchronous": False,
            "background_digest_idle_pause_seconds": 0,
            "digest_interval_hours": 2,
        }
    )
    assert calls["n"] == 1
    assert provider._journal_digest_needs_resume is True


def test_synchronous_digest_stays_single_pass(monkeypatch):
    provider = _provider()
    calls = {"n": 0}

    def fake_digest(**_kwargs):
        calls["n"] += 1
        return {"ok": True, "status": "ok", "processed_entries": 10, "backlog_after": 90, "backlog_delta": -10}

    module = importlib.import_module(ScopeRecallMemoryProvider.__module__)
    monkeypatch.setattr(module, "run_journal_digest", fake_digest)
    provider._run_background_journal_digest(
        {
            "extractor": "heuristic",
            "background_digest_drain_while_idle": True,
            "background_digest_synchronous": True,
            "background_digest_idle_pause_seconds": 0,
            "digest_interval_hours": 2,
        }
    )
    assert calls["n"] == 1
